"""Unprivileged owner tests: no real keys, namespaces or host mutations."""
import base64
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import gateway_runtime as runtime


def key(n):
    return base64.b64encode(bytes([n]) * 32).decode()


class FakeRunner:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.links = []
        self.current = key(2)
        self.handshakes = ''

    def run(self, *args, **kwargs):
        self.calls.append(args)
        if self.fail and self.fail in args:
            raise runtime.RuntimeFailure('synthetic failure')
        if args == ('ip', '-d', '-j', 'link', 'show'):
            output = json.dumps(self.links)
        elif args == ('wg', 'show', 'tbbwg', 'peers'):
            output = self.current
        elif args == ('wg', 'show', 'tbbwg', 'latest-handshakes'):
            output = self.handshakes
        else:
            output = ''
        if 'private-key' in args:
            path = Path(args[args.index('private-key') + 1])
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.read_text().strip() == key(1)
        return SimpleNamespace(stdout=output, returncode=0)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / 'bmxd.env'
        self.env.write_text('BMXD_PRIMARY_IP="10.200.3.149"\nBMXD_BACKBONE_INTERFACES="tbb_fastd"\n')
        self.runner = FakeRunner()
        self.owner = runtime.RuntimeOwner(self.runner, self.root / 'run', self.env, self.root / 'public.json')
        self.outgoing = [dict(node=51020, key=key(2), endpoint='example.invalid:5003')]
        self.incoming = [dict(node=15, publickey=key(3), created_at=1)]

    def apply(self):
        return self.owner.reconcile(913, key(1), self.outgoing, self.incoming)

    def test_union_preserves_existing_outgoing_and_accepts_endpointless_incoming(self):
        self.apply()
        commands = self.runner.calls
        self.assertFalse(any(c[-1] == 'remove' for c in commands))
        incoming = next(c for c in commands if c[:5] == ('wg', 'set', 'tbbwg', 'peer', key(3)))
        self.assertNotIn('endpoint', incoming)
        self.assertIn('10.203.0.16/32', incoming)
        outgoing = next(c for c in commands if c[:5] == ('wg', 'set', 'tbbwg', 'peer', key(2)))
        self.assertIn('example.invalid:5003', outgoing)
        self.assertIn(('bmxd', '-c', 'dev=tbb_wg15', '/linklayer', '1'), commands)
        self.assertIn('tbb_fastd tbb_wg15 tbb_wg51020', self.env.read_text())
        payload = json.loads((self.root / 'run/runtime.json').read_text())
        self.assertTrue(payload['peers']['15']['applied'])
        self.assertNotIn(key(3), (self.root / 'public.json').read_text())
        self.assertNotIn(key(1), (self.root / 'run/runtime.json').read_text())
        self.assertFalse(list((self.root / 'run').glob('wg-key-*')))

    def test_reciprocal_exact_peer_merges_origins_and_keeps_endpoint(self):
        self.incoming = [dict(node=51020, publickey=key(2))]
        self.apply()
        commands = [c for c in self.runner.calls if c[:5] == ('wg', 'set', 'tbbwg', 'peer', key(2))]
        self.assertEqual(len(commands), 1)
        self.assertIn('example.invalid:5003', commands[0])
        status = json.loads((self.root / 'run/runtime.json').read_text())['peers']['51020']
        self.assertTrue(status['inbound'])
        self.assertTrue(status['outgoing'])
        self.assertEqual(status['origin'], 'both')

    def test_operator_revocation_overrides_matching_outgoing_not_expiry(self):
        self.incoming = [dict(node=51020, publickey=key(2))]
        self.apply()
        self.runner.links = [dict(ifname='tbb_wg51020', ifalias='ffdd-gateway:51020')]
        self.runner.calls.clear()
        self.incoming[0]['revoked'] = True
        self.apply()
        self.assertIn(('wg', 'set', 'tbbwg', 'peer', key(2), 'remove'), self.runner.calls)
        self.assertIn(('bmxd', '-c', 'dev=-tbb_wg51020'), self.runner.calls)
        self.assertIn(('ip', 'link', 'del', 'tbb_wg51020'), self.runner.calls)
        self.incoming = []
        self.runner.links = []
        self.runner.calls.clear()
        self.apply()
        self.assertTrue(any(c[:5] == ('wg', 'set', 'tbbwg', 'peer', key(2)) for c in self.runner.calls))

    def test_conflicts_are_rejected_before_kernel_mutation(self):
        for record in [dict(node=15, publickey=key(2)), dict(node=51020, publickey=key(3))]:
            self.incoming = [record]
            with self.assertRaises(ValueError):
                self.apply()
            self.assertEqual(self.runner.calls, [])

    def test_failed_bmxd_never_acknowledges_enrollment(self):
        self.runner.fail = 'bmxd'
        with self.assertRaises(runtime.RuntimeFailure):
            self.apply()
        payload = json.loads((self.root / 'run/runtime.json').read_text())
        self.assertEqual(payload['peers'], {})
        self.assertFalse((self.root / 'public.json').exists())
        first_attach = next(i for i, c in enumerate(self.runner.calls) if c[0] == 'bmxd')
        for name in ('tbb_wg15', 'tbb_wg51020'):
            self.assertLess(self.runner.calls.index(('ip', 'link', 'set', name, 'up')), first_attach)
        self.runner.fail = None
        self.runner.calls.clear()
        self.apply()
        self.assertIn(('bmxd', '-c', 'dev=tbb_wg15', '/linklayer', '1'), self.runner.calls)

    def test_unknown_unaliased_link_is_not_deleted(self):
        self.runner.links = [dict(ifname='tbb_wg777', linkinfo={'info_kind': 'ipip'})]
        self.apply()
        self.assertFalse(any(c[:3] == ('ip', 'link', 'del') for c in self.runner.calls))

    def test_conflicting_existing_tunnel_fails_closed(self):
        self.runner.links = [dict(ifname='tbb_wg15', linkinfo={'info_kind': 'dummy'})]
        with self.assertRaises(runtime.RuntimeFailure):
            self.apply()
        self.assertFalse(any(c[:3] == ('ip', 'link', 'del') for c in self.runner.calls))

    def test_only_known_inbound_nonzero_handshakes_returned(self):
        self.runner.handshakes = f'{key(2)} 123\n{key(3)} 456\n{key(4)} 789\n'
        self.assertEqual(self.apply(), {key(3): 456})
        self.runner.handshakes = f'{key(3)} 0\n'
        self.assertEqual(self.apply(), {})

    def test_unchanged_intent_only_refreshes_status(self):
        self.apply()
        self.runner.calls.clear()
        self.incoming[0]['last_request_at'] = 12345
        self.incoming[0]['last_handshake_at'] = 12345
        self.apply()
        self.assertEqual(self.runner.calls, [('wg', 'show', 'tbbwg', 'latest-handshakes')])
        self.incoming.append(dict(node=16, publickey=key(4)))
        self.runner.calls.clear()
        self.apply()
        self.assertIn(('bmxd', '-c', 'dev=tbb_wg16', '/linklayer', '1'), self.runner.calls)

    def test_node_zero_is_valid_remote(self):
        peers = runtime.union_peers(913, [], [dict(node=0, publickey=key(3))])
        self.assertEqual(peers[0]['node'], 0)
        self.assertEqual(runtime.effective_node({'BMXD_NODE_ID': '0'}), 0)

    def test_effective_runtime_address_wins(self):
        self.assertEqual(runtime.effective_node({'BMXD_PRIMARY_IP': '10.200.3.149'}), 913)
        with self.assertRaises(ValueError):
            runtime.effective_node({'BMXD_PRIMARY_IP': '192.0.2.1'})

    def test_default_node_role_does_nothing(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {}, clear=True), patch.object(runtime, 'RuntimeOwner') as owner:
            self.assertEqual(runtime.main(), 0)
            owner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
