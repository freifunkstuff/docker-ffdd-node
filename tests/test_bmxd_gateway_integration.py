from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys


class BmxdGatewayIntegrationTests(unittest.TestCase):
    def _run_gateway(self, action: str, stat_file: Path) -> subprocess.CompletedProcess[str]:
        repo_root = Path(__file__).resolve().parents[1]
        runner = repo_root / "scripts" / "run_gateway_script.py"
        script = Path(os.environ.get("BMXD_GATEWAY_SCRIPT_UNDER_TEST", str(repo_root / "scripts" / "bmxd-gateway.py")))
        env = os.environ.copy()
        env["BMXD_GATEWAY_USAGE_FILE"] = str(stat_file)
        command = [sys.executable, str(runner), str(script)]
        if action:
            command.append(action)
        return subprocess.run(command, check=False, capture_output=True, text=True, env=env)

    def test_empty_action_logs_and_does_not_create_usage_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stat_file = Path(temp_dir) / "gateway_usage"

            result = self._run_gateway("", stat_file)

            self.assertEqual(result.returncode, 0)
            self.assertIn("Called without arguments", result.stderr)
            self.assertFalse(stat_file.exists())

    def test_gateway_action_is_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stat_file = Path(temp_dir) / "gateway_usage"

            first = self._run_gateway("gateway", stat_file)
            second = self._run_gateway("gateway", stat_file)

            self.assertEqual(first.returncode, 0)
            self.assertEqual(second.returncode, 0)
            self.assertIn("Entering community gateway mode", first.stderr)
            self.assertEqual(stat_file.read_text(encoding="utf-8"), "gateway:2\n")

    def test_custom_action_is_logged_and_counted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stat_file = Path(temp_dir) / "gateway_usage"
            action = "51020"

            result = self._run_gateway(action, stat_file)

            self.assertEqual(result.returncode, 0)
            self.assertIn(f"Selected gateway node {action}", result.stderr)
            self.assertEqual(stat_file.read_text(encoding="utf-8"), f"{action}:1\n")

    def test_multiple_actions_keep_individual_counters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            stat_file = Path(temp_dir) / "gateway_usage"

            self.assertEqual(self._run_gateway("init", stat_file).returncode, 0)
            self.assertEqual(self._run_gateway("del", stat_file).returncode, 0)
            self.assertEqual(self._run_gateway("init", stat_file).returncode, 0)

            content = stat_file.read_text(encoding="utf-8")
            self.assertIn("init:2\n", content)
            self.assertIn("del:1\n", content)


if __name__ == "__main__":
    unittest.main()