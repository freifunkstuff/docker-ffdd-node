# Implementierung eines Freifunk Dresden / Freifunk Leipzig Server-Nodes

## TODO

* Health-Check, wann failen wir (z.b. wenn registrar weg ist?)
* Hardening, Capabilities, User
* Anleitung zum extenden - wie baue ich auf der Basis eigene Server?

## Architekturüberblick

Der Server-Node ist als einzelner Container mit klar getrennten Laufzeitrollen aufgebaut:

- eine gemeinsame Python-Config-Schicht stellt generische Loader-, Merge- und Validierungs-Helfer bereit
- `registrar` beschafft bzw. persistiert die eindeutige Node-ID, erzeugt Schlüsselmaterial, validiert WireGuard-Peers gegen die API und rendert die Laufzeitkonfiguration für `fastd`, `wireguard` und `bmxd`
- ein `sysinfo`-Dienst läuft zyklisch, rendert `sysinfo.json` in ein flüchtiges Laufzeitverzeichnis und veröffentlicht stabile Web-Pfade per Symlink
- ein `wireguard`-Dienst beobachtet den WireGuard-Status, loggt konfigurierte Peers beim Start und meldet Statuswechsel als `connected`, `stale` oder `never-seen`
- `fastd` und `wireguard` stellen Backbone-Anbindungen über getrennte Interfaces bereit
- `bmxd` läuft mit den Freifunk-Dresden-kompatiblen Parametern auf allen vom Registrator vorbereiteten Backbone-Interfaces
- `runit` überwacht diese Prozesse im Container und startet sie bei Bedarf neu
- `nginx` liefert die vom Sysinfo-Dienst publizierten Dateien auf Port 80 aus

### Komponenten

- [dockernode/scripts/docker-entrypoint.sh](dockernode/scripts/docker-entrypoint.sh) setzt beim Start `net.ipv4.ip_forward=1`, prüft den Wert hart und startet danach standardmäßig `runsvdir` mit allen unter `runit` definierten Diensten.
- [dockernode/scripts/runit/registrar/run](dockernode/scripts/runit/registrar/run) startet den Registrator zyklisch mit `--loop`.
- [dockernode/scripts/node_config.py](dockernode/scripts/node_config.py) enthält generische Helfer für Defaults, Env, Validierung und persistierten Laufzeit-State.
- [dockernode/scripts/registrar.py](dockernode/scripts/registrar.py) bringt sein eigenes Registrar-Schema mit und nutzt die generischen Helpers für Registrierung, Persistenz und Runtime-Dateien.
- [dockernode/scripts/sysinfo.py](dockernode/scripts/sysinfo.py) ist der Sysinfo-Renderer: eigenes Sysinfo-Schema, `--checkconfig`, zyklisches Rendering nach `/run/freifunk/sysinfo/sysinfo.json` und Publikation der Web-Pfade in `/run/freifunk/www`.
- [dockernode/scripts/runit/sysinfo/run](dockernode/scripts/runit/sysinfo/run) startet den Sysinfo-Dienst zyklisch mit `--loop`.
- [dockernode/scripts/wireguard_status.py](dockernode/scripts/wireguard_status.py) liest `wireguard.env`, pollt `wg show <interface> dump` und loggt Zustände für konfigurierte Peers.
- [dockernode/scripts/runit/wireguard/run](dockernode/scripts/runit/wireguard/run) startet den WireGuard-Statusdienst zyklisch mit Polling-Intervall und Stale-Schwelle.
- [dockernode/scripts/runit/fastd/run](dockernode/scripts/runit/fastd/run) startet `fastd`, sobald die vom Registrator erzeugte `fastd.conf` vorhanden ist.
- [dockernode/scripts/runit/nginx/run](dockernode/scripts/runit/nginx/run) startet `nginx` mit `daemon off` und liefert `/run/freifunk/www` auf Port 80 aus.
- [dockernode/config/nginx.conf](dockernode/config/nginx.conf) definiert die nginx-Auslieferung für JSON-Endpunkte (`/sysinfo.json`, `/sysinfo-json.cgi`, `/nodes.json`), die UI unter `/ui/` sowie statische Rechtstexte unter `/licenses/*`.
- [dockernode/scripts/bmxd-launcher.sh](dockernode/scripts/bmxd-launcher.sh) wartet auf die vom Registrator erzeugte `bmxd.env`, bereitet Interfaces und Policy Rule vor und startet anschließend `bmxd`.

