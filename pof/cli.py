from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from .config import ConfigError, load_config, write_default_files
from .harness import AgentCommandError, FriendshipLoop, HarnessError, MissingExecutableError, doctor


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "run":
            return cmd_run(args)
    except ConfigError as exc:
        print(f"pof: configuration error: {exc}", file=sys.stderr)
        return 2
    except MissingExecutableError as exc:
        print(f"pof: {exc}", file=sys.stderr)
        return 127
    except AgentCommandError as exc:
        print(f"pof: {exc}", file=sys.stderr)
        print(f"pof: transcript: {exc.transcript_path}", file=sys.stderr)
        return exc.record.returncode or 1
    except HarnessError as exc:
        print(f"pof: {exc}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pof",
        description="Run a power-of-friendship loop across Claude, Codex, and Gemini.",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="write starter pof.toml and PROMPT.md")
    init_parser.add_argument("--force", action="store_true", help="overwrite existing files")
    init_parser.add_argument("--directory", type=Path, default=Path.cwd(), help="directory to initialize")

    doctor_parser = subparsers.add_parser("doctor", help="check configured agent executables")
    add_config_args(doctor_parser)
    doctor_parser.add_argument(
        "--agent",
        action="append",
        dest="agents",
        help="override the configured rotation; repeat for multiple agents",
    )

    run_parser = subparsers.add_parser("run", help="run the round-robin harness")
    add_config_args(run_parser)
    run_parser.add_argument("-n", "--iterations", type=int, default=9, help="maximum turns to run")
    run_parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="workspace to run agents in")
    run_parser.add_argument("--prompt-file", type=Path, default=Path("PROMPT.md"), help="task prompt file")
    run_parser.add_argument("--prompt", help="task prompt text; overrides --prompt-file")
    run_parser.add_argument("--transcript", type=Path, help="explicit transcript JSONL path")
    run_parser.add_argument("--context-chars", type=int, help="recent transcript characters passed to agents")
    run_parser.add_argument("--completion-token", help="token that marks successful completion")
    run_parser.add_argument("--continue-on-error", action="store_true", help="continue after non-zero agent exits")
    run_parser.add_argument("--timeout", type=float, help="per-agent timeout in seconds")
    run_parser.add_argument("--dry-run", action="store_true", help="show planned turns without running agents")
    run_parser.add_argument(
        "--agent",
        action="append",
        dest="agents",
        help="override the configured rotation; repeat for multiple agents",
    )

    return parser


def add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("pof.toml"), help="configuration file")


def cmd_init(args: argparse.Namespace) -> int:
    written = write_default_files(args.directory, force=args.force)
    if written:
        for path in written:
            print(f"wrote {path}")
    else:
        print("pof.toml and PROMPT.md already exist; use --force to overwrite")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config, agent_order=args.agents)
    results = doctor(config)
    width = max(len(result.agent) for result in results)
    ok = True
    for result in results:
        status = "ok" if result.ok else "missing"
        resolved = result.resolved or result.executable
        print(f"{result.agent:<{width}}  {status:<7}  {resolved}")
        ok = ok and result.ok
    return 0 if ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    if args.iterations < 1:
        raise ConfigError("--iterations must be at least 1")
    if args.context_chars is not None and args.context_chars < 1:
        raise ConfigError("--context-chars must be at least 1")
    if args.timeout is not None and args.timeout <= 0:
        raise ConfigError("--timeout must be greater than 0")

    config = load_config(args.config, agent_order=args.agents)
    if args.context_chars is not None or args.completion_token is not None:
        config = type(config)(
            agents=config.agents,
            completion_token=args.completion_token or config.completion_token,
            context_chars=args.context_chars if args.context_chars is not None else config.context_chars,
        )

    prompt = read_prompt(args.prompt, args.prompt_file, args.workspace)
    loop = FriendshipLoop(
        config=config,
        workspace=args.workspace,
        prompt=prompt,
        iterations=args.iterations,
        transcript_path=args.transcript,
        continue_on_error=args.continue_on_error,
        timeout_seconds=args.timeout,
        stream=not args.dry_run,
    )

    if args.dry_run:
        for iteration, agent, argv in loop.dry_run():
            print(f"{iteration:03d} {agent.name}: {shlex.join(argv)}")
        print(f"transcript: {loop.transcript_path}")
        return 0

    result = loop.run()
    print(f"pof: transcript: {result.transcript_path}")
    if result.completed:
        print(f"pof: complete after {result.iterations_run} iteration(s)")
        return 0
    print(f"pof: reached max iterations ({result.iterations_run})")
    return 1


def read_prompt(prompt_text: str | None, prompt_file: Path, workspace: Path) -> str:
    if prompt_text is not None:
        text = prompt_text
    else:
        path = prompt_file if prompt_file.is_absolute() else workspace / prompt_file
        if not path.exists():
            raise ConfigError(f"Prompt file not found: {path}")
        text = path.read_text(encoding="utf-8")

    if not text.strip():
        raise ConfigError("Prompt cannot be empty.")
    return text
