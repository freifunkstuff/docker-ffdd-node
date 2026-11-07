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

## Temporärer Betrieb und reine Mesh-Nodes

`NODE_REGISTRATION_MODE=registered` bleibt der Default. Mit
`NODE_REGISTRATION_MODE=temporary` wird ohne Registry-Anfragen eine temporäre
FFDD-ID von 900 bis 999 verwendet. Die Firmware-Formel bleibt erhalten:
`900 + ((ganze Uptime-Sekunden + erste sechs Nichtnull-Ziffern des MD5 von ip addr) % 100)`.
Die Uptime wird an Container angepasst: Host-Uptime minus Startzeit von Container-
PID 1. Das Ergebnis liegt separat unter `temporary.node_id` im persistenten
Zustand und wird beim Neustart wiederverwendet. Eine vorhandene reguläre
Registrierung wird nicht überschrieben; die UI kennzeichnet temporären Betrieb.
Der kleine Bereich ist nicht global reserviert und kann kollidieren. Ein Wechsel
auf registrierten Betrieb erfolgt ausdrücklich über Modusänderung und Neustart.

`BACKBONE_ENABLED=0` deaktiviert alle WireGuard-/fastd-Backbone-Verbindungen,
Peer-API-Abfragen und deren Konfigurationsanforderungen. Die Node kann dann
allein über `MESH_INTERFACES` teilnehmen. Leere Peer-Strings allein reichen dafür
nicht, weil sie ansonsten die Image-Defaults verwenden.

## Rollenmeldung, Modell und Container-Laufzeit

`NODE_TYPE=node` kennzeichnet Router/Offloader gegenüber Karte und Collector.
`NODE_TYPE=server` bleibt aus Kompatibilitätsgründen der Default für bisherige
Server-Deployments. Das ist eine Meldung, keine Aktivierung eines Internet-Exits.
Der Collector behandelt Legacy-Server besonders (Gatewaydarstellung und keine
Standortmarkierung); ein Offloader mit Koordinaten muss daher ausdrücklich `node`
melden. Registrierungsmodus und Rolle sind voneinander unabhängig.

Das Gerätemodell ist standardmäßig `docker-alpine`. `SYSINFO_BOARD`,
`SYSINFO_MODEL` und `SYSINFO_MODEL2` erlauben eine explizite Containerbeschreibung;
der Collector bevorzugt das nichtleere `model2`. Beispielsweise meldet ein
Kubernetes-Offloader `FFDD Docker-Node (Kubernetes)`, nicht die alte VM-Hardware.
CPU-Beschreibung bevorzugt den tatsächlichen CPU-Modelltext; Last und CPU-Anzahl
bleiben derzeit hostbezogen und sind keine Pod-CPU-Auslastungsmessung.

WireGuard-Datenzähler werden aus den konfigurierten `tbb_wgN`-IPIP-Interfaces
aggregiert als `tbb_wg_rx/tx` veröffentlicht. Das verschlüsselte Underlay `tbbwg`
wird nicht zusätzlich gezählt; fastd bleibt separat. Konfigurierte beliebig
benannte Mesh-Interfaces werden als LAN-Meshlinks klassifiziert.

Der Entrypoint begrenzt Soft-/Hard-`nofile` auf höchstens 65536 und erhält
strengere Operatorlimits. Das verhindert sehr lange fastd-v23-Startzeiten bei
modernen Runtime-Defaults von über einer Milliarde Dateideskriptoren; es gilt
auch für explizite Entrypoint-Kommandos und alle Runit-Kindprozesse.

## Vorhandene Mesh-Interfaces und lokaler VLAN-Test

`MESH_INTERFACES` ist eine durch Leerzeichen getrennte Liste bereits im Container
vorhandener Mesh-Interfaces. Die Node erstellt keine VLANs oder externen Bridges.
Sie setzt FFDD-Mesh-Adresse, `/16`, Broadcast `10.255.255.255`, MTU 1200 und
bindet die Interfaces mit `/linklayer 1` in bmxd ein. Der Registrar kontrolliert
sie zwischen den Registrierungen alle fünf Sekunden auf Verfügbarkeit und
repariert entfernte/wiederkehrende Interfaces. Der Status steht unter
`/run/freifunk/bmxd/mesh-interfaces.json`.

