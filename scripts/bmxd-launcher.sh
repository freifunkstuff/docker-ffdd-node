#!/bin/sh
set -eu

BMXD_ENV_FILE="${BMXD_ENV_FILE:-/run/freifunk/bmxd/bmxd.env}"

wait_for_file() {
    while [ ! -f "$1" ]; do
        sleep 1
    done
}

wait_for_interface() {
    while ! ip link show "$1" >/dev/null 2>&1; do
        sleep 1
    done
}

wait_for_file "$BMXD_ENV_FILE"
# shellcheck disable=SC1090
. "$BMXD_ENV_FILE"

: "${BMXD_PRIMARY_IP:?missing BMXD_PRIMARY_IP}"
: "${BMXD_DAEMON_RUNTIME_DIR:?missing BMXD_DAEMON_RUNTIME_DIR}"
: "${BMXD_PRIMARY_INTERFACE:?missing BMXD_PRIMARY_INTERFACE}"
: "${BMXD_FASTD_INTERFACE:?missing BMXD_FASTD_INTERFACE}"
: "${BMXD_BACKBONE_INTERFACES:?missing BMXD_BACKBONE_INTERFACES}"
: "${BMXD_GATEWAY_SCRIPT:?missing BMXD_GATEWAY_SCRIPT}"
: "${BMXD_POLICY_RULE_TO:?missing BMXD_POLICY_RULE_TO}"
: "${BMXD_POLICY_RULE_PRIORITY:?missing BMXD_POLICY_RULE_PRIORITY}"
: "${BMXD_POLICY_RULE_TABLE:?missing BMXD_POLICY_RULE_TABLE}"

mkdir -p "$BMXD_DAEMON_RUNTIME_DIR"

if ! ip link show "$BMXD_PRIMARY_INTERFACE" >/dev/null 2>&1; then
    brctl addbr "$BMXD_PRIMARY_INTERFACE"
fi

ip link set dev "$BMXD_PRIMARY_INTERFACE" up
ip addr flush dev "$BMXD_PRIMARY_INTERFACE" >/dev/null 2>&1 || true
ip addr add "$BMXD_PRIMARY_IP/32" dev "$BMXD_PRIMARY_INTERFACE"

for backbone_if in $BMXD_BACKBONE_INTERFACES; do
    wait_for_interface "$backbone_if"
done

while ip rule del pref "$BMXD_POLICY_RULE_PRIORITY" >/dev/null 2>&1; do
    :
done
ip rule add pref "$BMXD_POLICY_RULE_PRIORITY" to "$BMXD_POLICY_RULE_TO" lookup "$BMXD_POLICY_RULE_TABLE"

"$BMXD_GATEWAY_SCRIPT" init || true

set -- \
    --runtime_dir "$BMXD_DAEMON_RUNTIME_DIR" \
    --no_fork 0 \
    --network "$BMXD_MESH_NETWORK" \
    --netid "$BMXD_NETID" \
    --only_community-gw "$BMXD_ONLY_COMMUNITY_GW" \
    --gateway_hysteresis "$BMXD_GATEWAY_HYSTERESIS" \
    --path_hysteresis "$BMXD_PATH_HYSTERESIS" \
    --script "$BMXD_GATEWAY_SCRIPT" \
    -r "$BMXD_ROUTING_CLASS" \
    --hop_penalty "$BMXD_HOP_PENALTY" \
    --lateness_penalty "$BMXD_LATENESS_PENALTY" \
    --wireless_ogm_clone "$BMXD_WIRELESS_OGM_CLONE" \
    --udp_data_size "$BMXD_UDP_DATA_SIZE" \
    --ogm_interval "$BMXD_OGM_INTERVAL" \
    --purge_timeout "$BMXD_PURGE_TIMEOUT"

if [ -n "${BMXD_PREFERRED_GATEWAY:-}" ]; then
    set -- "$@" -p "$BMXD_PREFERRED_GATEWAY"
fi

set -- "$@" --dev="$BMXD_PRIMARY_INTERFACE" /linklayer 0

for backbone_if in $BMXD_BACKBONE_INTERFACES; do
    set -- "$@" --dev="$backbone_if" /linklayer 1
done

exec bmxd "$@"
