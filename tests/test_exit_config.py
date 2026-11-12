import base64
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from exit_config import ConfigError, parse_config


def fixture():
    return {'exits': [{'name': 'test', 'wireguard': {'privateKey': base64.b64encode(bytes(32)).decode(), 'addresses': ['10.64.0.2/32'], 'dns': ['10.64.0.1'], 'peer': {'publicKey': base64.b64encode(bytes(range(32))).decode(), 'endpoint': '127.0.0.1:51820'}}}]}

class ConfigTests(unittest.TestCase):
    def test_valid_and_secret_repr(self):
        data = fixture()
        config = parse_config(data)
        self.assertEqual(config.exits[0].dns, ('10.64.0.1',))
        self.assertNotIn(data['exits'][0]['wireguard']['privateKey'], repr(config))
        self.assertNotIn(data['exits'][0]['wireguard']['peer']['publicKey'], repr(config))

    def test_strict(self):
        for extra in ('activeExit', 'mullvad', 'privateKey'):
            data = fixture()
            data[extra] = 'secret'
            with self.assertRaises(ConfigError):
                parse_config(data)

    def test_errors_sanitized(self):
        data = fixture()
        data['exits'][0]['wireguard']['privateKey'] = 'VERY_SECRET_INVALID'
        with self.assertRaises(ConfigError) as caught:
            parse_config(data)
        self.assertNotIn('VERY_SECRET', str(caught.exception))

    def test_duplicate(self):
        data = fixture()
        data['exits'] *= 2
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_ipv6_and_bad_endpoint_rejected(self):
        for field, value in [('addresses', ['::1/128']), ('dns', ['::1'])]:
            data = fixture()
            data['exits'][0]['wireguard'][field] = value
            with self.assertRaises(ConfigError):
                parse_config(data)
        data = fixture()
        data['exits'][0]['wireguard']['peer']['endpoint'] = 'host:99999'
        with self.assertRaises(ConfigError):
            parse_config(data)

    def test_unsafe_and_duplicate_addresses_rejected(self):
        for address in ('127.0.0.1/32', '169.254.1.1/16', '10.200.1.1/32', '10.201.1.1/32', '10.203.1.1/32', '224.1.1.1/32'):
            data = fixture()
            data['exits'][0]['wireguard']['addresses'] = [address]
            with self.assertRaises(ConfigError):
                parse_config(data)
        data = fixture()
        data['exits'][0]['wireguard']['addresses'] = ['10.64.0.2/32', '10.64.0.2/24']
        with self.assertRaises(ConfigError):
            parse_config(data)
        for dns in ('127.0.0.1', '169.254.1.1', '10.64.0.2'):
            data = fixture()
            data['exits'][0]['wireguard']['dns'] = [dns]
            with self.assertRaises(ConfigError):
                parse_config(data)

if __name__ == '__main__':
    unittest.main()
