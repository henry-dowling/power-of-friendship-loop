from __future__ import annotations

import hashlib
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AgentConfig, LoopConfig

STATUS_INTERVAL_SECONDS = 5.0
HEADFUL_AUTO_EXIT_IDLE_SECONDS = 1.5
HEADFUL_AUTO_EXIT_POLL_SECONDS = 0.2
GIT_COMMAND_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class WorkspaceSnapshot:
    head: str | None
    status_lines: tuple[str, ...]
    dirty_digest: str


@dataclass(frozen=True)
class WorkspaceChanges:
    commits: tuple[str, ...]
    status_lines: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return bool(self.commits or self.status_lines)

    def to_json(self) -> dict[str, object]:
        return {
            "changed": self.changed,
            "commits": list(self.commits),
            "status": list(self.status_lines),
        }


@dataclass(frozen=True)
class TurnRecord:
    iteration: int
    agent: str
    command: list[str]
    returncode: int
    output: str
    duration_seconds: float
    completed: bool
    prompt_file: Path
    workspace_changes: WorkspaceChanges | None


@dataclass(frozen=True)
class LoopResult:
    completed: bool
    iterations_run: int
    transcript_path: Path
    records: list[TurnRecord]
    workspace_changes: WorkspaceChanges | None


@dataclass(frozen=True)
class DoctorResult:
    agent: str
    executable: str
    resolved: str | None
    ok: bool


class HarnessError(RuntimeError):
    pass


class AgentCommandError(HarnessError):
    def __init__(self, record: TurnRecord, transcript_path: Path):
        self.record = record
        self.transcript_path = transcript_path
        super().__init__(
            f"{record.agent} exited with status {record.returncode} on iteration {record.iteration}"
        )


class MissingExecutableError(HarnessError):
    def __init__(self, agent: str, executable: str):
        self.agent = agent
        self.executable = executable
        super().__init__(f"{agent} executable not found on PATH: {executable}")