Die normale Default-Route bleibt Internet-Uplink für eigenen Verkehr. Interfaces
mit einer Default-Route werden nicht als Mesh-Anschluss akzeptiert. Eine eigene
Forwarding-Chain erlaubt Transit ausschließlich zwischen Mesh- und
Backbone-Interfaces; die gewöhnliche Uplink-Schnittstelle ist kein Exit. Es
werden keine globalen Firewall-Chains geleert. Mesh-Deployments benötigen eine
**eigene Netzwerk-Namespace**, nicht `network_mode: host`, sowie `NET_ADMIN` und
vom Runtime gesetztes `net.ipv4.ip_forward=1`. Docker stellt `/proc/sys` innerhalb
des Containers schreibgeschützt bereit; dies wird daher über Compose gesetzt,
nicht durch zusätzliche Privilegien oder Host-Sysctl-Änderungen umgangen. Der
Entrypoint nimmt die bereitgestellten Mesh-Links zunächst herunter, installiert
die Schutzregeln und lässt erst dann die Mesh-L3-Konfiguration starten.
Fehlgeschlagene Schutzregeln verhindern den Start.

INPUT/OUTPUT-Regeln trennen zusätzlich die Node-Adressen: Aus dem Mesh sind nur
Mesh-Protokoll, ICMP-Diagnose und HTTP auf den FFDD-Adressen erlaubt. Die
Docker-/Uplink-Adresse und andere lokale Dienste sind von dort nicht erreichbar.
Die Web-UI bleibt auf Port 80 über den Mesh-Anschluss und über den normalen
Docker-/Managementanschluss verfügbar. Fehlgeroutete lokale Pakete in private
Netze über Mesh und Pakete mit Mesh-Quelladresse über den Uplink werden gesperrt.
IPv6-Mesh ist noch nicht unterstützt und wird explizit gesperrt. Diese Variante
bietet weder Client-DHCP noch eine neue Internet-Ausleitung an.

Für ein lokal bereits bis zum PC transportiertes VLAN liegt ein Compose-Override
bei. Beispiel für VLAN 1062 an Host-Interface `enp6s0`:

```dotenv
MESH_PARENT_INTERFACE=enp6s0.1062
HTTP_BIND_ADDRESS=127.0.0.1
HTTP_PORT=8080
```

```sh
docker compose -f docker-compose.yaml -f docker-compose.mesh.yaml config --quiet
docker compose -f docker-compose.yaml -f docker-compose.mesh.yaml up -d --build
```

Der Docker-macvlan-Treiber stellt daraus das Container-Interface `mesh0` bereit;
das normale Bridge-Netz hat weiterhin Vorrang für die Default-Route. Docker
vergibt auf dem macvlan weder IP-Adresse noch Default-Gateway. Die Node leitet
ihre Mesh-Adresse aus ihrer persistenten registrierten Identität ab. Die
Knotennummer/Identität muss vor dem Anschluss an ein vorhandenes Mesh passen;
keine fremde Nummer verwenden und nicht dieselbe Identität gleichzeitig auf
mehreren Nodes betreiben.

Die Variante benötigt einen aktuellen Docker Engine/Compose mit `enable_ipv4`,
`interface_name` und `gw_priority`. Docker kann die Trunk-Subschnittstelle
`enp6s0.1062` automatisch erstellen. Der Switchport muss VLAN 1062 getaggt zum PC
transportieren. Host und macvlan-Container können auf dem macvlan standardmäßig
nicht direkt miteinander sprechen; die lokale UI ist stattdessen über
`http://127.0.0.1:8080/` erreichbar. Der physische Host-Uplink und seine
Default-Route werden nicht durch die Node umkonfiguriert.

## Optionaler LAN-/Uplink-Zugriff auf FFDD

Standardmäßig bleibt `LAN_MESH_ACCESS_ENABLED=false`: Der normale Uplink ist
kein Mesh-Clientanschluss und kein Internet-Exit. Die gezielte Freigabe wird
explizit aktiviert, zum Beispiel:

```dotenv
LAN_MESH_ACCESS_ENABLED=true
LAN_MESH_ACCESS_SOURCES=192.168.10.0/24 192.168.20.5/32
```

`LAN_MESH_ACCESS_SOURCES` ist bei aktivierter Funktion zwingend erforderlich:
eine durch Leerzeichen getrennte Liste explizit vertrauenswürdiger, kanonischer
IPv4-CIDRs (Netzadresse mit Präfix, einzelne Hosts mit `/32`). Die Quellen müssen
den am Container tatsächlich sichtbaren Quelladressen entsprechen. Docker-NAT
kann diese verändern; nicht vorsorglich größere Netze freigeben. Auch öffentliche
Unicast-Netze können ausdrücklich vertraut werden; RFC1918 ist keine Voraussetzung.
Die Eingangs-Schnittstelle wird automatisch über die IPv4-Default-Route der
MAIN-Routingtabelle bestimmt, nicht über einen fest verdrahteten Namen wie `eth0`.

