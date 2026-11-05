from __future__ import annotations

from contextlib import ExitStack
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import registrar
from node_config import build_base_config


class WireguardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(registrar, "WIREGUARD_RUNTIME_DIR", self.runtime))
        self.stack.enter_context(patch.object(registrar, "WIREGUARD_ENV_FILE", self.runtime / "wireguard.env"))
        self.stack.enter_context(patch.object(registrar, "BMXD_RUNTIME_DIR", self.runtime / "bmxd"))
        self.ensure = self.stack.enter_context(patch.object(registrar, "_ensure_wireguard_interface", return_value=False))
        self.list_tunnels = self.stack.enter_context(patch.object(registrar, "_list_wireguard_tunnels", return_value=["tbb_wg52002"]))
        self.recreate = self.stack.enter_context(patch.object(registrar, "_recreate_ipip_interface"))
        self.peers = [{"host": "gateway.example.org", "endpoint_port": "5003", "key": "public-key", "node": "52002", "ifname": "tbb_wg52002"}]
        self.config = build_base_config()
        self.config["wireguard"] = {"port": 5003}

    def reconcile(self):
        return registrar.reconcile_wireguard_runtime(self.config, "secret", self.peers, 52001, "local-key")

    def test_failed_apply_does_not_commit_and_retry_repairs_existing_interfaces(self):
        env = self.runtime / "wireguard.env"
        env.write_text("old-applied-config\n")
        with patch.object(registrar, "_sync_wireguard_peers", side_effect=[subprocess.CalledProcessError(1, "wg"), None]) as sync:
            with self.assertRaises(subprocess.CalledProcessError):
                self.reconcile()
            self.assertEqual(env.read_text(), "old-applied-config\n")
            changed, interfaces = self.reconcile()
        self.assertTrue(changed)
        self.assertEqual(interfaces, ["tbb_wg52002"])
        self.assertEqual(sync.call_count, 2)
        self.recreate.assert_called_once()
        self.assertIn("gateway.example.org:5003", env.read_text())

    def test_unchanged_config_refreshes_endpoint_without_recreating_ipip(self):
        with patch.object(registrar, "_sync_wireguard_peers") as sync:
            self.reconcile()
            self.recreate.reset_mock()
            changed, _ = self.reconcile()
        self.assertFalse(changed)
        self.assertEqual(sync.call_count, 2)
        self.recreate.assert_not_called()

    def test_failed_ipip_apply_remains_retryable(self):
        with patch.object(registrar, "_sync_wireguard_peers"):
            self.recreate.side_effect = [RuntimeError("ipip failed"), None]
            with self.assertRaises(RuntimeError):
                self.reconcile()
            self.assertFalse((self.runtime / "wireguard.env").exists())
            self.assertTrue(self.reconcile()[0])
        self.assertEqual(self.recreate.call_count, 2)

    def test_half_created_ipip_is_repaired_even_with_unchanged_committed_config(self):
        with patch.object(registrar, "_sync_wireguard_peers"):
            self.reconcile()
            self.list_tunnels.return_value = []
            self.recreate.side_effect = RuntimeError("link created but address failed")
            with self.assertRaises(RuntimeError):
                self.reconcile()
            self.assertTrue((self.runtime / "apply-pending").exists())
            self.list_tunnels.return_value = ["tbb_wg52002"]
            self.recreate.reset_mock(side_effect=True)
            self.assertTrue(self.reconcile()[0])
            self.recreate.assert_called_once()
            self.assertFalse((self.runtime / "apply-pending").exists())

    def test_topology_repair_invalidates_bmxd_ack_before_later_service_failure(self):
        marker = self.runtime / "bmxd/service-applied.sha256"
        with patch.object(registrar, "_sync_wireguard_peers"):
            self.reconcile()
            marker.parent.mkdir()
            marker.write_text("previous successful restart")
            self.list_tunnels.return_value = []
            self.reconcile()
        self.assertFalse(marker.exists())
        with patch.object(registrar, "restart_service") as restart:
            registrar.reconcile_service("bmxd", [self.runtime / "wireguard.env"], marker)
        restart.assert_called_once_with("bmxd")

    def test_stale_tunnel_deletion_failure_keeps_apply_dirty(self):
        with patch.object(registrar, "_sync_wireguard_peers"):
            self.reconcile()
            self.list_tunnels.return_value = ["tbb_wg52002", "tbb_wg999"]
            with patch.object(registrar, "_run_ip", side_effect=RuntimeError("delete failed")) as run_ip:
                with self.assertRaises(RuntimeError):
                    self.reconcile()
            self.assertNotIn("check", run_ip.call_args.kwargs)
            self.assertTrue((self.runtime / "apply-pending").exists())

    def test_no_peers_removes_interfaces_and_commits_after_success(self):
        self.peers = []
        with patch.object(registrar, "_link_exists", return_value=True), patch.object(registrar, "_run_ip") as run_ip:
            changed, interfaces = self.reconcile()
        self.assertTrue(changed)
        self.assertEqual(interfaces, [])
        self.assertEqual(run_ip.call_args_list[0].args, ("link", "del", "tbb_wg52002"))
        self.assertEqual(run_ip.call_args_list[1].args, ("link", "del", registrar.WIREGUARD_INTERFACE))
        self.assertTrue((self.runtime / "wireguard.env").exists())

    def test_sync_updates_existing_peers_in_place_and_only_removes_obsolete(self):
        result = subprocess.CompletedProcess(["wg", "show"], 0, "public-key\nobsolete-key\n", "")
        with patch.object(registrar.subprocess, "run", return_value=result) as run:
            registrar._sync_wireguard_peers("tbbwg", self.peers)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[1][-2:], ["endpoint", "gateway.example.org:5003"])
        self.assertEqual(calls[2], ["wg", "set", "tbbwg", "peer", "obsolete-key", "remove"])
        self.assertNotIn(["wg", "set", "tbbwg", "peer", "public-key", "remove"], calls)

    def test_add_failure_does_not_remove_any_existing_peer(self):
        result = subprocess.CompletedProcess(["wg", "show"], 0, "public-key\nobsolete-key\n", "")
        with patch.object(registrar.subprocess, "run", side_effect=[result, subprocess.CalledProcessError(1, "wg")]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                registrar._sync_wireguard_peers("tbbwg", self.peers)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(any("remove" in c.args[0] for c in run.call_args_list))

    def test_failed_live_inventory_is_not_treated_as_empty(self):
        result = subprocess.CompletedProcess(["wg", "show"], 1, "", "failed")
        with patch.object(registrar.subprocess, "run", return_value=result) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                registrar._sync_wireguard_peers("tbbwg", self.peers)
        run.assert_called_once()


class ServiceRecoveryTests(unittest.TestCase):
    def test_new_registrar_applies_changed_files_even_on_first_iteration(self):
        config = build_base_config()
        config["registrar"] = {"initial_node_id": 52001}
        state = {"registration": {"node_id": 52001}}
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            for name, value in {
                "FASTD_RUNTIME_DIR": root / "fastd", "PEER_DIR": root / "fastd/peers",
                "FASTD_CONFIG": root / "fastd/fastd.conf", "FASTD_ENV_FILE": root / "fastd/fastd.env",
                "BMXD_RUNTIME_DIR": root / "bmxd", "BMXD_ENV_FILE": root / "bmxd/bmxd.env",
                "WIREGUARD_ENV_FILE": root / "wireguard/wireguard.env",
            }.items():
                stack.enter_context(patch.object(registrar, name, value))
            stack.enter_context(patch.object(registrar, "load_state", return_value=state))
            stack.enter_context(patch.object(registrar, "save_state"))
            stack.enter_context(patch.object(registrar, "ensure_secret", return_value=("secret", False)))
            stack.enter_context(patch.object(registrar, "ensure_wireguard_secret", return_value=("secret", False)))
            stack.enter_context(patch.object(registrar, "ensure_register_key", return_value="register-key"))
            stack.enter_context(patch.object(registrar, "register_once", return_value=(52001, "url")))
            stack.enter_context(patch.object(registrar, "requested_peer_string", return_value=("test", "")))
            stack.enter_context(patch.object(registrar, "parse_peer_string", return_value=([], [])))
            stack.enter_context(patch.object(registrar, "validate_requested_peers", return_value=([], [])))
            stack.enter_context(patch.object(registrar, "derive_wireguard_public_key", return_value="key"))
            stack.enter_context(patch.object(registrar, "resolve_requested_peers", return_value=[{"type": "fastd"}]))
            stack.enter_context(patch.object(registrar, "render_fastd_runtime", return_value=False))
            stack.enter_context(patch.object(registrar, "reconcile_wireguard_runtime", return_value=(False, [])))
            stack.enter_context(patch.object(registrar, "render_bmxd_runtime", return_value=False))
            stack.enter_context(patch.object(registrar, "log_info"))
            restart = stack.enter_context(patch.object(registrar, "restart_service"))
            registrar.run_iteration(True, True, config)
            self.assertEqual([c.args[0] for c in restart.call_args_list], ["fastd", "bmxd"])
            restart.reset_mock()
            registrar.run_iteration(True, True, config)
            restart.assert_not_called()
            registrar.FASTD_CONFIG.write_text("new desired configuration")
            registrar.run_iteration(True, True, config)
            restart.assert_called_once_with("fastd")

    def test_failed_restart_is_not_acknowledged_and_next_process_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "service.env"
            marker = Path(temp) / "applied"
            source.write_text("desired")
            with patch.object(registrar, "restart_service", side_effect=[subprocess.CalledProcessError(1, "sv"), None]) as restart:
                with self.assertRaises(subprocess.CalledProcessError):
                    registrar.reconcile_service("bmxd", [source], marker)
                self.assertFalse(marker.exists())
                registrar.reconcile_service("bmxd", [source], marker)
            self.assertEqual(restart.call_count, 2)
            self.assertTrue(marker.exists())

    def test_forced_restart_failure_invalidates_previous_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "service.env"
            marker = Path(temp) / "applied"
            source.write_text("desired")
            with patch.object(registrar, "restart_service"):
                registrar.reconcile_service("bmxd", [source], marker)
            with patch.object(registrar, "restart_service", side_effect=RuntimeError("failed")):
                with self.assertRaises(RuntimeError):
                    registrar.reconcile_service("bmxd", [source], marker, force=True)
            self.assertFalse(marker.exists())
            with patch.object(registrar, "restart_service") as restart:
                registrar.reconcile_service("bmxd", [source], marker)
            restart.assert_called_once_with("bmxd")

    def test_sv_restart_failure_propagates(self):
        with patch.object(registrar.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "sv")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                registrar.restart_service("bmxd")
        self.assertTrue(run.call_args.kwargs["check"])


class RegistrationRequestTests(unittest.TestCase):
    def test_registration_uses_verified_context_and_redirect_policy(self):
        config = {"registrar": {"registration_url": "https://example.org/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__", "initial_node_id": 52001}}
        context = object()
        response = io.BytesIO(b'{"registration":{"status":"ok","node":52001}}')
        opener = Mock()
        opener.open.return_value = response
        state = {"registration": {"node_id": 52001, "register_key": "test-key"}}
        with patch.object(registrar, "build_registration_ssl_context", return_value=context) as build_context, patch.object(registrar.urllib.request, "HTTPSHandler") as handler, patch.object(registrar.urllib.request, "build_opener", return_value=opener) as build_opener:
            assigned, _ = registrar.register_once(config, state)
        self.assertEqual(assigned, 52001)
        build_context.assert_called_once_with(config)
        handler.assert_called_once_with(context=context)
        self.assertIsInstance(build_opener.call_args.args[1], registrar.RegistrationRedirectHandler)

    def test_redirects_refuse_plaintext_and_other_origins(self):
        handler = registrar.RegistrationRedirectHandler()
        request = urllib.request.Request("https://example.org/register?key=secret")
        for target in ["http://example.org/register", "https://attacker.org/register", "https://example.org:444/register", "https://user@example.org/register"]:
            with self.subTest(target=target), self.assertRaises(urllib.error.URLError):
                handler.redirect_request(request, None, 302, "redirect", {}, target)
        redirected = handler.redirect_request(request, None, 302, "redirect", {}, "https://example.org/new")
        self.assertEqual(redirected.full_url, "https://example.org/new")

    def test_invalid_trust_prevents_network_request(self):
        config = {"registrar": {"registration_url": "http://example.org/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__", "initial_node_id": 52001}}
        with patch.object(registrar.urllib.request, "build_opener") as opener:
            with self.assertRaises(SystemExit):
                registrar.register_once(config, {})
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