class FriendshipLoop:
    def __init__(
        self,
        config: LoopConfig,
        workspace: Path,
        prompt: str,
        iterations: int,
        transcript_path: Path | None = None,
        continue_on_error: bool = False,
        timeout_seconds: float | None = None,
        stream: bool = True,
        headful: bool = False,
    ) -> None:
        if iterations < 1:
            raise ValueError("iterations must be at least 1")
        self.config = config
        self.workspace = workspace.resolve()
        self.prompt = prompt
        self.iterations = iterations
        self.continue_on_error = continue_on_error
        self.timeout_seconds = timeout_seconds
        self.stream = stream
        self.headful = headful
        self.run_dir, self.transcript_path = _run_paths(self.workspace, transcript_path)

    def run(self) -> LoopResult:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        records: list[TurnRecord] = []
        agreement_agents: list[str] = []
        required_agents = {agent.name for agent in self.config.agents}
        initial_workspace = capture_workspace_snapshot(self.workspace)

        self._write_event(
            {
                "type": "run_start",
                "workspace": str(self.workspace),
                "agents": [agent.name for agent in self.config.agents],
                "max_turns": self.iterations,
                "completion_token": self.config.completion_token,
                "completion_policy": "all_agents",
            }
        )
        self._print_status(
            f"run started; agents={', '.join(agent.name for agent in self.config.agents)}; "
            f"transcript={self.transcript_path}"
        )
        self._print_status("completion requires every configured agent to agree on consecutive turns")

        for iteration in range(1, self.iterations + 1):
            agent = self.config.agents[(iteration - 1) % len(self.config.agents)]
            record = self._run_turn(agent, iteration, records, agreement_agents)
            records.append(record)

            if record.completed:
                agreement_agents.append(agent.name)
                if required_agents.issubset(agreement_agents):
                    self._write_event(
                        {
                            "type": "run_complete",
                            "iteration": iteration,
                            "agent": agent.name,
                            "agreement_agents": agreement_agents,
                        }
                    )
                    workspace_changes = compare_workspace_snapshots(
                        self.workspace,
                        initial_workspace,
                        capture_workspace_snapshot(self.workspace),
                    )
                    return LoopResult(
                        completed=True,
                        iterations_run=iteration,
                        transcript_path=self.transcript_path,
                        records=records,
                        workspace_changes=workspace_changes,
                    )
            else:
                agreement_agents = []

            if record.returncode != 0 and not self.continue_on_error:
                self._write_event(
                    {
                        "type": "run_failed",
                        "iteration": iteration,
                        "agent": agent.name,
                        "returncode": record.returncode,
                    }
                )
                raise AgentCommandError(record, self.transcript_path)

        self._write_event({"type": "run_exhausted", "max_turns": self.iterations})
        workspace_changes = compare_workspace_snapshots(
            self.workspace,
            initial_workspace,
            capture_workspace_snapshot(self.workspace),
        )
        return LoopResult(
            completed=False,
            iterations_run=self.iterations,
            transcript_path=self.transcript_path,
            records=records,
            workspace_changes=workspace_changes,
        )

    def dry_run(self) -> list[tuple[int, AgentConfig, list[str]]]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        planned: list[tuple[int, AgentConfig, list[str]]] = []
        for iteration in range(1, self.iterations + 1):
            agent = self.config.agents[(iteration - 1) % len(self.config.agents)]
            prompt_file = self._prompt_file_path(agent, iteration)
            command = self._command_for_agent(agent)
            argv = render_command(
                command,
                prompt="<prompt>",
                prompt_file=prompt_file,
                workspace=self.workspace,
                transcript=self.transcript_path,
                iteration=iteration,
                agent=agent.name,
            )
            planned.append((iteration, agent, argv))
        return planned

    def _run_turn(
        self,
        agent: AgentConfig,
        iteration: int,
        records: list[TurnRecord],
        agreement_agents: list[str],
    ) -> TurnRecord:
        command = self._command_for_agent(agent)
        executable = command[0]
        if shutil.which(executable) is None and not Path(executable).exists():
            raise MissingExecutableError(agent.name, executable)

        turn_done_token = self._turn_done_token(agent, iteration) if self.headful else None
        turn_prompt = build_turn_prompt(
            base_prompt=self.prompt,
            iteration=iteration,
            max_iterations=self.iterations,
            agent=agent.name,
            agent_order=[configured.name for configured in self.config.agents],
            agreement_agents=agreement_agents,
            previous_records=records,
            transcript_path=self.transcript_path,
            completion_token=self.config.completion_token,
            turn_done_token=turn_done_token,
            context_chars=self.config.context_chars,
        )
        prompt_file = self._prompt_file_path(agent, iteration)
        prompt_file.write_text(turn_prompt, encoding="utf-8")
        argv = render_command(
            command,
            prompt=turn_prompt,
            prompt_file=prompt_file,
            workspace=self.workspace,
            transcript=self.transcript_path,
            iteration=iteration,
            agent=agent.name,
        )

        self._write_event(
            {
                "type": "turn_start",
                "iteration": iteration,
                "agent": agent.name,
                "command": redact_prompt(argv, turn_prompt),
                "prompt_file": str(prompt_file),
            }
        )
        self._print_status(f"turn {iteration:03d}/{self.iterations:03d}: running {agent.name}")
        self._print_status(f"prompt file: {prompt_file}")
        self._print_status(f"command: {shlex.join(redact_prompt(argv, turn_prompt))}")

        workspace_before = capture_workspace_snapshot(self.workspace)
        started = time.monotonic()
        if self.headful:
            output, returncode = run_headful_command(
                argv,
                cwd=self.workspace,
                timeout_seconds=self.timeout_seconds,
                stream=self.stream,
                run_dir=self.run_dir,
                agent=agent.name,
                iteration=iteration,
                completion_token=self.config.completion_token,
                turn_done_token=turn_done_token,
            )
        else:
            output, returncode = run_command(
                argv,
                cwd=self.workspace,
                timeout_seconds=self.timeout_seconds,
                stream=self.stream,
                status_label=f"{agent.name} turn {iteration:03d}",
            )
        duration = time.monotonic() - started
        workspace_changes = compare_workspace_snapshots(
            self.workspace,
            workspace_before,
            capture_workspace_snapshot(self.workspace),
        )
        completed = self.config.completion_token in output
        agreement = "agreed" if completed else "not complete"
        self._print_status(f"{agent.name} finished in {duration:.1f}s with exit {returncode} ({agreement})")
        self._print_workspace_changes(workspace_changes)
        record = TurnRecord(
            iteration=iteration,
            agent=agent.name,
            command=argv,
            returncode=returncode,
            output=output,
            duration_seconds=duration,
            completed=completed,
            prompt_file=prompt_file,
            workspace_changes=workspace_changes,
        )
        event: dict[str, object] = {
            "type": "turn_result",
            "iteration": iteration,
            "agent": agent.name,
            "returncode": returncode,
            "duration_seconds": round(duration, 3),
            "completed": completed,
            "output": output,
        }
        if workspace_changes is not None:
            event["workspace_changes"] = workspace_changes.to_json()
        self._write_event(event)
        return record

    def _write_event(self, event: dict[str, object]) -> None:
        event_with_time = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self.transcript_path.open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps(event_with_time, ensure_ascii=False) + "\n")

    def _print_status(self, message: str) -> None:
        if not self.stream:
            return
        print(f"[pof] {message}", flush=True)

    def _print_workspace_changes(self, changes: WorkspaceChanges | None) -> None:
        if changes is None or not changes.changed:
            return

        for message in format_workspace_changes(changes):
            self._print_status(message)

    def _prompt_file_path(self, agent: AgentConfig, iteration: int) -> Path:
        safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in agent.name)
        return self.run_dir / f"{iteration:03d}-{safe_agent}-prompt.md"

    def _turn_done_token(self, agent: AgentConfig, iteration: int) -> str:
        safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in agent.name)
        return f"POF_TURN_DONE_{os.getpid()}_{iteration:03d}_{safe_agent}"

    def _command_for_agent(self, agent: AgentConfig) -> list[str]:
        if self.headful and agent.headful_command:
            return agent.headful_command
        return agent.command


