# Changelog

## v1.1.2

- Prevent BMXD interface debug-buffer overflows when aggregating OGMs across many interfaces.
- Build interface debug strings only when debug output is active; retain compiler format checking.
- Add an opt-in isolated many-interface BMXD regression test.

## v1.0.1

- Verify registration HTTPS certificates and hostnames. Bundle the authoritative FFDD CA for the default registry; support custom CA bundles with `NODE_REGISTRATION_CA_FILE`.
- Reject plaintext registration and cross-origin redirects.
- Reconcile WireGuard peers in place on every successful registrar cycle, refreshing DNS endpoints without removing healthy peers first.
- Retry failed or interrupted WireGuard/IPIP changes, including partially configured existing interfaces. Publish runtime configuration only after successful application.
- Track successfully applied fastd/bmxd service generations so configuration changes and failed restarts recover across registrar process restarts.
- Clear stale fastd connection markers on daemon startup and interface up/down.
- Add 32 regression tests, bringing the suite to 93 tests; include the new tests and CA in the Docker test/build stages.

## v1.0.0

Initial tagged Docker server-node release.
