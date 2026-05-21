from __future__ import annotations

import stat
import shutil
import subprocess
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

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_result_reports_uncommitted_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            agent_path = root / "friend-agent"
            agent_path.write_text(
                "#!/bin/sh\n"
                "printf 'updated\\n' > README.md\n"
                "printf 'done <promise>COMPLETE</promise>\\n'\n",
                encoding="utf-8",
            )
            agent_path.chmod(agent_path.stat().st_mode | stat.S_IXUSR)
            config = LoopConfig(agents=[AgentConfig(name="friend", command=[str(agent_path), "{prompt}"])])

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Fix the README.",
                iterations=1,
                stream=False,
            ).run()

            self.assertTrue(result.completed)
            self.assertIsNotNone(result.workspace_changes)
            assert result.workspace_changes is not None
            self.assertEqual(result.workspace_changes.commits, ())
            self.assertIn(" M README.md", result.workspace_changes.status_lines)

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_result_reports_committed_workspace_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            init_git_repo(root)
            agent_path = root / "friend-agent"
            agent_path.write_text(
                "#!/bin/sh\n"
                "printf 'updated\\n' > README.md\n"
                "git add README.md\n"
                "git commit -m 'update readme' >/dev/null\n"
                "printf 'done <promise>COMPLETE</promise>\\n'\n",
                encoding="utf-8",
            )
            agent_path.chmod(agent_path.stat().st_mode | stat.S_IXUSR)
            config = LoopConfig(agents=[AgentConfig(name="friend", command=[str(agent_path), "{prompt}"])])

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Fix the README.",
                iterations=1,
                stream=False,
            ).run()

            self.assertTrue(result.completed)
            self.assertIsNotNone(result.workspace_changes)
            assert result.workspace_changes is not None
            self.assertTrue(any("update readme" in commit for commit in result.workspace_changes.commits))
            self.assertEqual(result.workspace_changes.status_lines, ())

    def test_doctor_reports_absolute_fake_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = fake_agent(root, "claude", root / "calls.txt")
            results = doctor(LoopConfig(agents=[agent]))
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0].ok)
            self.assertEqual(results[0].agent, "claude")

    @unittest.skipIf(shutil.which("tmux") is None, "tmux is not installed")
    def test_headful_command_auto_exits_after_turn_done_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_path = root / "headful-agent"
            agent_path.write_text(
                "#!/usr/bin/env python3\n"
                "import re\n"
                "import sys\n"
                "import time\n"
                "prompt = sys.argv[1]\n"
                "match = re.search(r'POF_TURN_DONE_[A-Za-z0-9_]+', prompt)\n"
                "print('done <promise>COMPLETE</promise>', flush=True)\n"
                "print(match.group(0), flush=True)\n"
                "while True:\n"
                "    time.sleep(1)\n",
                encoding="utf-8",
            )
            agent_path.chmod(agent_path.stat().st_mode | stat.S_IXUSR)
            config = LoopConfig(
                agents=[
                    AgentConfig(
                        name="friend",
                        command=[str(agent_path), "{prompt}"],
                        headful_command=[str(agent_path), "{prompt}"],
                    )
                ]
            )

            result = FriendshipLoop(
                config=config,
                workspace=root,
                prompt="Do the thing.",
                iterations=1,
                timeout_seconds=8,
                stream=False,
                headful=True,
            ).run()

            self.assertTrue(result.completed)
            self.assertEqual(result.records[0].returncode, 0)
            self.assertIn("POF_TURN_DONE", result.records[0].output)


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


def init_git_repo(root: Path) -> None:
    run_git(root, "init")
    run_git(root, "config", "user.email", "friend@example.com")
    run_git(root, "config", "user.name", "Friend")
    (root / "README.md").write_text("original\n", encoding="utf-8")
    run_git(root, "add", "README.md")
    run_git(root, "commit", "-m", "initial")


def run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    unittest.main()
