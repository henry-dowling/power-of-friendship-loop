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
        self.assertNotIn("run", result.output)
        self.assertNotIn("doctor-cmd", result.output)

    def test_goal_dry_run_accepts_objective_argument(self) -> None:
        with runner.isolated_filesystem():
            result = runner.invoke(app, ["goal", "Smoke goal", "--iterations", "4", "--dry-run"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Planned Goal Loop", result.output)
        self.assertIn("claude", result.output)
        self.assertIn("codex", result.output)
        self.assertIn("@google/gemini-cli", result.output)

    def test_goal_validates_iterations(self) -> None:
        result = runner.invoke(app, ["goal", "Smoke goal", "--iterations", "0", "--dry-run"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("--iterations must be at least 1", result.output)


if __name__ == "__main__":
    unittest.main()
