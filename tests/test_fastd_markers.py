from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import backbone_runtime


class FastdMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / "status with spaces"
        self.status.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FASTD_STATUS_DIR": str(self.status),
            "FASTD_ENV_FILE": str(self.root / "missing.env"),
            "FASTD_NONPRIMARY_IP": "10.200.0.1",
            "PEER_KEY": "abcdef",
        }
        self.stub("ip", "exit 0\n")

    def stub(self, name: str, body: str) -> None:
        executable = self.bin / name
        executable.write_text("#!/bin/sh\nset -eu\n" + body)
        executable.chmod(0o755)

    def hook(self, action: str) -> None:
        subprocess.run(["sh", str(SCRIPTS_DIR / "fastd-backbone-cmd.sh"), action],
                       env=self.env, check=True, capture_output=True, text=True)

    def keys(self) -> set[str]:
        with patch.object(backbone_runtime, "FASTD_STATUS_DIR", self.status):
            return backbone_runtime.read_fastd_connected_keys()

    def test_up_and_down_clear_all_old_markers_and_allow_reconnect(self) -> None:
        for action in ("up", "down"):
            with self.subTest(action=action):
                (self.status / "oldpeer").touch()
                (self.status / ".hiddenpeer").touch()
                self.hook(action)
                self.assertEqual(self.keys(), set())
                self.hook("establish")
                self.assertEqual(self.keys(), {"abcdef"})
                self.hook("disestablish")
                self.assertEqual(self.keys(), set())

    def test_disestablish_only_removes_its_peer(self) -> None:
        (self.status / "anotherpeer").touch()
        self.hook("establish")
        self.hook("disestablish")
        self.assertEqual(self.keys(), {"anotherpeer"})

    def test_cleanup_preserves_directories_and_symlink_targets(self) -> None:
        nested = self.status / "directory"
        nested.mkdir()
        (nested / "keep").touch()
        outside = self.root / "outside"
        outside.write_text("keep")
        (self.status / "linked-peer").symlink_to(outside)
        self.hook("up")
        self.assertTrue((nested / "keep").exists())
        self.assertEqual(outside.read_text(), "keep")
        self.assertFalse((self.status / "linked-peer").is_symlink())

    def test_startup_clears_crashed_daemon_markers_before_exec(self) -> None:
        (self.status / "oldpeer").touch()
        (self.status / ".hiddenpeer").touch()
        config = self.root / "fastd.conf"
        config.touch()
        self.env["FASTD_CONFIG"] = str(config)
        self.stub("fastd", 'test "$1" = --config\ntest "$2" = "$FASTD_CONFIG"\n'
                  'test -z "$(ls -A "$FASTD_STATUS_DIR")"\n')
        subprocess.run(["sh", str(SCRIPTS_DIR / "runit/fastd/run")],
                       env=self.env, check=True, capture_output=True, text=True)
        self.assertEqual(self.keys(), set())

    def test_startup_clears_markers_even_while_waiting_for_config(self) -> None:
        (self.status / "oldpeer").touch()
        self.env["FASTD_CONFIG"] = str(self.root / "missing.conf")
        self.stub("sleep", 'test ! -e "$FASTD_STATUS_DIR/oldpeer"\nexit 42\n')
        result = subprocess.run(["sh", str(SCRIPTS_DIR / "runit/fastd/run")],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(self.keys(), set())

    def test_empty_marker_directory_is_safe(self) -> None:
        self.hook("up")
        self.hook("down")
        self.assertEqual(self.keys(), set())


if __name__ == "__main__":
    unittest.main()
