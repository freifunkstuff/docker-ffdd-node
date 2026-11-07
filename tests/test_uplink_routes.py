import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import uplink_routes as uplink


class FakeIP:
    def __init__(self):
        self.routes = [{"dst": "default", "gateway": "10.42.0.1", "dev": "eth0", "protocol": "dhcp", "metric": 100, "mtu": 1400}]
        self.calls = []
        self.bad_verification = False
        self.replaced = False
        self.fail_rollback = False

    def __call__(self, argv, **kwargs):
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        self.calls.append(argv)
        if "-j" in argv:
            if "addr" in argv:
                ip = "192.168.61.10" if argv[-1] == "lan0" else "10.42.0.4"
                data = [{"addr_info": [{"family": "inet", "local": ip, "prefixlen": 24}]}]
            else:
                data = copy.deepcopy(self.routes)
                if self.bad_verification and self.replaced:
                    data.append({"dst": "default", "dev": "unknown"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(data))
        operation = argv[3]
        dst = argv[4]
        route = {"dst": dst, "gateway": argv[6], "dev": argv[8]}
        if operation == "replace":
            if self.fail_rollback and self.replaced:
                raise subprocess.CalledProcessError(2, argv)
            self.routes = [r for r in self.routes if r["dst"] != "default"] + [route]
            self.replaced = True
        else:
            self.routes.append(route)
        return subprocess.CompletedProcess(argv, 0, "")


class UplinkTests(unittest.TestCase):
    def setUp(self):
        self.ip = FakeIP()
        self.config = {"uplink": {"interface": "lan0", "gateway": "192.168.61.1", "management_interface": "eth0", "management_routes": "10.42.0.0/16 192.168.1.9/32"}}
        self.mock = patch.object(uplink.subprocess, "run", self.ip)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def mutations(self):
        return [c for c in self.ip.calls if "-j" not in c]

    def test_disabled_has_no_network_calls(self):
        self.assertFalse(uplink.configure_uplink_routes({}))
        self.assertEqual(self.ip.calls, [])

    def test_handoff_order_and_same_pod_restart(self):
        self.assertTrue(uplink.configure_uplink_routes(self.config))
        mutations = self.mutations()
        self.assertEqual([c[3] for c in mutations], ["add", "add", "replace"])
        self.assertIn("dhcp", mutations[-1])
        self.assertIn("1400", mutations[-1])
        self.ip.calls.clear()
        self.assertFalse(uplink.configure_uplink_routes(self.config))
        self.assertEqual(self.mutations(), [])

    def test_autodetect_initial_management(self):
        self.config["uplink"]["management_interface"] = ""
        self.assertTrue(uplink.configure_uplink_routes(self.config))

    def test_invalid_config_no_network(self):
        for field, value in [("interface", "lo"), ("interface", "bmx_prime"), ("interface", "tbbwg"), ("interface", "x;id"), ("gateway", "::1"), ("management_interface", "lan0"), ("management_routes", "0.0.0.0/0"), ("management_routes", "10.200.0.0/14"), ("management_routes", "10.203.1.0/24"), ("management_routes", "192.168.1.9/24")]:
            with self.subTest(field=field, value=value):
                config = copy.deepcopy(self.config)
                config["uplink"][field] = value
                with self.assertRaises(ValueError):
                    uplink.configure_uplink_routes(config)
        self.assertEqual(self.ip.calls, [])

    def test_mesh_role_conflicts_before_network_calls(self):
        for field in ("interface", "management_interface"):
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                config["mesh"] = {"interfaces": "mesh0 mesh1"}
                config["uplink"][field] = "mesh0"
                with self.assertRaisesRegex(ValueError, "conflicts with configured mesh"):
                    uplink.configure_uplink_routes(config)
        self.assertEqual(self.ip.calls, [])

    def test_cli_resolves_mesh_schema(self):
        with patch.object(uplink, "require_valid_config", return_value={}) as resolver:
            uplink.main()
        self.assertEqual(resolver.call_args.kwargs["schema"],
                         uplink.UPLINK_CONFIG_SCHEMA + uplink.MESH_CONFIG_SCHEMA)

    def test_offlink_gateway_no_mutations(self):
        self.config["uplink"]["gateway"] = "192.168.62.1"
        with self.assertRaises(ValueError):
            uplink.configure_uplink_routes(self.config)
        self.assertEqual(self.mutations(), [])

    def test_conflict_preflight(self):
        self.ip.routes.append({"dst": "192.168.1.9/32", "dev": "other"})
        with self.assertRaisesRegex(ValueError, "conflicting"):
            uplink.configure_uplink_routes(self.config)
        self.assertEqual(self.mutations(), [])

    def test_kernel_host_route_spelling_is_idempotent(self):
        uplink.configure_uplink_routes(self.config)
        for route in self.ip.routes:
            if route["dst"] == "192.168.1.9/32":
                route["dst"] = "192.168.1.9"
        self.ip.calls.clear()
        self.assertFalse(uplink.configure_uplink_routes(self.config))
        self.assertEqual(self.mutations(), [])

    def test_additional_default_preflight(self):
        self.ip.routes.append({"dst": "default", "dev": "other"})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            uplink.configure_uplink_routes(self.config)
        self.assertEqual(self.mutations(), [])

    def test_ambiguous_restart_gateway_and_explicit_override(self):
        self.ip.routes = [{"dst": "default", "dev": "lan0", "gateway": "192.168.61.1"}, {"dst": "1.1.1.1/32", "dev": "eth0", "gateway": "10.42.0.1"}, {"dst": "2.2.2.2/32", "dev": "eth0", "gateway": "10.42.0.2"}]
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            uplink.configure_uplink_routes(self.config)
        self.assertEqual(self.mutations(), [])
        self.config["uplink"]["management_gateway"] = "10.42.0.1"
        self.assertTrue(uplink.configure_uplink_routes(self.config))

    def test_no_restart_gateway_does_not_guess(self):
        self.ip.routes = [{"dst": "default", "dev": "lan0", "gateway": "192.168.61.1"}]
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            uplink.configure_uplink_routes(self.config)
        self.assertEqual(self.mutations(), [])

    def test_verification_failure_rolls_back_original(self):
        self.ip.bad_verification = True
        with self.assertRaisesRegex(RuntimeError, "original default restored"):
            uplink.configure_uplink_routes(self.config)
        last = self.mutations()[-1]
        self.assertIn("10.42.0.1", last)
        self.assertIn("dhcp", last)
        self.assertIn("1400", last)

    def test_rollback_failure_is_explicit(self):
        self.ip.bad_verification = self.ip.fail_rollback = True
        with self.assertRaisesRegex(RuntimeError, "rollback failed.*keep mesh down"):
            uplink.configure_uplink_routes(self.config)

    def test_schema_defaults(self):
        from node_config import require_valid_config
        config = require_valid_config(schema=uplink.UPLINK_CONFIG_SCHEMA, defaults={}, env={})
        self.assertFalse(uplink.configure_uplink_routes(config))
        self.assertEqual(self.ip.calls, [])


if __name__ == "__main__":
    unittest.main()
