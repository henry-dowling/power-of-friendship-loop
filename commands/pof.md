---
description: Run a goal through the Power of Friendship loop. Pass the same text and options you would give /goal.
---

# /pof

Run the Power of Friendship loop in the current workspace, delegating to the local `pof` Python CLI.

## Arguments

- `$ARGUMENTS`: goal text and options. If omitted, read `PROMPT.md`.

## Workflow

1. Confirm the CLI is available with `python -m pof --help`.
2. If the CLI is unavailable and the current workspace is this `pof` checkout, run `python -m pip install -e .`, then retry the help check.
3. If `$ARGUMENTS` is empty, run `python -m pof goal`.
4. Otherwise, run `python -m pof $ARGUMENTS`. Preserve supported flags such as `--max-turns`, `--from`, `--agent`, and `--dry-run`. Treat `--iterations` as a compatibility alias for `--max-turns`. For plain natural-language goal text, pass it as one shell-quoted argument.
5. Report whether the loop completed, hit the max-turns safety cap, or failed, and include the transcript path when one is printed.