def capture_workspace_snapshot(workspace: Path) -> WorkspaceSnapshot | None:
    if shutil.which("git") is None:
        return None

    inside_work_tree = _run_git(workspace, ["rev-parse", "--is-inside-work-tree"])
    if inside_work_tree is None or inside_work_tree.returncode != 0:
        return None
    if inside_work_tree.stdout.strip() != "true":
        return None

    head = _read_git_head(workspace)
    status_lines = _read_git_status_lines(workspace)
    return WorkspaceSnapshot(
        head=head,
        status_lines=status_lines,
        dirty_digest=_read_git_dirty_digest(workspace, status_lines),
    )


def compare_workspace_snapshots(
    workspace: Path,
    before: WorkspaceSnapshot | None,
    after: WorkspaceSnapshot | None,
) -> WorkspaceChanges | None:
    if before is None or after is None:
        return None

    commits = _read_git_commit_lines(workspace, before.head, after.head)
    status_lines: tuple[str, ...] = ()
    if before.dirty_digest != after.dirty_digest:
        status_lines = after.status_lines

    return WorkspaceChanges(commits=commits, status_lines=status_lines)


def format_workspace_changes(changes: WorkspaceChanges) -> list[str]:
    messages: list[str] = []
    if changes.commits:
        messages.append(f"workspace commits: {_format_summary_items(changes.commits)}")
    if changes.status_lines:
        messages.append(f"workspace files: {_format_summary_items(changes.status_lines)}")
    return messages


