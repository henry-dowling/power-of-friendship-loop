from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import AgentConfig, LoopConfig


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


@dataclass(frozen=True)
class LoopResult:
    completed: bool
    iterations_run: int
    transcript_path: Path
    records: list[TurnRecord]


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
        self.run_dir, self.transcript_path = _run_paths(self.workspace, transcript_path)

    def run(self) -> LoopResult:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        records: list[TurnRecord] = []

        self._write_event(
            {
                "type": "run_start",
                "workspace": str(self.workspace),
                "agents": [agent.name for agent in self.config.agents],
                "iterations": self.iterations,
                "completion_token": self.config.completion_token,
            }
        )

        for iteration in range(1, self.iterations + 1):
            agent = self.config.agents[(iteration - 1) % len(self.config.agents)]
            record = self._run_turn(agent, iteration, records)
            records.append(record)

            if record.completed:
                self._write_event({"type": "run_complete", "iteration": iteration, "agent": agent.name})
                return LoopResult(
                    completed=True,
                    iterations_run=iteration,
                    transcript_path=self.transcript_path,
                    records=records,
                )

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

        self._write_event({"type": "run_exhausted", "iterations": self.iterations})
        return LoopResult(
            completed=False,
            iterations_run=self.iterations,
            transcript_path=self.transcript_path,
            records=records,
        )

    def dry_run(self) -> list[tuple[int, AgentConfig, list[str]]]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        planned: list[tuple[int, AgentConfig, list[str]]] = []
        for iteration in range(1, self.iterations + 1):
            agent = self.config.agents[(iteration - 1) % len(self.config.agents)]
            prompt_file = self._prompt_file_path(agent, iteration)
            argv = render_command(
                agent.command,
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
    ) -> TurnRecord:
        executable = agent.command[0]
        if shutil.which(executable) is None and not Path(executable).exists():
            raise MissingExecutableError(agent.name, executable)

        turn_prompt = build_turn_prompt(
            base_prompt=self.prompt,
            iteration=iteration,
            max_iterations=self.iterations,
            agent=agent.name,
            agent_order=[configured.name for configured in self.config.agents],
            previous_records=records,
            transcript_path=self.transcript_path,
            completion_token=self.config.completion_token,
            context_chars=self.config.context_chars,
        )
        prompt_file = self._prompt_file_path(agent, iteration)
        prompt_file.write_text(turn_prompt, encoding="utf-8")
        argv = render_command(
            agent.command,
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

        started = time.monotonic()
        output, returncode = run_command(
            argv,
            cwd=self.workspace,
            timeout_seconds=self.timeout_seconds,
            stream=self.stream,
        )
        duration = time.monotonic() - started
        completed = self.config.completion_token in output
        record = TurnRecord(
            iteration=iteration,
            agent=agent.name,
            command=argv,
            returncode=returncode,
            output=output,
            duration_seconds=duration,
            completed=completed,
            prompt_file=prompt_file,
        )
        self._write_event(
            {
                "type": "turn_result",
                "iteration": iteration,
                "agent": agent.name,
                "returncode": returncode,
                "duration_seconds": round(duration, 3),
                "completed": completed,
                "output": output,
            }
        )
        return record

    def _write_event(self, event: dict[str, object]) -> None:
        event_with_time = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        with self.transcript_path.open("a", encoding="utf-8") as transcript:
            transcript.write(json.dumps(event_with_time, ensure_ascii=False) + "\n")

    def _prompt_file_path(self, agent: AgentConfig, iteration: int) -> Path:
        safe_agent = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in agent.name)
        return self.run_dir / f"{iteration:03d}-{safe_agent}-prompt.md"


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
    previous_records: list[TurnRecord],
    transcript_path: Path,
    completion_token: str,
    context_chars: int,
) -> str:
    history = format_history(previous_records, context_chars)
    agent_list = " -> ".join(agent_order)
    next_agent = agent_order[iteration % len(agent_order)]
    return f"""You are participating in the power-of-friendship loop.

The loop is a round-robin coding harness. One agent runs per iteration, then the
next agent continues from the current filesystem state and transcript.

Loop state:
- Current agent: {agent}
- Current iteration: {iteration} of {max_iterations}
- Agent rotation: {agent_list}
- Next agent if this turn is not complete: {next_agent}
- Transcript path: {transcript_path}

Instructions:
- Treat the workspace filesystem as the source of truth.
- Inspect the relevant files before making claims.
- Make concrete progress toward the task during this turn.
- Preserve work from previous agents unless the task requires changing it.
- If the whole task is complete and verified, include this exact token in your final output:
  {completion_token}
- If the task is not complete, do not print the completion token. Summarize what changed and what
  the next agent should do.

Original task:
{base_prompt.strip()}

Recent loop transcript:
{history}
"""


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
    output_queue: queue.Queue[str | None] = queue.Queue()

    assert process.stdout is not None
    stdout = process.stdout

    def read_output() -> None:
        try:
            for line in stdout:
                output_queue.put(line)
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
            output_parts.append(line)
            if stream:
                print(line, end="")
                sys.stdout.flush()

        if reader_done and process.poll() is not None:
            break

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
            output_parts.append(f"\n[pof] command timed out after {timeout_seconds:g} seconds\n")
            return "".join(output_parts), 124

    reader.join(timeout=1)
    return "".join(output_parts), process.returncode or 0


def _run_paths(workspace: Path, transcript_path: Path | None) -> tuple[Path, Path]:
    if transcript_path is not None:
        resolved_transcript = transcript_path.expanduser().resolve()
        return resolved_transcript.parent, resolved_transcript

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = workspace.resolve() / ".pof" / "runs" / run_id
    return run_dir, run_dir / "transcript.jsonl"
