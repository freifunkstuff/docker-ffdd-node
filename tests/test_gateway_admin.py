import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gateway_admin as admin

KEY = base64.b64encode(b'a' * 32).decode()


class Clock:
    value = 0
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class Store:
    def __init__(self):
        self.peers = [dict(node=42, publickey=KEY, created_at=0,
                           last_handshake_at=None, revoked=False, pinned=False,
                           secret='NEVER-PRINT', source_ip='198.51.100.9')]
    def snapshot(self):
        return self.peers
    def revoke(self, node):
        self.peers[0]['revoked'] = True
        return True
    def expired(self, **kwargs):
        return self.peers


class Fastd:
    revoked = False
    def counts(self):
        return {'accepted': 1, 'connected': 0}
    def revoke(self, key):
        self.revoked = True
        return True
    def unrevoke(self, key):
        self.revoked = False
        return True


class Runner:
    def __init__(self, peers='', links='[]', failure=False):
        self.peers, self.links, self.failure = peers, links, failure
        self.calls = []
    def run(self, args, timeout):
        self.calls.append((args, timeout))
        return SimpleNamespace(returncode=int(self.failure), stdout=(self.peers if args[0] == 'wg' else self.links))


class AdminTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runtime.json'
        self.path.write_text(json.dumps({'state': 'ready', 'peers': {}}))
        self.store, self.fastd, self.clock = Store(), Fastd(), Clock()
        self.runner = Runner()
        self.env = {'NODE_ROLE': 'gateway', 'GATEWAY_RUNTIME_DIR': self.temp.name}

    def run_cli(self, args, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = admin.main(args, self.env, store_factory=lambda **kw: self.store,
                              fastd_factory=lambda env: self.fastd, runner=self.runner,
                              clock=self.clock, sleep=self.clock.sleep, now=lambda: 40 * 86400,
                              getuid=lambda: 0, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_role_default_no_io(self):
        with patch.object(admin, 'GatewayStore', side_effect=AssertionError):
            self.env = {}
            self.assertEqual(self.run_cli(['list'])[0], 1)
        self.assertFalse(self.store.peers[0]['revoked'])

    def test_list_redacted_and_gc_preview(self):
        for args in (['list'], ['list', '--fingerprints'], ['wg-gc'], ['wg-gc', '--dry-run']):
            code, out, err = self.run_cli(args)
            self.assertEqual(code, 0)
            for secret in (KEY, 'NEVER-PRINT', '198.51.100.9'):
                self.assertNotIn(secret, out + err)
        self.assertEqual(json.loads(self.run_cli(['wg-gc'])[1])['candidates'], [{'node': 42, 'ageDays': 40}])
        self.assertEqual(self.run_cli(['wg-gc', '--apply'])[0], 1)
        self.assertFalse(self.store.peers[0]['revoked'])

    def test_revoke_requires_kernel_and_owner_proof(self):
        self.assertEqual(self.run_cli(['wg-revoke', '42'])[0], 0)
        self.assertTrue(self.store.peers[0]['revoked'])
        self.assertEqual([c[0] for c in self.runner.calls], [['wg', 'show', 'tbbwg', 'peers'], ['ip', '-j', 'link', 'show']])
        for peers, links, state in ((KEY, '[]', {'state': 'ready', 'peers': {}}),
                ('', '[{"ifname":"tbb_wg42"}]', {'state': 'ready', 'peers': {}}),
                ('', '[]', {'state': 'applying', 'peers': {}}),
                ('', '[]', {'state': 'ready', 'peers': {'42': {'key': KEY, 'applied': True}}})):
            self.clock.value = 0
            self.runner = Runner(peers, links)
            self.path.write_text(json.dumps(state))
            code, out, err = self.run_cli(['wg-revoke', '42'])
            self.assertEqual(code, 1)
            self.assertIn('pending', err)
            self.assertEqual(out, '')
            self.assertLessEqual(self.clock.value, 5)

    def test_fastd_restart_and_failure_keep_tombstone(self):
        self.assertEqual(self.run_cli(['fastd-revoke', 'ab' * 32])[0], 0)
        self.assertEqual(self.runner.calls[0][0], ['sv', 'restart', '/etc/service/fastd'])
        self.runner.failure = True
        code, out, err = self.run_cli(['fastd-revoke', 'ab' * 32])
        self.assertEqual(code, 1)
        self.assertTrue(self.fastd.revoked)
        self.assertIn('pending', err)
        self.assertEqual(self.run_cli(['fastd-unrevoke', 'ab' * 32])[0], 0)
        self.assertFalse(self.fastd.revoked)

    def test_invalid_arguments_and_exceptions_never_echo(self):
        for args in (['fastd-revoke', 'private-NO'], ['wg-revoke', '../private-NO'], ['wat-private-NO']):
            code, out, err = self.run_cli(args)
            self.assertEqual(code, 1)
            self.assertNotIn('private-NO', out + err)
        self.runner.failure = True
        self.assertEqual(self.run_cli(['wg-revoke', '42'])[0], 1)


if __name__ == '__main__':
    unittest.main()