def _read_git_head(workspace: Path) -> str | None:
    result = _run_git(workspace, ["rev-parse", "--verify", "HEAD"])
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _read_git_status_lines(workspace: Path) -> tuple[str, ...]:
    result = _run_git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"])
    if result is None or result.returncode != 0:
        return ()

    return tuple(
        line
        for line in result.stdout.splitlines()
        if line and not _is_pof_status_line(line)
    )


def _read_git_dirty_digest(workspace: Path, status_lines: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update("\0".join(status_lines).encode("utf-8", errors="replace"))

    for args in (
        ["diff", "--binary", "--no-ext-diff"],
        ["diff", "--cached", "--binary", "--no-ext-diff"],
    ):
        result = _run_git(workspace, args)
        if result is not None and result.returncode == 0:
            digest.update(result.stdout.encode("utf-8", errors="replace"))

    return digest.hexdigest()


def _read_git_commit_lines(
    workspace: Path,
    before_head: str | None,
    after_head: str | None,
) -> tuple[str, ...]:
    if before_head == after_head:
        return ()
    if after_head is None:
        before = _short_commit(before_head)
        return (f"HEAD changed from {before} to no commit",)

    if before_head is None:
        result = _run_git(
            workspace,
            ["log", "--oneline", "--decorate=short", "--no-color", "--max-count=5", after_head],
        )
    else:
        result = _run_git(
            workspace,
            ["log", "--oneline", "--decorate=short", "--no-color", f"{before_head}..{after_head}"],
        )

    if result is not None and result.returncode == 0:
        lines = tuple(line for line in result.stdout.splitlines() if line)
        if lines:
            return lines

    before = _short_commit(before_head)
    after = _short_commit(after_head)
    return (f"HEAD changed from {before} to {after}",)


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _is_pof_status_line(line: str) -> bool:
    paths = line[3:] if len(line) > 3 else line
    return any(_is_pof_path(path.strip()) for path in paths.split(" -> "))


def _is_pof_path(path: str) -> bool:
    return path == ".pof" or path.startswith(".pof/")


def _short_commit(commit: str | None) -> str:
    if commit is None:
        return "none"
    return commit[:7]


def _format_summary_items(items: tuple[str, ...], limit: int = 6) -> str:
    visible = list(items[:limit])
    summary = ", ".join(visible)
    hidden_count = len(items) - len(visible)
    if hidden_count > 0:
        summary += f", and {hidden_count} more"
    return summary


def doctor(config: LoopConfig) -> list[DoctorResult]:
    results: list[DoctorResult] = []
    for agent in config.agents:
        executable = agent.command[0]
        resolved = shutil.which(executable)
        if resolved is None and Path(executable).exists():
            resolved = str(Path(executable).resolve())
        results.append(
            DoctorResult(
                agent=agent.name,
                executable=executable,
                resolved=resolved,
                ok=resolved is not None,
            )
        )
    return results


def build_turn_prompt(
    *,
    base_prompt: str,
    iteration: int,
    max_iterations: int,
    agent: str,
    agent_order: list[str],
    agreement_agents: list[str],
    previous_records: list[TurnRecord],
    transcript_path: Path,
    completion_token: str,
    turn_done_token: str | None,
    context_chars: int,
) -> str:
    history = format_history(previous_records, context_chars)
    agent_list = " -> ".join(agent_order)
    next_agent = agent_order[iteration % len(agent_order)]
    turn_done_instruction = ""
    if turn_done_token is not None:
        turn_done_instruction = f"""
- End your final output with this exact line so pof can advance to the next agent:
  {turn_done_token}"""
    return f"""You are participating in the power-of-friendship loop.

The loop is a round-robin coding harness. One agent runs per turn, then the
next agent continues from the current filesystem state and transcript.

Loop state:
- Current agent: {agent}
- Current turn: {iteration} of {max_iterations}
- Agent rotation: {agent_list}
- Next agent if more agreement is needed: {next_agent}
- Current completion agreement: {format_agreement(agreement_agents, agent_order)}
- Transcript path: {transcript_path}

Instructions:
- Treat the workspace filesystem as the source of truth.
- Inspect the relevant files before making claims.
- Make concrete progress toward the task during this turn if work remains.
- Preserve work from previous agents unless the task requires changing it.
- The loop only succeeds after every configured agent agrees on consecutive turns.
- If the whole task is complete and verified, include this exact token in your final output:
  {completion_token}
- If the task is not complete, do not print the completion token. Summarize what changed and what
  the next agent should do. This resets the current agreement window.
{turn_done_instruction}

Original task:
{base_prompt.strip()}

Recent loop transcript:
{history}
"""


def format_agreement(agreement_agents: list[str], agent_order: list[str]) -> str:
    if not agreement_agents:
        return "none yet"
    agreed_agents = set(agreement_agents)
    missing_agents = [agent for agent in agent_order if agent not in agreed_agents]
    agreed_text = ", ".join(agreement_agents)
    if not missing_agents:
        return f"{agreed_text}; all configured agents have agreed"
    missing_text = ", ".join(missing_agents)
    return f"{agreed_text}; still waiting for {missing_text}"


def format_history(records: list[TurnRecord], context_chars: int) -> str:
    if not records:
        return "No previous turns in this run."

    chunks = []
    for record in records:
        chunks.append(
            f"## Iteration {record.iteration}: {record.agent} "
            f"(exit {record.returncode}, {record.duration_seconds:.1f}s)\n"
            f"{record.output.strip()}"
        )
    history = "\n\n".join(chunks).strip()
    if len(history) <= context_chars:
        return history
    return "[Earlier transcript trimmed]\n" + history[-context_chars:]


def render_command(
    command: list[str],
    *,
    prompt: str,
    prompt_file: Path,
    workspace: Path,
    transcript: Path,
    iteration: int,
    agent: str,
) -> list[str]:
    values = {
        "prompt": prompt,
        "prompt_file": str(prompt_file),
        "workspace": str(workspace),
        "transcript": str(transcript),
        "iteration": str(iteration),
        "agent": agent,
    }
    rendered = []
    for part in command:
        rendered.append(part.format(**values))
    return rendered


def redact_prompt(argv: list[str], prompt: str) -> list[str]:
    return ["<prompt>" if part == prompt else part for part in argv]


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float | None,
    stream: bool,
    status_label: str | None = None,
) -> tuple[str, int]:
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=os.environ.copy(),
    )
    output_parts: list[str] = []
    started = time.monotonic()
    last_status = started
    last_activity = started
    stream_needs_newline = False
    output_queue: queue.Queue[str | None] = queue.Queue()

    assert process.stdout is not None
    stdout = process.stdout

    def read_output() -> None:
        try:
            while True:
                chunk = stdout.read(1)
                if not chunk:
                    break
                output_queue.put(chunk)
        finally:
            stdout.close()
            output_queue.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    reader_done = False

    while True:
        try:
            line = output_queue.get(timeout=0.05)
        except queue.Empty:
            line = ""

        if line is None:
            reader_done = True
        elif line:
            last_activity = time.monotonic()
            output_parts.append(line)
            if stream:
                print(line, end="")
                sys.stdout.flush()
                stream_needs_newline = not line.endswith("\n")

        if reader_done and process.poll() is not None:
            break

        now = time.monotonic()
        if (
            stream
            and status_label
            and now - last_activity >= STATUS_INTERVAL_SECONDS
            and now - last_status >= STATUS_INTERVAL_SECONDS
        ):
            if stream_needs_newline:
                print()
                stream_needs_newline = False
            print(f"[pof] {status_label} still running ({now - started:.0f}s elapsed)", flush=True)
            last_status = now

        if timeout_seconds is not None and time.monotonic() - started > timeout_seconds:
            process.kill()
            process.wait()
            reader.join(timeout=1)
            while not output_queue.empty():
                remaining = output_queue.get_nowait()
                if remaining:
                    output_parts.append(remaining)
                    if stream:
                        print(remaining, end="")
                        stream_needs_newline = not remaining.endswith("\n")
            output_parts.append(f"\n[pof] command timed out after {timeout_seconds:g} seconds\n")
            return "".join(output_parts), 124

    reader.join(timeout=1)
    return "".join(output_parts), process.returncode or 0


