# Erweiterung eines ffdd-node Images

Dieses Dokument beschreibt den Startpunkt für ein eigenes Erweiterungs-Image.

## Basis

## Projektstruktur für eine Erweiterung

Lege ein separates Repo für deine Erweiterung an und übernimm als Basis:

- `docker-compose.yaml`
- `.env.example`
- `.gitignore`
- `.dockerignore`

Erstelle ein Basis `Dockerfile`:

```Dockerfile
FROM ghcr.io/freifunkstuff/ffdd-node:master
```

Erstelle eine passende .env auf Basis von env.example.

## Bauen und Testen

Image bauen und Tests ausführen:

```bash
# Image bauen
docker-compose build --pull

# Starten und Logs anschauen
docker-compose up -d && docker-compose logs -f
```

## App integrieren

### Contract

Jede App folgt diesem minimalen Contract:

**Konfiguration:**
- Alle Konfiguration erfolgt über app-spezifische Umgebungsvariablen, zum Beispiel `MYAPP_*`
- Typ-Konvertierung (str, int, float, bool) erfolgt in der App selbst
- Ein zentrales Defaults-YAML ist optional.

**Verzeichnisse:**
- **Persistente Daten:** `/data/APP/` — wird beim Start erzeugt (0755), Daten bleiben über Neustarts erhalten
- **Image-lokale Daten:** `/var/lib/freifunk/APP/` — temporär, wird bei Image-Update gelöscht, optional

**Beispiel ENV-Variablen in .env:**
```bash
MYAPP_ENABLED=true
MYAPP_INTERVAL=3600
MYAPP_DEBUG=false
```

### Optionaler Plattform-Dienst: Mesh-Status

Apps können den aktuellen Mesh-Zustand über `/run/freifunk/state/mesh-status.json` lesen, müssen davon aber nicht abhängen.
Die Datei wird vom Dienst `mesh-status` zyklisch aktualisiert.

Beispiel:

```json
{
    "updated_at": "2026-03-29T00:34:12.000000+01:00",
    "mesh": {
        "connected": true,
        "stable": true,
        "checked_links": 3,
        "reachable_links": 2,
        "connected_duration": 31,
        "stable_after": 30
    },
    "gateway": {
        "selected": "10.200.200.200",
        "connected": true
    }
}
```

Bedeutung:

- `mesh.connected`: mindestens eines der geprüften Ziele aus `bmxd -c --links` ist erreichbar
- `mesh.stable`: `mesh.connected` liegt seit mindestens `stable_after` Sekunden ohne Unterbrechung an
- `mesh.checked_links`: Anzahl der aktuell geprüften Link-Ziele
- `mesh.reachable_links`: Anzahl der aktuell erreichbaren Link-Ziele
- `mesh.connected_duration`: Sekunden seit Beginn des aktuellen zusammenhängenden `mesh.connected`-Zustands
- `gateway.selected`: aktuell von `bmxd -c --gateways` ausgewähltes Gateway, leer wenn keines selektiert ist
- `gateway.connected`: das aktuell selektierte Gateway ist erreichbar

Für Apps ist `mesh.stable` das robustere Startsignal als `gateway.connected`, weil Mesh auch ohne selektiertes Gateway sinnvoll benutzbar sein kann.
Wenn eine App keinen Mesh-Bezug hat, kann dieser Plattform-Dienst ignoriert werden.

### Installation

Die Anwendung wird im Dockerfile in das Image installiert.
Dabei werden auch alle benötigten Laufzeitabhängigkeiten eingebracht.
Das Ergebnis ist ein fester Einstiegspunkt im Image, auf den das runit-Service zeigt.

Zusätzlich werden im Dockerfile die Service-Dateien nach `/etc/service/APP/` kopiert.
Falls die App einen eigenen Failfast-Check benötigt, wird dafür ein ausführbarer Hook unter `/etc/docker-entrypoint.d/` installiert.

### Runit Service

Services werden als runit-Skripte unter `runit/APP/run` integriert:

```bash
#!/bin/sh
printf '%s [myapp] gestartet\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
exec sleep infinity
```

Der Dienst wird automatisch von runit gestartet und bei Fehler neu gestartet.
Das runit-Service zeigt auf den installierten Einstiegspunkt im Image, nicht auf einen beliebigen Quellpfad aus dem Build-Kontext.
Für erste Integrationsschritte muss der Prozess im Vordergrund dauerhaft weiterlaufen; ein einmaliges Loggen und direktes Beenden führt nur zu Neustart-Schleifen durch runit.

### Validierung (Optional)

Wenn deine App Validierung benötigt, registriere einen Hook unter `/etc/docker-entrypoint.d/NN-APP` (Exit-Code != 0 = Fehler):

```bash
#!/bin/sh
/usr/local/bin/myapp --checkconfig
```

Hooks werden beim Container-Start (nach festen Basis-Checks) nacheinander ausgeführt. Nur Apps mit Validierungsbedarf brauchen einen Hook.

### Logging

Verwende das Standard-Log-Prefix Format:

```python
from datetime import datetime, timezone

def log_info(message: str) -> None:
    tz = datetime.now(timezone.utc).strftime("%z")
    tz_formatted = tz[:-2] + ":" + tz[-2:]  # +0100 → +01:00
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " " + tz_formatted
    print(f"{timestamp} [myapp] {message}", flush=True)
```

**Format:** `YYYY-MM-DD HH:MM:SS ±HH:MM [APP]`

Beispiel:
```
2026-03-28 23:18:06 +01:00 [myapp] App gestartet
2026-03-28 23:19:12 +01:00 [myapp] ERROR: Config ungültig
```

**Wichtig:** Nutze `-u` Flag bei python3 für unbuffertes Output:

```bash
#!/bin/sh
exec python3 -u /usr/local/bin/myapp/main.py --loop --interval 60
```

Alle Logs gehen auf stdout/stderr und werden von Docker/runit/svlogd protokolliert.
