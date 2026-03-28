#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ "${SKIP_FAILFAST:-0}" != "1" ]; then
    FAILFAST_CHECKS="${FAILFAST_CHECKS:-python3 /usr/local/bin/registrar.py --checkconfig
python3 /usr/local/bin/sysinfo.py --checkconfig}"
    old_ifs="$IFS"
    IFS='
'
    for check in $FAILFAST_CHECKS; do
        [ -n "$check" ] || continue
        sh -c "$check"
    done
    IFS="$old_ifs"
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
