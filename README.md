# Freifunk Dresden/Leipzig Docker-Servernode

Dieser Ordner enthält einen containerisierten Freifunk-Servernode mit:

- `registrar` (Registrierung, Node-ID, fastd/bmxd Runtime-Dateien)
- `sysinfo` (30s-Refresh von `sysinfo.json` + `nodes.json`)
- `wireguard` (Status-Logging für WireGuard-Peers)
- `fastd` + `bmxd`
- `nginx` (Port 80, UI + JSON-Endpunkte)

## Quickstart

## Voraussetzungen

- Docker + Docker Compose Plugin
- Linux-Host mit `/dev/net/tun`
- Der Container muss `net.ipv4.ip_forward=1` aktivieren können

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

- `BACKBONE_PEERS` leer lassen → Defaults aus `config/defaults.yaml`
- `NODE_REGISTRATION_URL` leer lassen → Defaults aus `config/defaults.yaml`
- `REGISTRAR_INTERVAL` muss zwischen `3600` und `21600` Sekunden liegen (1–6h)

### TLS der Registrierung

Die Registrierung erfordert HTTPS und prüft immer Zertifikatskette und Hostnamen.
Für eigene `NODE_REGISTRATION_URL`-Hosts gelten standardmäßig die System-CAs.
Nur für den exakten Default-Host `selfsigned.register.freifunk-dresden.de` wird
stattdessen die mitgelieferte FFDD-CA verwendet. Es gibt keinen unsicheren
Fallback und keine Option zum Abschalten der Zertifikatsprüfung.

Die öffentliche CA in [config/ca-root-ffdd.crt](config/ca-root-ffdd.crt) stammt aus
[der Firmware](../ffdd-node/files/common/etc/ssl/certs/ca-root-ffdd.crt) und ist
bytegleich mit [der Server-Konfiguration](../ffdd-server/salt/freifunk/base/ddmesh/etc/ssl/certs/ca-root-ffdd.crt).
SHA-256-Fingerprint: `FA:A5:60:C0:D4:8B:7B:19:CA:BD:5E:EF:94:06:BF:2E:2E:FB:97:37:FC:45:DC:62:3A:AA:8E:68:82:8D:71:D1`.
Dies ist ein expliziter CA-Vertrauensanker, kein Pin des Server-Zertifikats.
Die ältere FFDD-Root-CA enthält keine `keyUsage`-Erweiterung. Nur für dieses
mitgelieferte Default-Bundle wird daher `VERIFY_X509_STRICT` deaktiviert;
Zertifikatskette, Gültigkeit und Hostname werden weiterhin geprüft. Für eigene
Bundles und System-CAs bleiben die Python-Standardprüfungen unverändert.

Mit `NODE_REGISTRATION_CA_FILE=/data/registration-ca.pem` kann ein eigenes
PEM-CA-Bundle gewählt werden. Der Pfad gilt **im Container**; die Datei muss
lesbar sein, etwa durch einen zusätzlichen read-only Bind-Mount:

```yaml
services:
  dockernode:
    volumes:
      - ./registration-ca.pem:/data/registration-ca.pem:ro
```

Ein explizites Bundle ersetzt für diese Anfrage die System- bzw. Default-CA.
Fehlende, unlesbare oder ungültige CA-Dateien führen zum Abbruch; sie schalten
keine Prüfung ab. CA-Dateien nur aus einer unabhängig vertrauenswürdigen Quelle
beziehen, niemals beim fehlgeschlagenen TLS-Verbindungsaufbau ungeprüft übernehmen.
Die Registrierungs-URL enthält einen geheimen Schlüssel und gehört nicht in Logs.
Redirects zu HTTP oder einem anderen Host/Port werden deshalb ebenfalls abgewiesen.

Bei der Live-Prüfung des aktuellen Default-Registrars wurde die Kette erfolgreich
gegen diese CA verifiziert. Das Server-Zertifikat gilt vom 24.01.2021 bis
22.01.2031; sein SHA-256-Fingerprint lautet
`76:98:0C:B1:DB:BE:42:95:85:69:BC:5B:E3:CC:AB:4B:0A:EE:27:63:64:0D:06:34:EA:A9:F8:00:25:A7:F8:79`.
Das Legacy-Zertifikat hat keinen SAN; der Hostname wird über den passenden CN
geprüft. Dies dokumentiert den Prüfstand und ist kein zusätzlicher Leaf-Pin.

## 2) Bauen

```bash
docker compose build dockernode
```

## CI Release (Tag -> Image-Tag)

Bei einem Git-Tag-Push baut GitHub Actions automatisch ein Image und pusht es nach GHCR:

- `ghcr.io/freifunkstuff/ffdd-node:<tag>`
- Push auf `master` erzeugt zusätzlich: `ghcr.io/freifunkstuff/ffdd-node:master`

Beispiel:

```bash
git tag v1.0.1
git push origin v1.0.1
```

Ergebnis:

- `ghcr.io/freifunkstuff/ffdd-node:v1.0.1`

Pull-Beispiel:

```bash
docker pull ghcr.io/freifunkstuff/ffdd-node:master
docker pull ghcr.io/freifunkstuff/ffdd-node:v1.0.1
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
- `wireguard`
- `fastd`
- `bmxd`
- `nginx`

Status prüfen:

```bash
docker compose exec -T dockernode sh -lc 'sv status registrar; sv status sysinfo; sv status wireguard; sv status fastd; sv status bmxd; sv status nginx'
```

Einzelnen Dienst neu starten:

```bash
docker compose exec -T dockernode sv restart sysinfo
```

Logs ansehen:

```bash
docker compose logs -f dockernode
```

## Wiederanlauf und WireGuard-Endpunkte

Der Registrar aktualisiert WireGuard-Peers bei jedem erfolgreichen Reconcile
in-place. Dadurch wird der DNS-Name des Endpunkts auch bei unverändertem
Konfigurationstext erneut aufgelöst. Das Intervall ist `REGISTRAR_INTERVAL`
(Standard eine Stunde, konfigurierbar auf 1–6 Stunden); dies ist kein
minütlicher DNS-Refresh wie in der Router-Firmware. Registrierung und
Peer-Validierung müssen für einen vollständigen Durchlauf erreichbar sein.

Bestehende gewünschte Peers werden nicht mehr vorsorglich entfernt. Erst nach
erfolgreichen Updates werden veraltete Peers gelöscht. Ein `apply-pending`-Marker
hält auch unterbrochene Interface-Reparaturen wiederholbar; die Runtime-Umgebung
wird erst nach erfolgreicher Anwendung veröffentlicht. Unveränderte, erfolgreich
angewendete Durchläufe erzeugen keine IPIP-Tunnel neu.

Für fastd und bmxd wird die zuletzt erfolgreich geladene Konfiguration separat
als `service-applied.sha256` im jeweiligen Runtime-Verzeichnis quittiert.
Fehlgeschlagene Neustarts werden erneut versucht, auch wenn nur der Registrar
neu gestartet wurde. Eine Topologie-Reparatur invalidiert die bmxd-Quittierung.
Fastd-Verbindungsmarker werden beim Daemonstart sowie bei Interface-up/down
bereinigt, damit ein Absturz keine alten „connected“-Zustände hinterlässt.

## Persistenz

Persistente Daten liegen auf dem Host in:

- `./data/node.yaml`

Darin werden u. a. gespeichert:

- `registration.node_id`
- `registration.register_key`
- `fastd.secret`
- `wireguard.secret`

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