Das einzige Zielnetz ist fest `10.200.0.0/16` und nicht konfigurierbar. Der Zugriff
verwendet die primäre FFDD-Adresse dieser Node als SNAT-Quelle, damit Rückverkehr
ohne Bekanntmachung der LAN-Netze im Mesh funktioniert. Dies ist keine Freigabe
für Internet-Ausleitung, allgemeinen Uplink-Transit oder neue Mesh-Initiierungen
ins LAN. LAN-Clients bzw. deren Router benötigen eine Route für `10.200.0.0/16`
über den erreichbaren Container-Uplink; die Node ändert keine Host-/LAN-Routen.
Die normalen Mesh-/Uplink-Schutzregeln bleiben für sonstigen Verkehr bestehen.
Nur am Uplink ursprünglich eingegangene erlaubte Verbindungen werden per
Conntrack-Markierung autorisiert. SNAT und die Rückrichtung verlangen diese
Markierung; eine aus dem Mesh gefälschte LAN-Quelladresse allein reicht nicht.
Die Freigabe für Antworten ist auf REPLY und ESTABLISHED/RELATED begrenzt.
Auch Antworten des lokalen Webservers auf seine eigene Mesh-Adresse sind damit
vom LAN aus möglich, ohne allgemeine Mesh-Quelladressen zum Uplink durchzulassen.

Explizite isolierte Pakettests (keine echten Netze, keine Registry):

```sh
python3 tests/integration_mesh_isolation.py ffdd-node:local-mesh
python3 tests/integration_lan_mesh_access.py ffdd-node:local-mesh
python3 tests/integration_runtime_capabilities.py ffdd-node:local-mesh
```

Der LAN-Test prüft Default aus, aktiviertes SNAT ohne LAN-Rückroute auf der
Gegenstelle, nicht erlaubte Quellen/Ziele, Mesh→LAN-Sperre, gefälschte LAN-Quellen
aus dem Mesh und erneutes Deaktivieren. Er verwendet vorab erfolgreiche
Positivkontrollen, damit bloß fehlende Routen nicht als Firewall-Erfolg zählen.

Ungültige CIDRs, Hostbits in Netzpräfixen, IPv6, `0.0.0.0/0`, Loopback,
Link-local, Multicast und reservierte/Broadcast-Bereiche werden abgewiesen.
Auch jede Überlappung mit FFDD `10.200.0.0/15` oder dem Transportnetz
`10.203.0.0/16` ist verboten, einschließlich größerer umschließender Präfixe:
LAN-Vertrauen darf kein Spoofing von Mesh-Quelladressen ermöglichen. Nichtleere
Quelllisten werden auch bei deaktivierter Funktion validiert. Ein Quellpräfix
ist eine Vertrauensentscheidung des Betreibers, kein Authentizitätsnachweis;
es sollte so klein wie möglich sein. Es gibt weder Client-DHCP noch eine neue
Internet-Gateway-Ankündigung.

## Gateway-Präferenz und Anzeige

`BMXD_PREFERRED_GATEWAY=10.200.200.21` übergibt die primäre Mesh-IP als bmxd-
Präferenz (`-p`). Ein geeignetes erreichbares bevorzugtes Gateway gewinnt gegen
bessere Alternativen; fehlt es oder zieht sein Gateway-Angebot zurück, kann bmxd
auf ein anderes Gateway ausweichen. Die Nodes-Ansicht zeigt **Bevorzugt** (★) und
**Aktiv** (●) unabhängig voneinander zwischen den Spalten **Usage** und **BRC**. Eine
bevorzugte Adresse ist nicht automatisch der aktuell gewählte Exit; die aktive
Gateway-Zeile erhält keinen besonderen Hintergrund mehr.

`BMXD_ONLY_COMMUNITY_GATEWAYS=true` (Default) wählt nur Gateway-Ankündigungen mit
dem bmxd-Merkmal `COMMUNITY_GATEWAY`. Mit `false` werden auch andere angebotene
Exits berücksichtigt. Die Einstellung akzeptiert auch `1`/`0`; ungültige Werte
werden beim Konfigurationscheck abgewiesen. Intern wird bmxd `1` bzw. `0` übergeben.
Das Merkmal kennzeichnet die im Protokoll für Community-übergreifende
Default-Ausleitung vorgesehenen Gateways. Es ist kein
Filter nach `NODE_COMMUNITY` und kein Authentizitäts-/Vertrauensnachweis. Ein
gewöhnlicher Router kann Internet anbieten, ohne dieses Merkmal zu setzen;
solche Kandidaten bleiben im Gateway-Verzeichnis sichtbar, werden mit diesem
Filter aber nicht ausgewählt. Die Präferenz überstimmt diesen Filter nicht.

Eine Auswahl allein schaltet weder die eigene Uplink-Freigabe noch eine
Client-Ausleitung ein. Die tatsächliche Exit-Funktion bleibt separat abzusichern.

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
