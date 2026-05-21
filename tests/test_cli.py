from __future__ import annotations

import unittest

from typer.testing import CliRunner

from pof.cli import app


runner = CliRunner()


class CliTests(unittest.TestCase):
    def test_help_exposes_goal_and_doctor_not_run(self) -> None:
        result = runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("goal", result.output)
        self.assertIn("doctor", result.output)
        self.assertNotIn("run    ", result.output)
        self.assertNotIn("doctor-cmd", result.output)

    def test_goal_dry_run_accepts_objective_argument(self) -> None:
        with runner.isolated_filesystem():
            result = runner.invoke(app, ["goal", "Smoke goal", "--max-turns", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)
        self.assertIn("claude", result.output)
        self.assertIn("codex", result.output)
        self.assertIn("@google/gemini-cli", result.output)

    def test_goal_dry_run_accepts_unquoted_objective_words(self) -> None:
        with runner.isolated_filesystem():
            result = runner.invoke(app, ["goal", "Smoke", "goal", "--max-turns", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)

    def test_root_invocation_defaults_to_goal(self) -> None:
        with runner.isolated_filesystem():
            result = runner.invoke(app, ["Smoke", "goal", "--max-turns", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)
        self.assertIn("claude", result.output)

    def test_root_invocation_accepts_goal_options_without_objective(self) -> None:
        with runner.isolated_filesystem():
            with open("PROMPT.md", "w", encoding="utf-8") as prompt_file:
                prompt_file.write("Smoke goal")

            result = runner.invoke(app, ["--max-turns", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)

    def test_goal_validates_max_turns(self) -> None:
        result = runner.invoke(app, ["goal", "Smoke goal", "--max-turns", "0", "--dry-run"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("--max-turns must be at least 1", result.output)

    def test_iterations_alias_still_works(self) -> None:
        with runner.isolated_filesystem():
            result = runner.invoke(app, ["goal", "Smoke goal", "--iterations", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)


if __name__ == "__main__":
    unittest.main()
