from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated, Sequence

import click
import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from .config import ConfigError, LoopConfig, load_config, write_default_files
from .harness import (
    AgentCommandError,
    FriendshipLoop,
    HarnessError,
    MissingExecutableError,
    WorkspaceChanges,
    doctor,
    format_workspace_changes,
)

console = Console()
DEFAULT_MAX_TURNS = 30
COMPLETION_BANNER = r'''
        .-""""-.           .-""""-.           .-""""-.
       /  CLAUDE \         /  CODEX  \        / GEMINI  \
      |    o  o   |       |   o  o   |      |   o  o   |
       \    __   /         \   __   /        \   __   /
        '-.____.-'          '-.__.-'          '-.__.-'
             \                  |                  /
              \                 |                 /
               \                |                /
                \               |               /
                 \______________|______________/
                                *
                         POWER OF FRIENDSHIP
'''


class DefaultGoalGroup(TyperGroup):
    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args:
            command = self.get_command(ctx, args[0])
            if command is not None:
                return super().resolve_command(ctx, args)

            goal_command = self.get_command(ctx, "goal")
            if goal_command is not None:
                return "goal", goal_command, args

        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="pof",
    cls=DefaultGoalGroup,
    help="Power-of-friendship loop — rotate a goal through Claude, Codex, and Gemini.",
    epilog="Omit 'goal' to run a goal directly, e.g. pof 'Fix the bug'.",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    no_args_is_help=True,
)


ConfigPath = Annotated[Path, typer.Option("--config", help="Configuration file.")]
AgentOverride = Annotated[
    list[str] | None,
    typer.Option("--agent", help="Override the configured rotation; repeat for multiple agents."),
]


@app.command()
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite existing files.")] = False,
    directory: Annotated[Path, typer.Option("--directory", "-C", help="Directory to initialize.")] = Path.cwd(),
) -> None:
    """Write starter pof.toml and PROMPT.md."""
    written = write_default_files(directory, force=force)
    if written:
        for path in written:
            console.print(f"[green]wrote[/green] {path}")
        return
    console.print("[dim]pof.toml and PROMPT.md already exist; use --force to overwrite.[/dim]")


@app.command("doctor")
def doctor_cmd(
    config: ConfigPath = Path("pof.toml"),
    agents: AgentOverride = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print machine-readable JSON.")] = False,
) -> None:
    """Check configured agent executables."""
    import json

    loop_config = _load_or_exit(config, agents)
    results = doctor(loop_config)

    if as_json:
        print(
            json.dumps(
                [
                    {
                        "agent": result.agent,
                        "executable": result.executable,
                        "resolved": result.resolved,
                        "ok": result.ok,
                    }
                    for result in results
                ]
            )
        )
    else:
        table = Table(title="Agent Commands")
        table.add_column("Agent", style="bold")
        table.add_column("Status")
        table.add_column("Executable", overflow="fold")
        for result in results:
            status = "[green]ok[/green]" if result.ok else "[red]missing[/red]"
            table.add_row(result.agent, status, result.resolved or result.executable)
        console.print(table)

    if not all(result.ok for result in results):
        raise typer.Exit(1)


@app.command()
def goal(
    objective: Annotated[
        list[str] | None,
        typer.Argument(help="Goal objective. If omitted, pof reads --from/PROMPT.md."),
    ] = None,
    from_file: Annotated[
        Path,
        typer.Option("--from", "--prompt-file", help="Read the goal objective from a file."),
    ] = Path("PROMPT.md"),
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", "--iterations", "-n", help="Safety cap for agent turns."),
    ] = DEFAULT_MAX_TURNS,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "--cd", "-C", help="Workspace to run agents in."),
    ] = Path.cwd(),
    config: ConfigPath = Path("pof.toml"),
    transcript: Annotated[Path | None, typer.Option("--transcript", help="Explicit transcript JSONL path.")] = None,
    context_chars: Annotated[
        int | None,
        typer.Option("--context-chars", help="Recent transcript characters passed to agents."),
    ] = None,
    completion_token: Annotated[
        str | None,
        typer.Option("--completion-token", help="Token that marks successful completion."),
    ] = None,
    continue_on_error: Annotated[
        bool,
        typer.Option("--continue-on-error", help="Continue after non-zero agent exits."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Per-agent timeout in seconds.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show planned turns without running agents.")] = False,
    headless: Annotated[
        bool,
        typer.Option("--headless", help="Run agents through non-interactive subprocess pipes."),
    ] = False,
    agents: AgentOverride = None,
) -> None:
    """Run a Codex-style goal through the friendship loop."""
    result = _run_goal(
        objective=objective,
        from_file=from_file,
        max_turns=max_turns,
        workspace=workspace,
        config_path=config,
        transcript=transcript,
        context_chars=context_chars,
        completion_token=completion_token,
        continue_on_error=continue_on_error,
        timeout=timeout,
        dry_run=dry_run,
        headful=not headless,
        agents=agents,
    )
    if result is not None:
        raise typer.Exit(result)


@app.command(hidden=True)
def run(
    prompt: Annotated[str | None, typer.Option("--prompt", help="Goal text.")] = None,
    prompt_file: Annotated[Path, typer.Option("--prompt-file", help="Goal file.")] = Path("PROMPT.md"),
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", "--iterations", "-n", help="Safety cap for agent turns."),
    ] = DEFAULT_MAX_TURNS,
    workspace: Annotated[
        Path,
        typer.Option("--workspace", "--cd", "-C", help="Workspace to run agents in."),
    ] = Path.cwd(),
    config: ConfigPath = Path("pof.toml"),
    transcript: Annotated[Path | None, typer.Option("--transcript", help="Explicit transcript JSONL path.")] = None,
    context_chars: Annotated[
        int | None,
        typer.Option("--context-chars", help="Recent transcript characters passed to agents."),
    ] = None,
    completion_token: Annotated[
        str | None,
        typer.Option("--completion-token", help="Token that marks successful completion."),
    ] = None,
    continue_on_error: Annotated[
        bool,
        typer.Option("--continue-on-error", help="Continue after non-zero agent exits."),
    ] = False,
    timeout: Annotated[float | None, typer.Option("--timeout", help="Per-agent timeout in seconds.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show planned turns without running agents.")] = False,
    headless: Annotated[
        bool,
        typer.Option("--headless", help="Run agents through non-interactive subprocess pipes."),
    ] = False,
    agents: AgentOverride = None,
) -> None:
    """Compatibility alias for pof goal."""
    result = _run_goal(
        objective=prompt,
        from_file=prompt_file,
        max_turns=max_turns,
        workspace=workspace,
        config_path=config,
        transcript=transcript,
        context_chars=context_chars,
        completion_token=completion_token,
        continue_on_error=continue_on_error,
        timeout=timeout,
        dry_run=dry_run,
        headful=not headless,
        agents=agents,
    )
    if result is not None:
        raise typer.Exit(result)