### Startreihenfolge

1. Der Container startet `runit`.
2. `registrar`, `sysinfo`, `wireguard`, `fastd`, `bmxd` und `nginx` werden als getrennte Services hochgefahren.
3. `registrar` erzeugt zunächst die benötigten Laufzeitdateien unter `/run/freifunk/...`.
4. `sysinfo` rendert zyklisch die JSON-Ausgabe nach `/run/freifunk/sysinfo/sysinfo.json` und aktualisiert die Symlinks in `/run/freifunk/www`.
5. `fastd` wartet auf seine Config und startet danach nur dann aktiv, wenn der Registrator tatsächlich Fastd-Peers gerendert hat.
6. `wireguard` wartet auf die erste vom Registrator erzeugte `wireguard.env` und beginnt danach mit dem Status-Monitoring.
7. `bmxd` wartet auf seine Env-Datei und zusätzlich auf alle in `BMXD_BACKBONE_INTERFACES` eingetragenen Backbone-Interfaces.
8. `nginx` startet sofort und liefert ab dem ersten Rendering-Zyklus von `sysinfo` gültige Antworten auf Port 80.

Wichtig dabei: Der Registrator startet `fastd` und `bmxd` nicht direkt per `exec`, sondern liefert die Konfiguration, auf die deren Startskripte warten.

### Laufzeit- und Änderungsmodell

- Persistente Knotendaten liegen in `/data/node.yaml`.
- Flüchtige Laufzeitdateien liegen unter `/run/freifunk/fastd`, `/run/freifunk/wireguard`, `/run/freifunk/bmxd`, `/run/freifunk/sysinfo` und `/run/freifunk/www`.
- Der WireGuard-Dienst liest seine Konfiguration aus `/run/freifunk/wireguard/wireguard.env` und loggt Änderungen ausschließlich anhand der vom Registrator erzeugten Peer-Liste und `wg`-Live-Daten.
- Der Registrator läuft zyklisch und prüft in jedem Durchlauf, ob sich registrierungsrelevante oder gerenderte Inhalte geändert haben.
- Der Sysinfo-Dienst läuft ebenfalls zyklisch und schreibt immer den aktuellen JSON-Stand in das volatile Runtime-Verzeichnis.
- Nur bei inhaltlichen Änderungen werden Runtime-Dateien neu geschrieben.
- Im Loop-Modus löst der Registrator danach gezielt `sv restart` für `fastd` und/oder `bmxd` aus.
- Backbone-Routing zwischen mehreren Fastd-/WireGuard-Links erfolgt nicht per Bridge, sondern über `bmxd`-gesteuerte Routen in der Policy-Routing-Tabelle.

Damit ergibt sich folgende Semantik:

- Erststart: indirekt über die vom Registrator erzeugten Dateien
- spätere Änderungen: Neustart der betroffenen Dienste beim nächsten Registrator-Durchlauf
- kein permanenter File-Watcher, sondern reconcile-basierter Betrieb

### Sysinfo-Dienst und Webserver-Vertrag

Sysinfo und Webserver sind als getrennte Rollen umgesetzt: `sysinfo` schreibt, `nginx` liefert aus.

Producer (bereits implementiert):

- schreibt atomar nach `/run/freifunk/sysinfo/sysinfo.json` (temp-Datei + replace)
- schreibt zusätzlich atomar nach `/run/freifunk/sysinfo/nodes.json` für die spätere menschenlesbare Knotenansicht
- rendert zyklisch (festes Intervall 30s)
- erzeugt stabile Symlinks in `/run/freifunk/www`:
    - `/run/freifunk/www/sysinfo.json`
    - `/run/freifunk/www/sysinfo-json.cgi`
    - `/run/freifunk/www/nodes.json`
- hält das Sysinfo-JSON-Schema stabil (Root `version=17`, Block `data.*`); zusätzliche Knotendaten liegen nur in `nodes.json`

Consumer (bereits implementiert):

- Webserver auf Port 80
- Auslieferung der JSON-Endpunkte:
    - `GET /sysinfo.json` → Datei `/run/freifunk/www/sysinfo.json`
    - `GET /sysinfo-json.cgi` → Datei `/run/freifunk/www/sysinfo-json.cgi`