def run_headful_command(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: float | None,
    stream: bool,
    run_dir: Path,
    agent: str,
    iteration: int,
    completion_token: str,
    turn_done_token: str | None,
) -> tuple[str, int]:
    if shutil.which("tmux") is None:
        raise HarnessError("headful mode requires tmux on PATH")
    if stream and not sys.stdin.isatty():
        raise HarnessError("headful mode requires a real terminal; run pof from an interactive shell")

    safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in agent)
    session_name = f"pof-{os.getpid()}-{iteration:03d}-{safe_agent}"
    script_path = run_dir / f"{iteration:03d}-{safe_agent}-headful.sh"
    status_path = run_dir / f"{iteration:03d}-{safe_agent}-status.txt"
    auto_status_path = run_dir / f"{iteration:03d}-{safe_agent}-auto-status.txt"
    completion_seen_path = run_dir / f"{iteration:03d}-{safe_agent}-completion-seen.txt"
    pane_log_path = run_dir / f"{iteration:03d}-{safe_agent}-pane.log"
    command_line = shlex.join(argv)
    script_path.write_text(
        "#!/bin/sh\n"
        "status=0\n"
        f"printf '%s\\n' {shlex.quote('[pof] Headful turn started. pof will advance when the turn-done marker appears.')}\n"
        f"{command_line}\n"
        "status=$?\n"
        f"printf '\\n[pof] {agent} exited with status %s; returning to pof...\\n' \"$status\"\n"
        f"printf '%s\\n' \"$status\" > {shlex.quote(str(status_path))}\n"
        f"tmux detach-client -s {shlex.quote(session_name)} 2>/dev/null || true\n"
        "exit \"$status\"\n",
        encoding="utf-8",
    )
    script_path.chmod(0o700)

    _run_tmux(["new-session", "-d", "-s", session_name, "-c", str(cwd)])
    try:
        _run_tmux(["set-option", "-t", session_name, "remain-on-exit", "on"])
        _run_tmux(["pipe-pane", "-o", "-t", session_name, f"cat > {shlex.quote(str(pane_log_path))}"])
        _run_tmux(["send-keys", "-t", session_name, shlex.join([str(script_path)]), "C-m"])

        started = time.monotonic()
        stop_monitor = threading.Event()
        monitor = _start_headful_auto_exit_monitor(
            session_name=session_name,
            pane_log_path=pane_log_path,
            status_path=status_path,
            auto_status_path=auto_status_path,
            completion_seen_path=completion_seen_path,
            completion_token=completion_token,
            turn_done_token=turn_done_token,
            stop=stop_monitor,
        )
        try:
            while not status_path.exists() and not auto_status_path.exists():
                if timeout_seconds is not None:
                    remaining_timeout = timeout_seconds - (time.monotonic() - started)
                    if remaining_timeout <= 0:
                        _kill_tmux_session(session_name)
                        output = _read_text_if_exists(pane_log_path)
                        output += f"\n[pof] headful command timed out after {timeout_seconds:g} seconds\n"
                        return output, 124
                else:
                    remaining_timeout = None

                if not _tmux_has_session(session_name):
                    break

                if stream:
                    attach_timeout = remaining_timeout
                    if attach_timeout is not None:
                        attach_timeout = max(0.1, attach_timeout)
                    try:
                        subprocess.run(["tmux", "attach-session", "-t", session_name], check=False, timeout=attach_timeout)
                    except subprocess.TimeoutExpired:
                        _kill_tmux_session(session_name)
                        output = _read_text_if_exists(pane_log_path)
                        output += f"\n[pof] headful command timed out after {timeout_seconds:g} seconds\n"
                        return output, 124

                    if not status_path.exists() and not auto_status_path.exists() and _tmux_has_session(session_name):
                        print(f"[pof] {agent} turn {iteration:03d} is still active; reattaching tmux session", flush=True)
                else:
                    time.sleep(HEADFUL_AUTO_EXIT_POLL_SECONDS)
        finally:
            stop_monitor.set()
            monitor.join(timeout=1)

        if _tmux_has_session(session_name):
            _run_tmux(["pipe-pane", "-t", session_name], check=False)
        output = _read_text_if_exists(pane_log_path)
        if completion_seen_path.exists() and completion_token not in output:
            output += f"\n{completion_token}\n"
        if auto_status_path.exists():
            return output, 0
        if not status_path.exists():
            return output, 1
        status_text = status_path.read_text(encoding="utf-8").strip()
        try:
            return output, int(status_text)
        except ValueError:
            return output, 1
    finally:
        if _tmux_has_session(session_name):
            _run_tmux(["pipe-pane", "-t", session_name], check=False)
            _kill_tmux_session(session_name)


