from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import registrar


class GatewayConfigTests(unittest.TestCase):
    def config(self, value=None, defaults=None):
        env = {"NODE_REGISTRATION_MODE": "temporary", "BACKBONE_ENABLED": "0"}
        if value is not None:
            env["BMXD_ONLY_COMMUNITY_GATEWAYS"] = value
        return registrar.require_valid_registrar_config(defaults=defaults or {}, env=env)

    def test_community_only_defaults_true_and_blank_uses_default(self):
        self.assertIs(self.config()["bmxd"]["only_community_gw"], True)
        self.assertIs(self.config("")["bmxd"]["only_community_gw"], True)

    def test_switch_accepts_boolean_words_and_numeric_values(self):
        for value in ["true", "TRUE", "1", "yes", "on"]:
            with self.subTest(value=value):
                self.assertIs(self.config(value)["bmxd"]["only_community_gw"], True)
        for value in ["false", "FALSE", "0", "no", "off"]:
            with self.subTest(value=value):
                self.assertIs(self.config(value)["bmxd"]["only_community_gw"], False)

    def test_defaults_yaml_boolean_and_env_override(self):
        defaults = {"BMXD_ONLY_COMMUNITY_GATEWAYS": False}
        self.assertIs(self.config(defaults=defaults)["bmxd"]["only_community_gw"], False)
        self.assertIs(self.config("true", defaults)["bmxd"]["only_community_gw"], True)

    def test_invalid_values_fail_configuration(self):
        for value in ["2", "maybe", "-1"]:
            with self.subTest(value=value), self.assertRaises(SystemExit):
                self.config(value)

    def test_runtime_renders_numeric_bmxd_flag(self):
        for value, expected in [("true", "1"), ("false", "0")]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "bmxd.env"
                with patch.object(registrar, "BMXD_ENV_FILE", path):
                    registrar.render_bmxd_runtime(self.config(value), 913, [])
                self.assertIn(f'BMXD_ONLY_COMMUNITY_GW="{expected}"', path.read_text())
                self.assertNotIn('"True"', path.read_text())
                self.assertNotIn('"False"', path.read_text())


if __name__ == "__main__":
    unittest.main()
