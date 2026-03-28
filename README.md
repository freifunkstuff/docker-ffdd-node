# Freifunk Dresden/Leipzig Docker-Servernode

Dieser Ordner enthält einen containerisierten Freifunk-Servernode mit:

- `registrar` (Registrierung, Node-ID, fastd/bmxd Runtime-Dateien)
- `sysinfo` (30s-Refresh von `sysinfo.json` + `nodes.json`)
- `fastd` + `bmxd`
- `nginx` (Port 80, UI + JSON-Endpunkte)

## Quickstart

## Voraussetzungen

- Docker + Docker Compose Plugin
- Linux-Host mit `/dev/net/tun`

## 1) Konfiguration anlegen

```bash
cd dockernode
cp .env.example .env
```

Mindestens diese Werte in `.env` setzen:

- `NODE_CONTACT_EMAIL`
- `NODE_NAME`
- `NODE_COMMUNITY` (`Dresden` oder `Leipzig`)
- `NODE_GPS_LATITUDE`
- `NODE_GPS_LONGITUDE`

Optional:

- `FASTD_PEERS` leer lassen → Defaults aus `config/defaults.yaml`
- `NODE_REGISTRATION_URL` leer lassen → Defaults aus `config/defaults.yaml`
- `REGISTRAR_INTERVAL` muss zwischen `3600` und `21600` Sekunden liegen (1–6h)

## 2) Bauen

```bash
docker compose build dockernode
```

## 3) Starten

```bash
docker compose up -d dockernode
```

## 4) Erreichbarkeit prüfen

```bash
docker compose ps
curl -sSf http://127.0.0.1/sysinfo.json | head
curl -sSf http://127.0.0.1/nodes.json | head
curl -sSf http://127.0.0.1/ui/ | head
```

---

## Betrieb

## Services (runit)

Im Container laufen getrennte Dienste:

- `registrar`
- `sysinfo`
- `fastd`
- `bmxd`
- `nginx`

Status prüfen:

```bash
docker compose exec -T dockernode sh -lc 'sv status registrar; sv status sysinfo; sv status fastd; sv status bmxd; sv status nginx'
```

Einzelnen Dienst neu starten:

```bash
docker compose exec -T dockernode sv restart sysinfo
```

Logs ansehen:

```bash
docker compose logs -f dockernode
```

## Persistenz

Persistente Daten liegen auf dem Host in:

- `./data/node.yaml`

Darin werden u. a. gespeichert:

- `registration.node_id`
- `registration.register_key`
- `fastd.secret`

Flüchtige Laufzeitdaten liegen im Container unter:

- `/run/freifunk/fastd`
- `/run/freifunk/bmxd`
- `/run/freifunk/sysinfo`
- `/run/freifunk/www`

## Wichtige Endpunkte

- `GET /sysinfo.json`
- `GET /sysinfo-json.cgi`
- `GET /nodes.json`
- `GET /ui/`
- `GET /licenses/*` (Rechtstexte)

---

## Wichtige Befehle (insb. `bmxd -c`)

Ausführen im Container:

```bash
docker compose exec -T dockernode <kommando>
```

## bmxd-Status und Routing

```bash
bmxd -c status
```

- Kurzstatus des Daemons (Uptime/Node/IP/Last)

```bash
bmxd -c options
```

- Effektive bmxd-Parameter

```bash
bmxd -c --links
```

- Direkte Nachbarn + Linkqualität (`RTQ`, `RQ`, `TQ`)

```bash
bmxd -c --gateways
```

- Bekannte Gateways + bevorzugtes/ausgewähltes Gateway

```bash
bmxd -c --originators
```

- Originator-Tabelle (Mesh-Routen, Next-Hop, BRC)