- `GET /nodes.json` → Datei `/run/freifunk/www/nodes.json`
- `GET /` leitet auf `GET /ui/` um
- `GET /ui/*` liefert die gebaute SPA aus
- `GET /licenses/*` liefert Rechtstexte (`agreement-de.txt`, `pico-de.txt`, `gpl2.txt`, `gpl3.txt`)
- keine eigene JSON-Generierung im Webserver, nur statische Auslieferung der vom Sysinfo-Dienst publizierten Dateien
- Read-Only-Verhalten gegenüber `/run/freifunk/sysinfo` und `/run/freifunk/www`

Damit bleiben Rendering und HTTP-Serving sauber getrennt: Sysinfo schreibt, Webserver liefert aus.

### Config- und Fail-Fast-Modell

- User-Inputs kommen ausschließlich per Env.
- [dockernode/config/defaults.yaml](dockernode/config/defaults.yaml) enthält nur Defaults für optionale oder technische Werte.
- `/data/node.yaml` enthält nur zur Laufzeit erhobene und persistierte Daten, z. B. `fastd.secret`, `registration.register_key` und `registration.node_id`.
- `registrar` und `sysinfo` bekommen jeweils eine Option `--checkconfig`, die ihr jeweiliges Dienst-Schema validiert und danach sofort beendet.
- `node_config.py` kennt absichtlich keine fachlichen Scopes mehr; die konkreten Schemas liegen direkt in den Diensten.
- Die Validierung ist damit dienstspezifisch: `registrar` prüft nur Registrierungs-, `fastd`- und `bmxd`-relevante Werte; `sysinfo` prüft Metadaten wie Kontakt, Name und GPS.
- Der Container führt vor dem Start von `runit` einen Fail-Fast-Check aus.
- Vor den eigentlichen Dienst-Checks setzt der Entrypoint `net.ipv4.ip_forward=1`, prüft den Wert erneut und bricht hart ab, wenn IP-Forwarding nicht aktivierbar ist.
- Standardmäßig werden dabei sowohl `registrar --checkconfig` als auch `sysinfo --checkconfig` ausgeführt.
- Leere Env-Werte für technische Default-Keys wie `BACKBONE_PEERS`, `NODE_REGISTRATION_URL` und `INITIAL_NODE_ID` fallen auf [dockernode/config/defaults.yaml](dockernode/config/defaults.yaml) zurück. Das ist wichtig, weil `docker compose` diese Variablen als leeren String in den Container injiziert.

Aktueller inhaltlicher Stand der Validierung:

- im `registrar`-Scope sind `NODE_REGISTRATION_URL`, `BACKBONE_PEERS`, `INITIAL_NODE_ID`, `FASTD_PORT`, `WIREGUARD_PORT`, `REGISTRAR_INTERVAL` und `BMXD_PREFERRED_GATEWAY` relevant.
- `NODE_REGISTRATION_URL` und `BACKBONE_PEERS` dürfen im Compose-Setup leer bleiben und werden dann aus [dockernode/config/defaults.yaml](dockernode/config/defaults.yaml) gezogen.
- `REGISTRAR_INTERVAL` wird semantisch auf 1 bis 6 Stunden geprüft.
- alle WireGuard-Peers werden im Reconcile-Lauf gegen die API geprüft; nur API-konsistente Peers bleiben aktiv, unvollständige oder abweichende Peers werden verworfen.
- im `sysinfo`-Schema bleiben `NODE_CONTACT_EMAIL`, `NODE_NAME`, `NODE_COMMUNITY` sowie GPS-Daten fachlich verankert.
- fehlende GPS-Angaben erzeugen dort zunächst nur eine Warnung im Log, noch kein Fail.
- `autoupdate` wird für den Dockernode immer als deaktiviert modelliert.
- technische Protokollparameter wie `tbb_fastd`, `bmx_prime`, MTU und die festen `bmxd`-Timings sind statisch im Basis-Image und nicht per Env verstellbar.

### Aktuelle Scope-Grenzen

- Der Container ist derzeit ein reiner Server-/Backbone-Knoten ohne WLAN-AP-Funktion.
- Das aktuelle Modell unterstützt mehrere Backbone-Interfaces gleichzeitig, gemischt aus `fastd` und `wireguard`.
- `nginx` liefert auf Port 80 die JSON-Endpunkte `/sysinfo.json`, `/sysinfo-json.cgi` und `/nodes.json` sowie die UI unter `/ui/` und Rechtstexte unter `/licenses/*` aus; Verzeichnis-Listing ist deaktiviert.

