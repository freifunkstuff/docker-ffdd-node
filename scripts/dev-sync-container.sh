#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

mode="${1:-ui}"

case "$mode" in
  ui|full)
    ;;
  *)
    echo "usage: ./scripts/dev-sync-container.sh [ui|full]"
    exit 1
    ;;
esac

sync_file() {
  src="$1"
  dest="$2"
  file_mode="${3:-}"

  if [ -n "$file_mode" ]; then
    cat "$src" | docker exec -i "$container_id" sh -lc "cat > '$dest' && chmod $file_mode '$dest'"
    return
  fi

  cat "$src" | docker exec -i "$container_id" sh -lc "cat > '$dest'"
}

container_id="$(docker-compose ps -q dockernode)"
if [ -z "$container_id" ]; then
  echo "dockernode container not running"
  exit 1
fi

sync_file config/nginx.conf /etc/nginx/nginx.conf
sync_file ../files/common/usr/lib/license/agreement-de.txt /usr/local/share/freifunk/agreement-de.txt
sync_file ../files/common/usr/lib/license/pico-de.txt /usr/local/share/freifunk/pico-de.txt

if [ "$mode" = "full" ]; then
  sync_file config/defaults.yaml /usr/local/share/freifunk/defaults.yaml
  sync_file scripts/node_config.py /usr/local/bin/node_config.py 755
  sync_file scripts/backbone_runtime.py /usr/local/bin/backbone_runtime.py 755
  sync_file scripts/registrar.py /usr/local/bin/registrar.py 755
  sync_file scripts/sysinfo.py /usr/local/bin/sysinfo.py 755
  sync_file scripts/wireguard_status.py /usr/local/bin/wireguard_status.py 755
  sync_file scripts/bmxd-launcher.sh /usr/local/bin/bmxd-launcher.sh 755
  sync_file scripts/bmxd-gateway.py /usr/lib/bmxd/bmxd-gateway.py 755
  sync_file scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh 755
  sync_file scripts/runit/registrar/run /etc/service/registrar/run 755
  sync_file scripts/runit/sysinfo/run /etc/service/sysinfo/run 755
  sync_file scripts/runit/wireguard/run /etc/service/wireguard/run 755
  sync_file scripts/runit/fastd/run /etc/service/fastd/run 755
  sync_file scripts/runit/bmxd/run /etc/service/bmxd/run 755
  sync_file scripts/runit/nginx/run /etc/service/nginx/run 755
fi

if [ ! -d ui/node_modules ]; then
  ( cd ui && npm install --no-audit --no-fund )
fi
( cd ui && npm run build )

( cd ui/dist && tar -cf - . ) | docker exec -i "$container_id" sh -lc 'mkdir -p /usr/local/share/freifunk/ui /run/freifunk/www/ui && rm -rf /usr/local/share/freifunk/ui/* /run/freifunk/www/ui/* && tar -xf - -C /usr/local/share/freifunk/ui && cp -a /usr/local/share/freifunk/ui/. /run/freifunk/www/ui/'
docker-compose exec -T dockernode sh -lc 'mkdir -p /run/freifunk/www/licenses && rm -f /run/freifunk/www/licenses/license.txt && ln -snf /usr/local/share/freifunk/agreement-de.txt /run/freifunk/www/licenses/agreement-de.txt && ln -snf /usr/local/share/freifunk/pico-de.txt /run/freifunk/www/licenses/pico-de.txt && ln -snf /usr/local/share/freifunk/gpl2.txt /run/freifunk/www/licenses/gpl2.txt && ln -snf /usr/local/share/freifunk/gpl3.txt /run/freifunk/www/licenses/gpl3.txt'

if [ "$mode" = "full" ]; then
  docker-compose exec -T dockernode sh -lc 'nginx -t && sv restart registrar && sv restart sysinfo && sv restart wireguard && sv restart fastd && sv restart bmxd && sv restart nginx && sleep 1 && sv status registrar && sv status sysinfo && sv status wireguard && sv status fastd && sv status bmxd && sv status nginx'
else
  docker-compose exec -T dockernode sh -lc 'nginx -t >/dev/null && sv status nginx'
fi

echo "sync complete ($mode)"
