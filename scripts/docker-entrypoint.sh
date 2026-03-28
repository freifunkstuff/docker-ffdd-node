#!/bin/sh
set -eu

ensure_ip_forward() {
    current_value="$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || printf '')"
    if [ "$current_value" = "1" ]; then
        return 0
    fi

    if ! sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1; then
        printf '%s [entrypoint] failed to enable net.ipv4.ip_forward\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >&2
        exit 1
    fi

    current_value="$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || printf '')"
    if [ "$current_value" != "1" ]; then
        printf '%s [entrypoint] net.ipv4.ip_forward is not enabled after sysctl attempt\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" >&2
        exit 1
    fi
}

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

ensure_ip_forward

if [ "${SKIP_FAILFAST:-0}" != "1" ]; then
    python3 /usr/local/bin/registrar.py --checkconfig
    python3 /usr/local/bin/sysinfo.py --checkconfig
fi

# Dynamische App-Hooks
if [ -d /etc/docker-entrypoint.d ]; then
    for hook in /etc/docker-entrypoint.d/*; do
        if [ -x "$hook" ]; then
            "$hook" || exit 1
        fi
    done
fi

if [ "${REGISTRAR_ONLY:-0}" = "1" ]; then
    exec python3 /usr/local/bin/registrar.py
fi

if [ -d /usr/local/share/freifunk/ui ]; then
    mkdir -p /run/freifunk/www
    rm -rf /run/freifunk/www/ui
    cp -a /usr/local/share/freifunk/ui /run/freifunk/www/ui
fi

mkdir -p /run/freifunk/www/licenses
rm -f /run/freifunk/www/licenses/license.txt
ln -snf /usr/local/share/freifunk/agreement-de.txt /run/freifunk/www/licenses/agreement-de.txt
ln -snf /usr/local/share/freifunk/pico-de.txt /run/freifunk/www/licenses/pico-de.txt
ln -snf /usr/local/share/freifunk/gpl2.txt /run/freifunk/www/licenses/gpl2.txt
ln -snf /usr/local/share/freifunk/gpl3.txt /run/freifunk/www/licenses/gpl3.txt

exec runsvdir -P /etc/service