## Anforderungen

Es ist essenziell, dass sich der Node an die Standards des Freifunk Dresden Netzwerkes hält. Diese sind in folgenden Quellen beschrieben:

* https://wiki.freifunk-dresden.de/index.php/Technische_Information
* https://wiki.freifunk-dresden.de/index.php/Knoten_Spezifikation
* https://wiki.freifunk-dresden.de/index.php/Sysinfo-json

Insbesondere relevant ist die Server-Spezifikation:

> Ein Freifunk Server ist ein Freifunk Knoten, der selber nicht als Hotspot arbeitet. Falls er WLAN anbietet und nicht die Spezifikationen für ein Freifunk Hotspot erfüllt, so darf dieser auch nicht als solcher verstanden werden können. Es ist möglich, dass sich ein solcher Server per WLAN Adhoc verbindet, darf dann aber niemals "Freifunk Dresden" in der SSID enthalten (auch nicht für Adhoc). Denn findet jemand eine SSID mit der Bezeichnung "Freifunk Dresden" und übersieht, dass nur Adhoc verfügbar ist, kann sich dieser nicht per Accesspoint Mode verbinden.
>
> Ein Freifunk Server kann Dienste im Netz anbieten, muss aber nicht als Freifunk-Hotspot arbeiten. Der Server kann dann auch ohne WLAN via Backbone ans Netz angeschlossen sein.
>
> **Spezifikation**
>
> 1. Darf in der WLAN SSID kein "Freifunk Dresden", "Freifunk Meißen" oder andere regionale Bezeichnungen enthalten. Weder im Accesspoint Mode, noch im Adhoc Mode.
> 2. Darf keine HNA (bmxd) verwenden, um private IP-Adressen oder Internet-Adressen im Netz bekannt zu geben.
> 3. Muss korrekte Kontaktinformationen (Nickname und E-Mail-Adresse) enthalten.
> 4. Muss korrekte GPS-Koordinaten enthalten. Diese werden für die Hotspotliste, Kartendienste und die Planung des Netzausbaus verwendet.
> 5. Muss die Nutzungsbedingungen (Pico Peering Agreement) erfüllen. Diese sind derzeit in der Firmware oder auf GitHub verfügbar und wurden vom Pico Peering Agreement abgeleitet.
> 6. Muss alle Daten ungesehen weiterleiten.
> 7. Darf keine Daten umleiten oder verändern.
> 8. Darf keine Datenströme priorisieren oder Ports sperren.
> 9. Das Routing, welches durch das Routingprotokoll (bmxd) definiert wird, darf nicht verändert werden.
> 10. Muss als Router arbeiten.
> 11. Muss den Registrator nutzen, um eine eindeutige Knotennummer zu erhalten.
> 12. Muss die vorgegebene Berechnungsgrundlage für die IP-Adressberechnung verwenden.
> 13. Muss einen Webserver auf Port 80 bereitstellen. Dieser dient der Abfrage der Systeminformationen.
> 14. Muss Systeminformationen im vorgegebenen JSON-Format bereitstellen.
> 15. Muss das Routingprotokoll `bmxd` in gleicher, von der Firmware genutzten Version mit vorgegebenen Parametern verwenden (BMXD: GitHub). Andere Parameter sind nicht erlaubt.
>
> Quelle: https://wiki.freifunk-dresden.de/index.php/Knoten_Spezifikation, Stand 27.03.2026

## Umsetzung

Dieses Kapitel gruppiert die Anforderungen logisch und hält pro Gruppe den aktuellen Umsetzungsstand fest.

### Übersicht je Punkt

