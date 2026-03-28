from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import node_config


class NodeConfigTests(unittest.TestCase):
    def test_load_defaults_uses_runtime_environment_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            defaults_file = Path(temp_dir) / "defaults.yaml"
            defaults_file.write_text("FOO: bar\n", encoding="utf-8")

            with patch.dict(os.environ, {"DEFAULTS_FILE": str(defaults_file)}, clear=False):
                self.assertEqual(node_config.load_defaults(), {"FOO": "bar"})

    def test_save_and_load_state_use_runtime_environment_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "node.yaml"
            state = {"registration": {"node_id": 52001}, "fastd": {"secret": "abc"}}

            with patch.dict(os.environ, {"NODE_YAML": str(state_file)}, clear=False):
                node_config.save_state(state)
                self.assertEqual(node_config.load_state(), state)

    def test_resolve_config_uses_blank_env_as_default_when_enabled(self) -> None:
        schema = (
            {
                "env": "BACKBONE_PEERS",
                "default_key": "BACKBONE_PEERS",
                "path": ("backbone", "peers"),
                "type": "str",
                "required": True,
                "allow_blank": False,
                "blank_env_uses_default": True,
            },
        )

        result = node_config.resolve_config(
            schema=schema,
            defaults={
                "BACKBONE_PEERS": "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
            },
            env={"BACKBONE_PEERS": ""},
            base_values={},
        )

        self.assertEqual(
            result.values["backbone"]["peers"],
            "fastd;vpn1.example.org:5002;0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        )
        self.assertEqual(result.errors, [])

    def test_require_valid_config_logs_warning_and_error(self) -> None:
        schema = (
            {
                "env": "WARN_ONLY",
                "path": ("warn",),
                "type": "str",
                "required": True,
                "required_level": "warning",
            },
            {
                "env": "HARD_ERROR",
                "path": ("error",),
                "type": "str",
                "required": True,
            },
        )
        messages: list[str] = []

        with self.assertRaises(SystemExit):
            node_config.require_valid_config(
                schema=schema,
                defaults={},
                env={},
                base_values={},
                logger=messages.append,
            )

        self.assertIn("config warning: WARN_ONLY: WARN_ONLY is required", messages)
        self.assertIn("config error: HARD_ERROR: HARD_ERROR is required", messages)


if __name__ == "__main__":
    unittest.main()
