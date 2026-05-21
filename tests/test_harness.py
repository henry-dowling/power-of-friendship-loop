from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from pof.config import AgentConfig, LoopConfig
from pof.harness import FriendshipLoop, doctor


class FriendshipLoopTests(unittest.TestCase):
    def test_cycles_agents_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.txt"
            config = LoopConfig(
                agents=[
                    fake_agent(root, "claude", log),
                    fake_agent(root, "codex", log),
                    fake_agent(root, "gemini", log),
                ],
                context_chars=2000,
            )

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Do the thing.",
                iterations=4,
                stream=False,
            ).run()

            self.assertFalse(result.completed)
            self.assertEqual([record.agent for record in result.records], ["claude", "codex", "gemini", "claude"])
            self.assertEqual(log.read_text().splitlines(), ["claude", "codex", "gemini", "claude"])
            self.assertTrue(result.transcript_path.exists())

    def test_completion_requires_every_agent_to_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.txt"
            config = LoopConfig(
                agents=[
                    fake_agent(root, "claude", log),
                    fake_agent(root, "codex", log, complete=True),
                    fake_agent(root, "gemini", log, complete=True),
                ]
            )

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Do the thing.",
                iterations=3,
                stream=False,
            ).run()

            self.assertFalse(result.completed)
            self.assertEqual(result.iterations_run, 3)
            self.assertEqual([record.agent for record in result.records], ["claude", "codex", "gemini"])

    def test_stops_after_all_agents_agree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "calls.txt"
            config = LoopConfig(
                agents=[
                    fake_agent(root, "claude", log, complete=True),
                    fake_agent(root, "codex", log, complete=True),
                    fake_agent(root, "gemini", log, complete=True),
                ]
            )

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Do the thing.",
                iterations=6,
                stream=False,
            ).run()

            self.assertTrue(result.completed)
            self.assertEqual(result.iterations_run, 3)
            self.assertEqual([record.agent for record in result.records], ["claude", "codex", "gemini"])
            self.assertTrue(all("<promise>COMPLETE</promise>" in record.output for record in result.records))

    def test_doctor_reports_absolute_fake_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = fake_agent(root, "claude", root / "calls.txt")
            results = doctor(LoopConfig(agents=[agent]))
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].ok)
            self.assertEqual(results[0].agent, "claude")


def fake_agent(root: Path, name: str, log: Path, complete: bool = False) -> AgentConfig:
    path = root / name
    output = f"agent:{name}"
    if complete:
        output += " <promise>COMPLETE</promise>"
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' {shell_quote(name)} >> {shell_quote(str(log))}\n"
        f"printf '%s\\n' {shell_quote(output)}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return AgentConfig(name=name, command=[str(path), "{prompt}"])


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    unittest.main()