def _start_headful_auto_exit_monitor(
    *,
    session_name: str,
    pane_log_path: Path,
    status_path: Path,
    auto_status_path: Path,
    completion_seen_path: Path,
    completion_token: str,
    turn_done_token: str | None,
    stop: threading.Event,
) -> threading.Thread:
    completion_markers = _completion_display_markers(completion_token)

    def monitor() -> None:
        last_output = ""
        last_change = time.monotonic()
        saw_turn_done = False
        saw_completion = False

        while not stop.is_set():
            if status_path.exists() or auto_status_path.exists() or not _tmux_has_session(session_name):
                return

            output = _read_text_if_exists(pane_log_path)
            now = time.monotonic()
            if output != last_output:
                last_output = output
                last_change = now

            if output:
                if turn_done_token is not None and turn_done_token in output:
                    saw_turn_done = True
                if any(marker in output for marker in completion_markers):
                    saw_completion = True

            should_exit = saw_turn_done or saw_completion
            if should_exit and now - last_change >= HEADFUL_AUTO_EXIT_IDLE_SECONDS:
                if saw_completion:
                    completion_seen_path.write_text("1\n", encoding="utf-8")
                auto_status_path.write_text("0\n", encoding="utf-8")
                _run_tmux(["display-message", "-t", session_name, "pof detected turn completion; cycling"], check=False)
                _run_tmux(["detach-client", "-s", session_name], check=False)
                _kill_tmux_session(session_name)
                return

            time.sleep(HEADFUL_AUTO_EXIT_POLL_SECONDS)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    return thread


def _completion_display_markers(completion_token: str) -> list[str]:
    markers = [completion_token]
    if completion_token.startswith("<") and completion_token.endswith(">"):
        inner_start = completion_token.find(">")
        inner_end = completion_token.rfind("<")
        if 0 <= inner_start < inner_end:
            inner = completion_token[inner_start + 1 : inner_end].strip()
            if inner:
                markers.append(inner)
    return markers


def _run_tmux(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["tmux", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise HarnessError(f"tmux {' '.join(args)} failed: {detail}")
    return result


def _tmux_has_session(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _kill_tmux_session(session_name: str) -> None:
    subprocess.run(
        ["tmux", "kill-session", "-t", session_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _run_paths(workspace: Path, transcript_path: Path | None) -> tuple[Path, Path]:
    if transcript_path is not None:
        resolved_transcript = transcript_path.expanduser().resolve()
        return resolved_transcript.parent, resolved_transcript

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = workspace.resolve() / ".pof" / "runs" / run_id
    return run_dir, run_dir / "transcript.jsonl"
