<img width="1216" height="730" alt="CleanShot 2026-05-21 at 01 48 11@2x" src="https://github.com/user-attachments/assets/32f19c13-01ff-4647-8e25-64a26ee1ec1f" />

# Power of Friendship Loop

`pof` is a variation of /goal (provided by most modern harnesses). it cycles through codex, claude code, and gemini as it loops and only completes when all configured coding agents agree that the task is done.

From a shell, run `pof [whatever you would put into /goal normally]` and it'll work. For Codex, this repo also ships a `/pof` command definition that delegates to the same CLI once the plugin is installed.


Each turn gets the original task, the current loop state, recent agent output,
and a completion contract. When every configured agent prints
`<promise>COMPLETE</promise>` on consecutive turns, the loop stops successfully.

## Quick Start

```sh
python -m pof init
$EDITOR PROMPT.md
python -m pof doctor
python -m pof goal
```

The loop writes JSONL transcripts under `.pof/runs/` so later agents and humans
can inspect what happened.

## Commands

```sh
python -m pof init
python -m pof doctor
python -m pof --from PROMPT.md
python -m pof "Fix the bug and add a regression test"
python -m pof "Fix the bug and add a regression test" --dry-run
python -m pof goal --from PROMPT.md
python -m pof goal "Fix the bug and add a regression test"
python -m pof goal --dry-run
```

Install locally if you want the `pof` command on PATH:

```sh
python -m pip install -e .
pof "Build the thing and verify it"
```

The primary command is `pof ...`, with `pof goal ...` kept as an explicit alias.
The goal text is the main argument, and the harness keeps cycling agents until
every configured agent prints the completion token in the current agreement
window. If you omit the argument, use `pof goal` or pass `--from PROMPT.md` to
read `PROMPT.md`.

## Codex Slash Command

The repo includes `.codex-plugin/plugin.json` and `commands/pof.md` so Codex can
expose `/pof` when the plugin is installed. The slash command verifies
`python -m pof` is available, installs this checkout in editable mode if needed,
and then runs the CLI with the provided arguments.

## Configuration

`pof init` writes a `pof.toml` with the default commands:

```toml
[loop]
agents = ["claude", "codex", "gemini"]
completion_token = "<promise>COMPLETE</promise>"
context_chars = 12000

[agents.claude]
command = ["claude", "-p", "{prompt}", "--output-format", "text", "--dangerously-skip-permissions"]

[agents.codex]
command = ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "{prompt}"]

[agents.gemini]
command = ["npx", "-y", "@google/gemini-cli", "-p", "{prompt}"]
```

You can override commands with environment variables:

```sh
POF_GEMINI_CMD='gemini -p {prompt}' python -m pof doctor
```

Command templates support these placeholders:

- `{prompt}`: the full turn prompt as one argument
- `{prompt_file}`: path to a temporary prompt file for the turn
- `{workspace}`: absolute workspace path
- `{transcript}`: absolute JSONL transcript path
- `{iteration}`: current turn number
- `{agent}`: current agent name

## Runtime Behavior

- One agent runs per turn.
- The selected agent is `agents[(turn - 1) % len(agents)]`.
- Completion requires every configured agent to print the completion token on
  consecutive turns. If an agent does not print the token, the agreement window
  resets.
- `--max-turns` is a safety cap for runaway loops. The default is 30 turns.
- `--iterations` and `-n` remain compatibility aliases for `--max-turns`.
- Non-zero exits stop the run by default.
- Use `--continue-on-error` to keep rotating after a failed turn.
- Use `--agent` repeatedly to override the configured order for a goal.
- Use `--dry-run` to inspect the planned rotation without invoking any agents.

Gemini CLI headless mode uses `-p/--prompt` according to the official Gemini CLI
documentation. The default config runs it through `npx` so a global `gemini`
binary is not required.