1. **nicht relevant** – kein WLAN im aktuellen Container-Modell
2. **umgesetzt** – der `bmxd`-Start enthält keine HNA-Ankündigungen und der Container ergänzt keine zusätzlichen HNA-Routen
3. **umgesetzt** – Kontaktfelder (Nickname und E-Mail-Adresse) sind technisch verpflichtend integriert, werden validiert und im Sysinfo-JSON ausgegeben
4. **umgesetzt** – GPS-Felder sind technisch integriert und werden im Sysinfo-JSON ausgegeben; die korrekten Koordinaten müssen durch den Nodebetreiber gesetzt werden
5. **umgesetzt** – Pico Peering / Nutzungsbedingungen sowie GPL-Texte sind in der UI unter `Rechtliches` eingebunden und werden unter `/licenses/*` ausgeliefert
6. **umgesetzt** – der Container enthält keinen Proxy-, NAT-, Filter- oder Umschreibpfad, sondern leitet Mesh-Verkehr nur über `fastd` und `bmxd` weiter
7. **umgesetzt** – es ist keine Umleitungs- oder Manipulationslogik für Nutzdaten implementiert
8. **umgesetzt** – es gibt keine QoS-, Traffic-Shaping-, Firewall- oder Port-Block-Regeln im Container-Setup
9. **umgesetzt** – die Routenentscheidung für das Mesh wird durch `bmxd` getroffen; der Container ergänzt nur die für den Betrieb nötige Interface- und Policy-Rule-Vorbereitung
10. **umgesetzt** – der Node arbeitet als Router; `ip_forward` wird im Entrypoint aktiv gesetzt und geprüft, und Transit-Routing zwischen mehreren Backbone-Interfaces erfolgt über `bmxd`-gesteuerte Routen
11. **umgesetzt** – Registrierung und eindeutige Node-ID sind implementiert
12. **umgesetzt** – IP-Adressberechnung aus der Node-ID ist implementiert
13. **umgesetzt** – `nginx` auf Port 80 liefert die Sysinfo-Endpunkte aus
14. **umgesetzt** – Sysinfo-JSON wird vom Sysinfo-Dienst gerendert und per Symlink in `/run/freifunk/www` veröffentlicht
15. **umgesetzt** – `bmxd`-Build und Startparameter sind implementiert

### Status-Legende

- **umgesetzt**: im aktuellen Stand bereits technisch abgebildet
- **teilweise umgesetzt**: Grundbausteine sind vorhanden, aber noch nicht vollständig abgesichert oder dokumentiert
- **offen**: noch nicht umgesetzt
- **nicht relevant**: im aktuellen Container-Modell bewusst nicht Teil des Scopes

### 1. Server-Rolle, Mesh-Verhalten und Routing

**Status:** umgesetzt

**Betroffene Punkte:** 1, 2, 6, 7, 8, 9, 10, 15

**Aktueller Stand**

- **Punkt 1:** Für das aktuelle Container-Modell ist WLAN bewusst nicht Teil des Scopes. Der Node ist als reiner Backbone-/Server-Knoten ohne Access-Point-Funktion gedacht.
- **Punkt 2:** Der aktuelle `bmxd`-Start enthält keine HNA-Parameter. Der Container ergänzt auch außerhalb des `bmxd`-Starts keine HNA-Ankündigungen für private oder Internet-Netze.
- **Punkt 6:** Der Datenpfad besteht im Container aus `fastd`, `wireguard`, `bmxd` und dem notwendigen Interface-Setup. Es gibt keinen zusätzlichen Proxy-, NAT- oder Paket-Umschreibpfad, der Nutzdaten inhaltlich verändert.
- **Punkt 7:** Es gibt keine Logik zum Umleiten oder Verändern von Nutzdaten. Das `bmxd`-Event-Script protokolliert nur Zustände und greift nicht in den Datenverkehr ein.
- **Punkt 8:** Im Container sind keine QoS-, Traffic-Shaping-, Firewall- oder Port-Sperrregeln konfiguriert. Das `docker-compose`-Setup veröffentlicht nur den `fastd`-UDP-Port und definiert keine selektiven Filtersätze.
- **Punkt 9:** Die Routingentscheidung für das Mesh verbleibt bei `bmxd`. Der Launcher setzt nur die Primär-IP, die beteiligten Interfaces und eine Policy Rule für das Mesh-Präfix, damit das von `bmxd` aufgebaute Routing im Container nutzbar wird.
- **Punkt 10:** Der Node arbeitet als Router. `ip_forward` wird beim Start explizit aktiviert und hart geprüft. Mehrere Backbone-Interfaces gleichzeitig sind möglich; Transit-Routing zwischen ihnen erfolgt über `bmxd`-Routen in der Policy-Tabelle, nicht über Bridging.
- **Punkt 15:** `bmxd` wird aus den Freifunk-Dresden-Quellen gebaut und mit fest vorgegebenen Parametern gestartet.

**Belege im Repository**

- [dockernode/Dockerfile](dockernode/Dockerfile)
- [dockernode/scripts/bmxd-launcher.sh](dockernode/scripts/bmxd-launcher.sh)
- [dockernode/scripts/bmxd-gateway.py](dockernode/scripts/bmxd-gateway.py)
- [dockernode/docker-compose.yml](dockernode/docker-compose.yml)

