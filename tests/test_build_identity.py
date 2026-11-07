from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sysinfo


class BuildIdentityTests(unittest.TestCase):
    def render(self, identity=None, env=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "build-identity.json"
            if identity is not None:
                path.write_text(json.dumps(identity))
            with patch.object(sysinfo, "BUILD_IDENTITY_PATH", path), patch.dict(os.environ, env or {}, clear=True):
                block = sysinfo._build_firmware_block({
                    "ID": "alpine", "VERSION_ID": "3.23.0", "PRETTY_NAME": "Alpine Linux v3.23",
                })
                return json.loads(json.dumps({"firmware": block}))["firmware"]

    def test_branch_commit_and_os_separation_json(self):
        block = self.render({"ref_type": "branch", "ref": "master", "commit": "123456789abcdef"})
        self.assertEqual(block["name"], "dockernode")
        self.assertEqual(block["version"], "dockernode / master / 1234567")
        self.assertEqual(block["DISTRIB_DESCRIPTION"], "Alpine Linux v3.23")
        self.assertEqual(block["DISTRIB_RELEASE"], "3.23.0")
        self.assertNotIn("Alpine", block["version"])

    def test_semver_tag(self):
        for tag in ("v1.2.3", "1.2.3"):
            with self.subTest(tag=tag):
                self.assertEqual(self.render({"ref_type": "tag", "ref": tag, "version": tag})["version"], "dockernode / 1.2.3")

    def test_missing_local_identity_is_development(self):
        for identity in (None, {}, {"ref_type": "branch", "ref": "master"}, []):
            with self.subTest(identity=identity):
                self.assertEqual(self.render(identity)["version"], "dockernode / development")

    def test_runtime_build_variables_cannot_replace_image_identity(self):
        block = self.render({"ref_type": "branch", "ref": "master", "commit": "abcdef012345"},
                            {"BUILD_REF": "stale", "BUILD_COMMIT": "0000000", "SYSINFO_GIT_DDMESH_BRANCH": "stale"})
        self.assertEqual(block["version"], "dockernode / master / abcdef0")

    def test_legacy_explicit_override(self):
        self.assertEqual(self.render(env={"SYSINFO_FIRMWARE_VERSION": "custom"})["version"], "custom")


if __name__ == "__main__":
    unittest.main()
