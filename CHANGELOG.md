# Changelog

## v1.1.4

- Hide the selected upstream-gateway client status on functional gateway instances; preserve it for normal nodes and keep the gateway's own health and announcement status separate.

## v1.1.3

- Keep healthy gateway advertisements stable during bounded, unchanged runtime maintenance; startup, configuration changes and failures remain fail-closed.
- Measure backbone traffic from kernel-owned interfaces rather than transient enrollment status; mark counter-membership changes as gaps instead of artificial throughput spikes.
- Add maintenance-lease, admission, counter-composition and isolated 143-peer regression coverage.

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
