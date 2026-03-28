#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

container_id="$(docker-compose ps -q dockernode)"
if [ -z "$container_id" ]; then
  echo "dockernode container not running"
  exit 1
fi

cat config/nginx.conf | docker exec -i "$container_id" sh -lc 'cat > /etc/nginx/nginx.conf'
cat scripts/sysinfo.py | docker exec -i "$container_id" sh -lc 'cat > /usr/local/bin/sysinfo.py && chmod 755 /usr/local/bin/sysinfo.py'
cat scripts/runit/sysinfo/run | docker exec -i "$container_id" sh -lc 'cat > /etc/service/sysinfo/run && chmod 755 /etc/service/sysinfo/run'
cat scripts/docker-entrypoint.sh | docker exec -i "$container_id" sh -lc 'cat > /usr/local/bin/docker-entrypoint.sh && chmod 755 /usr/local/bin/docker-entrypoint.sh'
cat ../files/common/usr/lib/license/agreement-de.txt | docker exec -i "$container_id" sh -lc 'cat > /usr/local/share/freifunk/agreement-de.txt'
cat ../files/common/usr/lib/license/pico-de.txt | docker exec -i "$container_id" sh -lc 'cat > /usr/local/share/freifunk/pico-de.txt'

if [ ! -d ui/node_modules ]; then
  ( cd ui && npm install --no-audit --no-fund )
fi
( cd ui && npm run build )

( cd ui/dist && tar -cf - . ) | docker exec -i "$container_id" sh -lc 'mkdir -p /usr/local/share/freifunk/ui /run/freifunk/www/ui && rm -rf /usr/local/share/freifunk/ui/* /run/freifunk/www/ui/* && tar -xf - -C /usr/local/share/freifunk/ui && cp -a /usr/local/share/freifunk/ui/. /run/freifunk/www/ui/'
docker-compose exec -T dockernode sh -lc 'mkdir -p /run/freifunk/www/licenses && rm -f /run/freifunk/www/licenses/license.txt && ln -snf /usr/local/share/freifunk/agreement-de.txt /run/freifunk/www/licenses/agreement-de.txt && ln -snf /usr/local/share/freifunk/pico-de.txt /run/freifunk/www/licenses/pico-de.txt && ln -snf /usr/local/share/freifunk/gpl2.txt /run/freifunk/www/licenses/gpl2.txt && ln -snf /usr/local/share/freifunk/gpl3.txt /run/freifunk/www/licenses/gpl3.txt'

docker-compose exec -T dockernode sh -lc 'nginx -t && sv restart nginx && sv restart sysinfo && sleep 1 && sv status nginx && sv status sysinfo'

echo "sync complete"
