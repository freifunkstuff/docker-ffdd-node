import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from exit_config import parse_config
from exit_runtime import Controller, RuntimeErrorSafe, interface_name
try:
    from .test_exit_config import fixture
except ImportError:
    from test_exit_config import fixture

class FakeRunner:
    def __init__(self):
        self.commands = []
        self.links = []
        self.rules = []
        self.fail = None
        self.routes = []
        self.policies = []
    def run(self, args):
        self.commands.append(args)
        if args[0] == 'iptables-restore':
            self.policies.append(Path(args[-1]).read_text())
        if args[:4] == ['ip', '-j', 'address', 'show']:
            return json.dumps([{key: value for key, value in link.items() if key != 'ifalias'} for link in self.links])
        if args[:4] == ['ip', '-j', 'route', 'show']:
            return json.dumps(self.routes)
        if self.fail and self.fail in args:
            raise RuntimeError('SECRET STDERR')
        if args[:4] == ['ip', '-j', 'link', 'show']:
            return json.dumps(self.links)
        if args[:4] == ['ip', '-j', 'rule', 'show']:
            return json.dumps(self.rules)
        if '-j' in args:
            return '[]'
        return ''

class RuntimeTests(unittest.TestCase):
    def setup_controller(self):
        runner = FakeRunner()
        return Controller('10.200.3.149', ['10.200.0.0/15'], runner), runner

    def test_owned_names_and_plan(self):
        controller, runner = self.setup_controller()
        status = controller.apply(parse_config(fixture()))
        self.assertEqual(status['phase'], 'unreachable')
        self.assertLessEqual(len(interface_name('test')), 15)
        commands = runner.commands
        self.assertTrue(any('unreachable' in c for c in commands))
        self.assertFalse(any('main' in c or '-g' in c for c in commands))
        key = fixture()['exits'][0]['wireguard']['privateKey']
        self.assertNotIn(key, repr(commands))
        self.assertTrue(any('0.0.0.0/0' in c for c in commands))
        self.assertTrue(any('10.200.0.0/16' in c and 'MASQUERADE' in c for c in commands))

    def test_zero_prefix_never_installs_main_connected_route(self):
        controller, runner = self.setup_controller()
        data = fixture()
        data['exits'][0]['wireguard']['addresses'] = ['10.64.0.2/0']
        controller.apply(parse_config(data))
        added = [c for c in runner.commands if c[:3] == ['ip', 'address', 'add']]
        self.assertTrue(added)
        self.assertTrue(all('noprefixroute' in c for c in added))

    def test_local_tunnel_or_dns_address_collision_refused(self):
        for field, value in [('addresses', ['192.168.1.1/32']), ('dns', ['192.168.1.1'])]:
            controller, runner = self.setup_controller()
            runner.links = [{'ifname': 'eth0', 'addr_info': [{'family': 'inet', 'local': '192.168.1.1'}]}]
            data = fixture()
            data['exits'][0]['wireguard'][field] = value
            with self.assertRaises(RuntimeErrorSafe):
                controller.apply(parse_config(data))
            self.assertFalse(any(c[:3] == ['ip', 'link', 'add'] for c in runner.commands))

    def test_collision_refused(self):
        controller, runner = self.setup_controller()
        runner.links = [{'ifname': 'ffgw'}]
        with self.assertRaises(RuntimeErrorSafe):
            controller.apply(parse_config(fixture()))
        self.assertFalse(any(c[:4] == ['ip', 'link', 'del', 'dev'] for c in runner.commands))
        self.assertEqual(controller.status['phase'], 'error')

    def test_rule_collision(self):
        controller, runner = self.setup_controller()
        runner.rules = [{'priority': 100}]
        with self.assertRaises(RuntimeErrorSafe):
            controller.apply(parse_config(fixture()))
        self.assertFalse(any(c[:3] == ['ip', 'link', 'add'] for c in runner.commands))

    def test_fail_closed_and_safe_error(self):
        controller, runner = self.setup_controller()
        runner.fail = 'wg'
        with self.assertRaises(RuntimeErrorSafe) as caught:
            controller.apply(parse_config(fixture()))
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(controller.status['phase'], 'error')
        self.assertTrue(any(c[0] == 'iptables-restore' and '--noflush' in c for c in runner.commands))

    def test_sanitized_status(self):
        controller, runner = self.setup_controller()
        controller.apply(parse_config(fixture()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'exits.json'
            controller.write_status(path)
            public = json.loads(path.read_text())
        self.assertEqual(public['exits'][0]['interface'], interface_name('test'))
        self.assertIn('timestamp', public)
        self.assertNotIn('Key', repr(public))

    def test_requested_invalid_config_remains_enabled(self):
        controller, runner = self.setup_controller()
        controller.enabled = True
        controller.close()
        controller.status = {'phase': 'error', 'exits': []}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'exits.json'
            controller.write_status(path)
            public = json.loads(path.read_text())
        self.assertTrue(public['enabled'])
        self.assertEqual(public['phase'], 'error')
        self.assertEqual(public['exits'], [])

    def test_health_closes_and_recovers_same_interface(self):
        controller, runner = self.setup_controller()
        controller.apply(parse_config(fixture()))
        iface = controller.status['ifname']
        self.assertIn('-A FFDD_EXIT_FORWARD -i ffgw -j DROP', runner.policies[-1])
        self.assertTrue(controller.probe())
        self.assertEqual(controller.status['phase'], 'ready')
        self.assertNotIn('-A FFDD_EXIT_FORWARD -i ffgw -j DROP\n-A FFDD_EXIT_FORWARD -i wgx+', runner.policies[-1][:200])
        runner.fail = 'ping'
        self.assertFalse(controller.probe())
        self.assertEqual(controller.status['exits'][0]['state'], 'unreachable')
        self.assertIn('-A FFDD_EXIT_FORWARD -i ffgw -j DROP', runner.policies[-1])
        runner.fail = None
        self.assertTrue(controller.probe())
        self.assertEqual(controller.status['ifname'], iface)
        probes = [c for c in runner.commands if c[0] == 'ping']
        self.assertTrue(all(c[c.index('-I') + 1] == '10.64.0.2' for c in probes))
        repairs = [c for c in runner.commands if c[:3] == ['ip', 'route', 'replace']]
        self.assertEqual(len(repairs), len(probes))
        self.assertTrue(all('52000' in c and '192' in c for c in repairs))

    def test_input_scoped_to_interfaces(self):
        runner = FakeRunner()
        controller = Controller('10.200.3.149', ['10.200.0.0/15'], runner, ['mesh0'])
        controller.apply(parse_config(fixture()))
        rules = [line.split() for policy in runner.policies for line in policy.splitlines() if line.startswith('-A FFDD_EXIT_INPUT') and 'ACCEPT' in line]
        self.assertTrue(rules)
        self.assertTrue(all('-i' in c for c in rules))
        self.assertTrue(all(c[c.index('-i') + 1] in ('mesh0', 'tbb+', 'ffgw', 'wgx+') for c in rules))
        self.assertTrue(any('-A FFDD_EXIT_INPUT -d 10.200.3.149 -p 4 -j DROP' in policy for policy in runner.policies))
        self.assertTrue(all('ESTABLISHED,RELATED' in c and '-d' in c for c in rules if c[c.index('-i') + 1] == 'wgx+'))
        self.assertTrue(any('-A FFDD_EXIT_INPUT -i wgx+ -j DROP' in policy for policy in runner.policies))

    def test_blank_config_never_touches_network(self):
        import exit_service
        from unittest.mock import patch
        handlers = []
        with tempfile.TemporaryDirectory() as directory:
            def sleep(_):
                handlers[0]()
            with patch.dict('os.environ', {'EXIT_CONFIG_FILE': '', 'EXIT_STATUS_FILE': str(Path(directory) / 'exits.json')}), patch.object(exit_service.signal, 'signal', side_effect=lambda _, handler: handlers.append(handler)), patch.object(exit_service.time, 'sleep', side_effect=sleep), patch('exit_runtime.Runner.run', side_effect=AssertionError('network must not run')):
                exit_service.main()
            self.assertFalse(json.loads((Path(directory) / 'exits.json').read_text())['enabled'])

    def test_eigrp_without_owned_alias_refuses_adoption(self):
        controller, runner = self.setup_controller()
        runner.rules = [{'priority': 100, 'table': 52000, 'protocol': 'eigrp', 'iif': 'ffgw'}]
        runner.routes = [{'table': 52000, 'protocol': 'eigrp'}]
        with self.assertRaises(RuntimeErrorSafe):
            controller.apply(parse_config(fixture()))
        self.assertFalse(any(c[:3] == ['ip', 'rule', 'del'] or c[:3] == ['ip', 'route', 'flush'] for c in runner.commands))

    def test_restart_recovers_only_tagged_resources(self):
        controller, runner = self.setup_controller()
        runner.links = [{'ifname': 'ffgw', 'ifalias': 'ffdd-exit:ffgw'}, {'ifname': interface_name('test'), 'ifalias': 'ffdd-exit:' + interface_name('test'), 'addr_info': [{'family': 'inet', 'local': '10.64.0.2'}]}, {'ifname': 'wgxforeign'}]
        runner.rules = [{'priority': 100, 'table': 52000, 'protocol': 'eigrp', 'iif': 'ffgw'}, {'priority': 101, 'table': 52001}]
        runner.routes = [{'table': 52000, 'protocol': 'eigrp'}]
        controller.cleanup()
        deleted = [c[-1] for c in runner.commands if c[:4] == ['ip', 'link', 'del', 'dev']]
        self.assertIn('ffgw', deleted)
        self.assertNotIn('wgxforeign', deleted)
        self.assertTrue(any(c[:3] == ['ip', 'rule', 'del'] and '192' in c for c in runner.commands))
        self.assertIn('-A FFDD_EXIT_OUTPUT -s 10.64.0.2 -j DROP', runner.policies[-1])
        self.assertTrue(all('proto' in c and '192' in c for c in runner.commands if c[:3] == ['ip', 'route', 'flush']))

if __name__ == '__main__':
    unittest.main()