**Noch offen / zu verifizieren**

- Host-seitige Firewall-, NAT- oder QoS-Regeln sollten als Betriebsanforderung weiterhin ausgeschlossen bleiben, damit die Container-Eigenschaften 6 bis 9 auf dem Zielsystem nicht nachträglich ausgehebelt werden.
- Für Punkt 1 kann optional noch ein kurzer Satz ergänzt werden, dass der Container absichtlich kein WLAN bereitstellt.

### 2. Registrierung und Adressierung

**Status:** umgesetzt

**Betroffene Punkte:** 11, 12

**Aktueller Stand**

- **Punkt 11:** Der Registrator erzeugt bzw. persistiert `register_key` und `node_id` und holt eine eindeutige Knotennummer über die Registrierungs-URL.
- **Punkt 12:** Die Adressberechnung ist zentral in `node_addresses()` hinterlegt und leitet die Mesh-Adressen direkt aus der Node-ID ab.
- Änderungen an der zugewiesenen Node-ID werden in die Laufzeitkonfiguration für `fastd`, `wireguard` und `bmxd` übernommen.

**Belege im Repository**

- [dockernode/scripts/registrar.py](dockernode/scripts/registrar.py)

**Noch offen / optional**

- Ergänzende Tests oder ein kurzer Abschnitt mit Beispielwerten für die Adressberechnung wären hilfreich.

### 3. Knoten-Metadaten und organisatorische Anforderungen

**Status:** umgesetzt

**Betroffene Punkte:** 3, 4, 5

**Aktueller Stand**

- Das Zielmodell ist festgelegt: User-Inputs kommen per Env, Laufzeit-State bleibt in `/data/node.yaml`.
- Kontaktinformationen und Community werden zentral in der Python-Config validiert und im Sysinfo-JSON ausgegeben; Nickname und E-Mail-Adresse sind verpflichtend.
- GPS-Koordinaten werden ebenfalls zentral gelesen und im Sysinfo-JSON ausgegeben; die korrekten Werte müssen durch den Nodebetreiber gepflegt werden.
- Die Nutzungsbedingungen sowie das Pico Peering Agreement sind als Rechtstexte eingebunden und werden zusammen mit GPLv2/GPLv3 im Webroot unter `/licenses/*` ausgeliefert und in der UI unter `Rechtliches` angezeigt.

**Noch offen**

- Inhaltliche/rechtliche Prüfung der finalen Dokumentversionen (Texte selbst) bleibt organisatorisch möglich, die technische Einbindung ist abgeschlossen.

### 4. Webserver und Systeminformationen

**Status:** umgesetzt

**Betroffene Punkte:** 13, 14

**Aktueller Stand**

- Der Sysinfo-Dienst rendert zyklisch gültiges JSON nach `/run/freifunk/sysinfo/sysinfo.json` und aktualisiert die Symlinks in `/run/freifunk/www`.
- `nginx` läuft als eigener runit-Dienst auf Port 80 und liefert:
    - `GET /sysinfo.json` → `/run/freifunk/www/sysinfo.json`
    - `GET /sysinfo-json.cgi` → `/run/freifunk/www/sysinfo-json.cgi`
    - `GET /nodes.json` → `/run/freifunk/www/nodes.json`
    - `GET /` → Redirect auf `/ui/`
    - `GET /ui/*` → gebaute SPA
    - `GET /licenses/*` → Rechtstexte (`agreement-de.txt`, `pico-de.txt`, `gpl2.txt`, `gpl3.txt`)
- Verzeichnis-Listing ist deaktiviert.
- Log (access + error) geht direkt nach stdout/stderr des Containers.
- Port 80 wird im `docker-compose`-Setup als `${HTTP_PORT:-80}:80` veröffentlicht.

**Belege im Repository**

- [dockernode/config/nginx.conf](dockernode/config/nginx.conf)
- [dockernode/scripts/runit/nginx/run](dockernode/scripts/runit/nginx/run)
- [dockernode/docker-compose.yml](dockernode/docker-compose.yml)

**Noch offen / optional**

- Healthcheck ergänzen, der die JSON-Endpunkte auf HTTP 200 prüft
- optional: separates access-log-Format, das die Freifunk-Node-ID enthält