def _run_goal(
    *,
    objective: str | Sequence[str] | None,
    from_file: Path,
    max_turns: int,
    workspace: Path,
    config_path: Path,
    transcript: Path | None,
    context_chars: int | None,
    completion_token: str | None,
    continue_on_error: bool,
    timeout: float | None,
    dry_run: bool,
    headful: bool,
    agents: list[str] | None,
) -> int | None:
    _validate_goal_options(max_turns=max_turns, context_chars=context_chars, timeout=timeout)
    loop_config = _load_or_exit(config_path, agents)
    loop_config = _override_loop_config(
        loop_config,
        context_chars=context_chars,
        completion_token=completion_token,
    )
    prompt = _read_objective(objective, from_file, workspace)
    loop = FriendshipLoop(
        config=loop_config,
        workspace=workspace,
        prompt=prompt,
        iterations=max_turns,
        transcript_path=transcript,
        continue_on_error=continue_on_error,
        timeout_seconds=timeout,
        stream=not dry_run,
        headful=headful,
    )

    if dry_run:
        table = Table(title="Planned Headful Goal Loop" if headful else "Planned Goal Loop")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Agent", style="bold")
        table.add_column("Command", overflow="fold")
        for iteration, agent, argv in loop.dry_run():
            table.add_row(f"{iteration:03d}", agent.name, shlex.join(argv))
        console.print(table)
        console.print(f"[dim]transcript:[/dim] {loop.transcript_path}")
        return None

    try:
        result = loop.run()
    except MissingExecutableError as exc:
        console.print(f"[red]pof:[/red] {exc}")
        return 127
    except AgentCommandError as exc:
        console.print(f"[red]pof:[/red] {exc}")
        console.print(f"[dim]transcript:[/dim] {exc.transcript_path}")
        return exc.record.returncode or 1
    except HarnessError as exc:
        console.print(f"[red]pof:[/red] {exc}")
        return 1

    console.print(f"[dim]transcript:[/dim] {result.transcript_path}")
    _print_workspace_effects(result.workspace_changes)
    if result.completed:
        console.print(COMPLETION_BANNER, style="green", markup=False)
        console.print(f"[green]complete[/green] after {result.iterations_run} turn(s)")
        return None

    console.print(f"[yellow]reached max turns[/yellow] ({result.iterations_run})")
    return 1


def _print_workspace_effects(changes: WorkspaceChanges | None) -> None:
    console.print("[bold]workspace effects[/bold]")
    if changes is None:
        console.print("[dim]not available; workspace is not a git worktree[/dim]")
        return
    if not changes.changed:
        console.print("[yellow]no git changes detected; completion was agreement-only[/yellow]")
        return

    for message in format_workspace_changes(changes):
        console.print(message)


def _load_or_exit(config: Path, agents: list[str] | None) -> LoopConfig:
    try:
        return load_config(config, agent_order=agents)
    except ConfigError as exc:
        console.print(f"[red]configuration error:[/red] {exc}")
        raise typer.Exit(2)


def _override_loop_config(
    config: LoopConfig,
    *,
    context_chars: int | None,
    completion_token: str | None,
) -> LoopConfig:
    if context_chars is None and completion_token is None:
        return config
    return LoopConfig(
        agents=config.agents,
        completion_token=completion_token or config.completion_token,
        context_chars=context_chars if context_chars is not None else config.context_chars,
    )


def _validate_goal_options(
    *,
    max_turns: int,
    context_chars: int | None,
    timeout: float | None,
) -> None:
    if max_turns < 1:
        console.print("[red]configuration error:[/red] --max-turns must be at least 1")
        raise typer.Exit(2)
    if context_chars is not None and context_chars < 1:
        console.print("[red]configuration error:[/red] --context-chars must be at least 1")
        raise typer.Exit(2)
    if timeout is not None and timeout <= 0:
        console.print("[red]configuration error:[/red] --timeout must be greater than 0")
        raise typer.Exit(2)


def _read_objective(objective: str | Sequence[str] | None, from_file: Path, workspace: Path) -> str:
    if isinstance(objective, str):
        text = objective
    elif objective is not None:
        text = " ".join(objective)
    else:
        path = from_file if from_file.is_absolute() else workspace / from_file
        if not path.exists():
            console.print(f"[red]configuration error:[/red] Goal file not found: {path}")
            raise typer.Exit(2)
        text = path.read_text(encoding="utf-8")

    if not text.strip():
        console.print("[red]configuration error:[/red] Goal cannot be empty.")
        raise typer.Exit(2)
    return text


def main() -> None:
    app()


if __name__ == "__main__":
    main()
