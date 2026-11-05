#!/bin/sh
set -eu

FASTD_ENV_FILE="${FASTD_ENV_FILE:-/run/freifunk/fastd/fastd.env}"
STATUS_DIR="${FASTD_STATUS_DIR:-/run/freifunk/fastd/backbone_status}"

if [ -f "$FASTD_ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$FASTD_ENV_FILE"
fi

mkdir -p "$STATUS_DIR"

interface="${INTERFACE:-${FASTD_INTERFACE:-tbb_fastd}}"
mtu="${INTERFACE_MTU:-${FASTD_MTU:-1200}}"
nonprimary_ip="${FASTD_NONPRIMARY_IP:?missing FASTD_NONPRIMARY_IP}"
mesh_prefix="${FASTD_MESH_PREFIX:-16}"
mesh_broadcast="${FASTD_MESH_BROADCAST:-10.200.255.255}"
peer_key="${PEER_KEY:-unknown}"

case "${1:-}" in
    up|down)
        # A crashed daemon may never emit disestablish. Interface lifecycle
        # events invalidate every peer marker, not just the current peer.
        for marker in "$STATUS_DIR"/* "$STATUS_DIR"/.[!.]* "$STATUS_DIR"/..?*; do
            if [ -f "$marker" ] || [ -L "$marker" ]; then
                rm -f -- "$marker"
            fi
        done
        if [ "$1" = down ]; then
            ip link set "$interface" down || true
            ip addr del "$nonprimary_ip/$mesh_prefix" broadcast "$mesh_broadcast" dev "$interface" || true
            exit 0
        fi
        ip link set "$interface" down || true
        ip link set "$interface" promisc off || true
        ip link set "$interface" multicast off mtu "$mtu"
        ip addr replace "$nonprimary_ip/$mesh_prefix" broadcast "$mesh_broadcast" dev "$interface"
        ip link set "$interface" up
        ;;
    establish)
        : > "$STATUS_DIR/$peer_key"
        ;;
    disestablish)
        rm -f "$STATUS_DIR/$peer_key"
        ;;
    connect|verify|*)
        ;;
esac

exit 0