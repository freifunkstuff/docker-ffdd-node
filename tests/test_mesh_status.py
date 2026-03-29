from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
MODULE_PATH = SCRIPTS_DIR / "mesh-status.py"
SPEC = importlib.util.spec_from_file_location("mesh_status", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
mesh_status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mesh_status)


class MeshStatusTests(unittest.TestCase):
    def test_read_links_parses_unique_originators_per_interface(self) -> None:
        raw_output = "\n".join(
            [
                "10.1.1.2 tbb_fastd 10.200.200.16 255 255 255",
                "10.1.1.2 tbb_fastd 10.200.200.16 255 255 255",
                "10.1.1.3 vlan42 10.200.200.17 200 190 180",
            ]
        )

        with patch.object(
            mesh_status.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0, "stdout": raw_output})(),
        ):
            links = mesh_status.read_links()

        self.assertEqual(
            links,
            [
                {"originator": "10.200.200.16", "neighbor": "10.1.1.2", "interface": "tbb_fastd"},
                {"originator": "10.200.200.17", "neighbor": "10.1.1.3", "interface": "vlan42"},
            ],
        )

    def test_read_selected_gateway_returns_selected_originator(self) -> None:
        raw_output = "\n".join(
            [
                "  10.200.200.11 10.201.200.11 128, 1, 96MBit",
                "> 10.200.200.16 10.201.200.16 255, 1, 100MBit",
            ]
        )

        with patch.object(
            mesh_status.subprocess,
            "run",
            return_value=type("Result", (), {"returncode": 0, "stdout": raw_output})(),
        ):
            selected_gateway = mesh_status.read_selected_gateway()

        self.assertEqual(selected_gateway, "10.200.200.16")

    def test_build_status_payload_marks_mesh_stable_when_three_targets_are_reachable(self) -> None:
        payload = mesh_status.build_status_payload(
            selected_gateway="10.200.200.16",
            gateway_connected=True,
            link_targets=[
                {"originator": "10.200.200.11", "neighbor": "10.201.200.11", "interface": "tbb_fastd"},
                {"originator": "10.200.200.16", "neighbor": "10.201.200.16", "interface": "tbb_fastd"},
                {"originator": "10.200.200.21", "neighbor": "10.201.200.21", "interface": "tbb_fastd"},
            ],
            reachable_targets={"10.200.200.11", "10.200.200.16", "10.200.200.21"},
            connected_duration=30,
        )

        self.assertTrue(payload["mesh"]["connected"])
        self.assertTrue(payload["mesh"]["stable"])
        self.assertTrue(payload["gateway"]["connected"])

    def test_build_status_payload_keeps_mesh_unstable_before_stable_after(self) -> None:
        payload = mesh_status.build_status_payload(
            selected_gateway="",
            gateway_connected=False,
            link_targets=[
                {"originator": "10.200.200.11", "neighbor": "10.201.200.11", "interface": "tbb_fastd"},
                {"originator": "10.200.200.16", "neighbor": "10.201.200.16", "interface": "tbb_fastd"},
                {"originator": "10.200.200.21", "neighbor": "10.201.200.21", "interface": "tbb_fastd"},
            ],
            reachable_targets={"10.200.200.11", "10.200.200.16", "10.200.200.21"},
            connected_duration=29,
        )

        self.assertTrue(payload["mesh"]["connected"])
        self.assertFalse(payload["mesh"]["stable"])
        self.assertFalse(payload["gateway"]["connected"])

    def test_build_status_payload_allows_stable_without_gateway(self) -> None:
        payload = mesh_status.build_status_payload(
            selected_gateway="",
            gateway_connected=False,
            link_targets=[
                {"originator": "10.200.200.11", "neighbor": "10.201.200.11", "interface": "tbb_fastd"},
            ],
            reachable_targets={"10.200.200.11"},
            connected_duration=30,
        )

        self.assertTrue(payload["mesh"]["connected"])
        self.assertTrue(payload["mesh"]["stable"])
        self.assertFalse(payload["gateway"]["connected"])

    def test_select_link_targets_limits_result_count(self) -> None:
        links = [
            {"originator": "10.200.200.11", "neighbor": "10.201.200.11", "interface": "a"},
            {"originator": "10.200.200.16", "neighbor": "10.201.200.16", "interface": "b"},
            {"originator": "10.200.200.21", "neighbor": "10.201.200.21", "interface": "c"},
            {"originator": "10.200.200.68", "neighbor": "10.201.200.68", "interface": "d"},
        ]

        self.assertEqual(mesh_status.select_link_targets(links, 3), links[:3])


if __name__ == "__main__":
    unittest.main()