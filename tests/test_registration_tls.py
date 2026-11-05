from __future__ import annotations

import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import node_config
import registration_tls

CA_FILE = Path(__file__).resolve().parents[1] / "config" / "ca-root-ffdd.crt"


class RegistrationTLSTests(unittest.TestCase):
    def config(self, url="https://register.example.org/bot", ca=""):
        return {"registrar": {"registration_url": url, "registration_ca_file": ca}}

    def test_system_trust_is_secure_default(self):
        with patch("registration_tls.ssl.create_default_context", wraps=ssl.create_default_context) as create:
            context = registration_tls.build_registration_ssl_context(self.config())
        create.assert_called_once_with(cafile=None)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_default_host_uses_only_bundled_ca(self):
        with patch.object(registration_tls, "BUNDLED_REGISTRATION_CA_FILE", str(CA_FILE)):
            context = registration_tls.build_registration_ssl_context(
                self.config("https://selfsigned.register.freifunk-dresden.de/bot.php")
            )
        self.assertEqual(context.cert_store_stats()["x509_ca"], 1)
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_similar_hostname_does_not_receive_default_trust(self):
        with patch("registration_tls.ssl.create_default_context") as create:
            registration_tls.build_registration_ssl_context(
                self.config("https://selfsigned.register.freifunk-dresden.de.attacker.example/bot")
            )
        create.assert_called_once_with(cafile=None)

    def test_explicit_private_ca_keeps_verification_enabled(self):
        context = registration_tls.build_registration_ssl_context(self.config(ca=str(CA_FILE)))
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(context.cert_store_stats()["x509_ca"], 1)

    def test_explicit_ca_overrides_default_host(self):
        with patch("registration_tls.ssl.create_default_context") as create:
            registration_tls.build_registration_ssl_context(self.config(
                "https://selfsigned.register.freifunk-dresden.de/bot", "/private/ca.pem"
            ))
        create.assert_called_once_with(cafile="/private/ca.pem")

    def test_missing_and_malformed_ca_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "bad.pem"
            malformed.write_text("not a certificate", encoding="utf-8")
            for path in (str(malformed), str(Path(directory) / "missing.pem"), directory):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "NODE_REGISTRATION_CA_FILE"):
                    registration_tls.build_registration_ssl_context(self.config(ca=path))

    def test_missing_bundled_ca_fails_closed(self):
        with patch.object(registration_tls, "BUNDLED_REGISTRATION_CA_FILE", "/nonexistent/ffdd-ca.pem"):
            with self.assertRaises(ValueError):
                registration_tls.build_registration_ssl_context(self.config(
                    "https://selfsigned.register.freifunk-dresden.de/bot"
                ))

    def test_invalid_urls_rejected_without_disclosing_credential(self):
        for url in ("http://host/?key=secret", "file:///secret", "https:///secret",
                    "https://user:secret@host", "https://host:bad/?key=secret", "https://[bad"):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                registration_tls.build_registration_ssl_context(self.config(url))
            self.assertNotIn("secret", str(error.exception))

    def test_schema_default_blank_and_override(self):
        for env, defaults, expected in (
            ({}, {}, ""),
            ({"NODE_REGISTRATION_CA_FILE": ""}, {"NODE_REGISTRATION_CA_FILE": "/default.pem"}, "/default.pem"),
            ({"NODE_REGISTRATION_CA_FILE": " /custom.pem "}, {"NODE_REGISTRATION_CA_FILE": "/default.pem"}, "/custom.pem"),
        ):
            with self.subTest(env=env):
                result = node_config.resolve_config(schema=node_config.REGISTRATION_TLS_CONFIG_SCHEMA,
                    env=env, defaults=defaults, base_values={})
                self.assertEqual(result.errors, [])
                self.assertEqual(result.values["registrar"]["registration_ca_file"], expected)


if __name__ == "__main__":
    unittest.main()
