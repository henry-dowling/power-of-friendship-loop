# Power of Friendship Loop

`pof` is a variation of /goal (provided by most modern harnesses). it cycles through codex, claude code, and gemini as it loops and only terminates when all three coding agents agree that the task is done.

Each turn gets the original task, the current loop state, recent agent output,
and a completion contract. When an agent prints `<promise>COMPLETE</promise>`,
the loop stops successfully.

## Quick Start

```sh
python -m pof init
$EDITOR PROMPT.md
python -m pof doctor
python -m pof goal --iterations 9
```

The loop writes JSONL transcripts under `.pof/runs/` so later agents and humans
can inspect what happened.

## Commands

```sh
python -m pof init
python -m pof doctor
python -m pof goal --from PROMPT.md --iterations 9
python -m pof goal "Fix the bug and add a regression test" --iterations 6
python -m pof goal --dry-run --iterations 6
```

Install locally if you want the `pof` command on PATH:

```sh
python -m pip install -e .
pof goal "Build the thing and verify it" --iterations 9
```

The primary command is `pof goal ...`, matching Codex goal ergonomics: the goal
text is the main argument, and the harness keeps cycling agents until one prints
the completion token or the turn budget runs out. If you omit the argument,
`pof goal` reads `PROMPT.md` by default.

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
- `{iteration}`: current iteration number
- `{agent}`: current agent name

## Runtime Behavior

- One agent runs per iteration.
- The selected agent is `agents[(iteration - 1) % len(agents)]`.
- Non-zero exits stop the run by default.
- Use `--continue-on-error` to keep rotating after a failed turn.
- Use `--agent` repeatedly to override the configured order for a goal.
- Use `--dry-run` to inspect the planned rotation without invoking any agents.

Gemini CLI headless mode uses `-p/--prompt` according to the official Gemini CLI
documentation. The default config runs it through `npx` so a global `gemini`
binary is not required.
