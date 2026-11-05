"""Verified TLS policy for registration; never fall back to unverified HTTPS."""
from __future__ import annotations

import ssl
from urllib.parse import urlsplit


DEFAULT_REGISTRATION_HOST = "selfsigned.register.freifunk-dresden.de"
BUNDLED_REGISTRATION_CA_FILE = "/usr/local/share/freifunk/ca-root-ffdd.crt"


def build_registration_ssl_context(config: dict) -> ssl.SSLContext:
    """Validate the URL and load system trust or an explicitly selected PEM CA.

    Call during configuration validation as well as before the request so a
    missing/invalid private CA fails closed. No URL is included in errors: its
    query can contain the node's registration credential.
    """
    registrar = config.get("registrar", {})
    try:
        url = urlsplit(str(registrar.get("registration_url", "")))
        valid_url = (
            url.scheme == "https"
            and bool(url.hostname)
            and url.username is None
            and url.password is None
        )
        # Accessing port also detects malformed/out-of-range ports.
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("NODE_REGISTRATION_URL must be an HTTPS URL without userinfo")

    ca_file = str(registrar.get("registration_ca_file") or "").strip()
    bundled_ca = not ca_file and url.hostname == DEFAULT_REGISTRATION_HOST
    if bundled_ca:
        ca_file = BUNDLED_REGISTRATION_CA_FILE
    try:
        # With cafile=None Python loads its default system trust store. An
        # explicit cafile deliberately limits trust to that supplied bundle.
        context = ssl.create_default_context(cafile=ca_file or None)
        if bundled_ca:
            # The authoritative 2021 FFDD root lacks keyUsage. Python 3.13+
            # enables strict RFC-5280 profile checks by default; permit this
            # legacy root only, retaining chain, hostname and expiry checks.
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return context
    except (OSError, ValueError) as exc:
        raise ValueError(
            "NODE_REGISTRATION_CA_FILE: unable to load registration CA trust; "
            "provide a readable PEM CA bundle or leave blank for system trust"
        ) from exc
