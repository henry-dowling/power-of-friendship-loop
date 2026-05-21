from __future__ import annotations

import os
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_COMPLETION_TOKEN = "<promise>COMPLETE</promise>"
DEFAULT_CONTEXT_CHARS = 12000
DEFAULT_AGENT_ORDER = ["claude", "codex", "gemini"]

DEFAULT_COMMANDS: dict[str, list[str]] = {
    "claude": [
        "claude",
        "-p",
        "{prompt}",
        "--output-format",
        "text",
        "--dangerously-skip-permissions",
    ],
    "codex": [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "{prompt}",
    ],
    "gemini": ["npx", "-y", "@google/gemini-cli", "-p", "{prompt}"],
}


DEFAULT_TOML = """[loop]
agents = ["claude", "codex", "gemini"]
completion_token = "<promise>COMPLETE</promise>"
context_chars = 12000

[agents.claude]
command = ["claude", "-p", "{prompt}", "--output-format", "text", "--dangerously-skip-permissions"]

[agents.codex]
command = ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "{prompt}"]

[agents.gemini]
command = ["npx", "-y", "@google/gemini-cli", "-p", "{prompt}"]
"""


DEFAULT_PROMPT = """Describe the task for the power-of-friendship loop.

The loop will rotate through Claude, Codex, and Gemini. Each agent should inspect
the current workspace, make concrete progress, and print
<promise>COMPLETE</promise> only when the task is genuinely finished.
"""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    command: list[str]


@dataclass(frozen=True)
class LoopConfig:
    agents: list[AgentConfig]
    completion_token: str = DEFAULT_COMPLETION_TOKEN
    context_chars: int = DEFAULT_CONTEXT_CHARS


class ConfigError(ValueError):
    pass


def load_config(path: Path | None = None, agent_order: list[str] | None = None) -> LoopConfig:
    raw: dict[str, Any] = {}
    if path and path.exists():
        with path.open("rb") as config_file:
            raw = tomllib.load(config_file)

    loop_raw = _mapping(raw.get("loop", {}), "loop")
    agents_raw = _mapping(raw.get("agents", {}), "agents")

    configured_order = loop_raw.get("agents", DEFAULT_AGENT_ORDER)
    if agent_order:
        names = agent_order
    else:
        names = _string_list(configured_order, "loop.agents")

    completion_token = str(loop_raw.get("completion_token", DEFAULT_COMPLETION_TOKEN))
    context_chars = _positive_int(loop_raw.get("context_chars", DEFAULT_CONTEXT_CHARS), "loop.context_chars")

    agents: list[AgentConfig] = []
    for name in names:
        agent_raw = _mapping(agents_raw.get(name, {}), f"agents.{name}")
        command = _command_for(name, agent_raw)
        agents.append(AgentConfig(name=name, command=command))

    if not agents:
        raise ConfigError("At least one agent must be configured.")

    return LoopConfig(agents=agents, completion_token=completion_token, context_chars=context_chars)


def write_default_files(directory: Path, force: bool = False) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    files = {
        directory / "pof.toml": DEFAULT_TOML,
        directory / "PROMPT.md": DEFAULT_PROMPT,
    }
    for path, content in files.items():
        if path.exists() and not force:
            continue
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


def _command_for(name: str, agent_raw: dict[str, Any]) -> list[str]:
    env_name = f"POF_{name.upper().replace('-', '_')}_CMD"
    if env_name in os.environ:
        return shlex.split(os.environ[env_name])

    raw_command = agent_raw.get("command")
    if raw_command is None:
        if name not in DEFAULT_COMMANDS:
            raise ConfigError(f"No command configured for agent {name!r}.")
        return list(DEFAULT_COMMANDS[name])

    if isinstance(raw_command, str):
        command = shlex.split(raw_command)
    elif isinstance(raw_command, list) and all(isinstance(part, str) for part in raw_command):
        command = list(raw_command)
    else:
        raise ConfigError(f"agents.{name}.command must be a string or list of strings.")

    if not command:
        raise ConfigError(f"agents.{name}.command cannot be empty.")
    return command


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    raise ConfigError(f"{name} must be a table.")


def _string_list(value: Any, name: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(f"{name} must be a list of strings.")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, int) and value > 0:
        return value
    raise ConfigError(f"{name} must be a positive integer.")
