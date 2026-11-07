import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import registrar


class IdentityPinTests(unittest.TestCase):
    def config(self, expected=1468):
        return {'registrar': {'mode': 'registered', 'initial_node_id': 1468,
                             'expected_node_id': expected,
                             'registration_url': 'https://example.org/register?key=__NODE_REGISTER_KEY__&node=__NODE_ID__'}}

    def state(self, node_id=1468):
        return {'registration': {'node_id': node_id, 'register_key': 'private-test-token'}}

    def call(self, config, state, assigned):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'registration': {'status': 'ok', 'node': assigned}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(registrar, 'build_registration_ssl_context', return_value=object()), patch.object(registrar.urllib.request, 'build_opener', return_value=opener):
            return registrar.register_once(config, state)

    def test_matching_identity_accepted(self):
        state = self.state()
        assigned, _ = self.call(self.config(), state, 1468)
        self.assertEqual(assigned, 1468)
        self.assertEqual(state['registration']['node_id'], 1468)

    def test_unexpected_assignment_never_changes_state_or_leaks_token(self):
        state = self.state()
        before = copy.deepcopy(state)
        with self.assertRaises(SystemExit) as error:
            self.call(self.config(), state, 51014)
        self.assertEqual(state, before)
        self.assertIn('registry assigned 51014', str(error.exception))
        self.assertNotIn('private-test-token', str(error.exception))

    def test_wrong_persisted_identity_rejected_before_http(self):
        state = self.state(51014)
        with patch.object(registrar.urllib.request, 'build_opener') as opener:
            with self.assertRaisesRegex(SystemExit, 'persisted node 51014'):
                registrar.register_once(self.config(), state)
            opener.assert_not_called()
        self.assertEqual(state['registration']['node_id'], 51014)

    def test_automatic_assignment_remains_default_compatible(self):
        state = self.state()
        assigned, _ = self.call(self.config(0), state, 51014)
        self.assertEqual(assigned, 51014)
        self.assertEqual(state['registration']['node_id'], 51014)

    def test_schema_and_temporary_conflict(self):
        env = {'BACKBONE_ENABLED': '0', 'NODE_REGISTRATION_URL': 'https://example.org/?key=__NODE_REGISTER_KEY__&node=__NODE_ID__'}
        config = registrar.require_valid_registrar_config(defaults={}, env=env)
        self.assertEqual(config['registrar']['expected_node_id'], 0)
        env['NODE_EXPECTED_ID'] = '1468'
        self.assertEqual(registrar.require_valid_registrar_config(defaults={}, env=env)['registrar']['expected_node_id'], 1468)
        env['NODE_EXPECTED_ID'] = '-1'
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(defaults={}, env=env)
        env.update(NODE_EXPECTED_ID='1468', NODE_REGISTRATION_MODE='temporary')
        with self.assertRaises(SystemExit):
            registrar.require_valid_registrar_config(defaults={}, env=env)
