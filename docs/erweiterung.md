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

### Statusdateien im Webroot bereitstellen

Wenn eine App einen JSON-Status nach außen bereitstellen soll, gilt dasselbe Muster wie bei `sysinfo` und `mesh-status`:

- Die App schreibt ihre Laufzeitdatei atomar in ein flüchtiges Laufzeitverzeichnis, zum Beispiel nach `/run/freifunk/state/myapp-status.json`
- Für den stabilen HTTP-Pfad wird im Webroot `/run/freifunk/www/` ein Symlink angelegt, zum Beispiel `/run/freifunk/www/myapp-status.json`
- `nginx` liefert nur diesen stabilen Pfad aus und erzeugt den Inhalt nicht selbst

Beispiel:

```text
/run/freifunk/state/myapp-status.json
/run/freifunk/www/myapp-status.json -> /run/freifunk/state/myapp-status.json
GET /myapp-status.json
```

Praktisch bedeutet das:

- Producer schreiben in ihr eigenes Runtime-Verzeichnis
- Das Webroot enthält nur veröffentlichte Dateinamen oder Symlinks
- Der HTTP-Pfad bleibt stabil, auch wenn sich das interne Runtime-Verzeichnis einer App ändert

Wichtig für die Auslieferung:

- Die Datei selbst sollte mit mindestens `0644` geschrieben werden
- Alle übergeordneten Verzeichnisse auf dem Pfad zum Symlink-Ziel müssen für den `nginx`-Prozess durchsuchbar sein, in der Praxis also typischerweise mindestens `0755`
- Fehlt dieses Execute-Bit auf einem Verzeichnis, endet der Request trotz vorhandenem Symlink mit `403 Forbidden`

Minimal nötig sind also drei Dinge: Runtime-Datei schreiben, Symlink im Webroot anlegen und den Pfad für `nginx` lesbar bzw. durchsuchbar machen.

### Wann reicht ein JSON-Endpoint?

Nicht jede Erweiterung braucht einen eigenen Menüpunkt in der UI.

- Ein zusätzlicher JSON-Endpoint reicht, wenn die bestehende UI nur weitere Daten anzeigen soll, zum Beispiel ein zusätzliches Panel oder weitere Kennzahlen auf einer vorhandenen Seite
- Ein eigener UI-View ist erst dann sinnvoll, wenn die Erweiterung einen eigenen Bedienkontext, eigene Interaktion oder eine eigene Seite innerhalb der Navigation braucht

Faustregel: Neue Daten allein sind noch keine UI-Erweiterung. Ein neuer View ist erst dann sinnvoll, wenn die bestehende Seite dafür fachlich zu eng wird.

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

### UI-Erweiterungen

Wenn eine App einen eigenen Menüpunkt in der Standard-UI bekommen soll, klinkt sie sich als zusätzlicher View in die bestehende SPA ein.
Topbar, Sidebar, Routing und Grundlayout bleiben dabei in der Basis-UI.
Die Erweiterung liefert nur ihren eigenen Inhaltsbereich.

Ziel ist ein einheitliches Erscheinungsbild ohne harte Runtime-Kopplung an das Basis-Frontend.
Für UI-Erweiterungen gilt folgender Minimalvertrag:

- Die Erweiterung verwendet dieselbe visuelle Sprache: Farben, Abstände, Status-Pills, Tabellenstil und allgemeine Layout-Konventionen
- Die Erweiterung kann intern dasselbe Framework wie die Basis-UI verwenden, zum Beispiel Preact
- Die Erweiterung hängt aber nicht von der konkret im Basis-Image eingebauten Framework-Version ab
- Die Erweiterung liefert ein eigenes Bundle aus und erwartet nicht, dass Host-Komponenten oder die Host-Runtime direkt importierbar sind
- Geteilt wird ein kleiner UI-Vertrag, nicht die komplette interne Frontend-Struktur

Minimaler technischer Vertrag:

- Die Basis-UI besitzt Navigation, Hash-Routing, Kopfbereich und allgemeines Seitenlayout
- Eine Erweiterung wird nicht per Verzeichnis-Scan gefunden, sondern über eine Registry-Datei angemeldet
- Das Bundle der Erweiterung wird im abgeleiteten Image nach `/usr/local/share/freifunk/ui-extensions/APP/` kopiert
- Zur Laufzeit wird es nach `/run/freifunk/www/ui/extensions/APP/` veröffentlicht
- Die Registry-Datei `/ui/extensions/index.json` wird von der Plattform erzeugt; einzelne Erweiterungen schreiben diese Datei nicht direkt
- Wenn mehrere Erweiterungen vorhanden sind, führt die Plattform deren Einträge zu einer gemeinsamen Registry zusammen
- Die Basis-UI lädt `/ui/extensions/index.json`, baut daraus die Menüeinträge und lädt das aktive Bundle dynamisch
- Erweiterungen deklarieren die von ihnen benötigten JSON-Endpunkte im jeweiligen Registry-Eintrag, damit die Basis-UI diese in ihren gemeinsamen Refresh-Zyklus aufnehmen kann
- Eine Erweiterung liefert mindestens Menü-Key, Label, Reihenfolge und einen Renderer für den Content-Bereich

Beispiel für die Registry-Datei:

```json
{
    "extensions": [
        {
            "id": "metadata",
            "label": "Metadaten",
            "order": 100,
            "hash": "metadata",
            "entry": "/ui/extensions/metadata/index.js",
            "endpoints": [
                "/metadata.json"
            ]
        }
    ]
}
```

Minimaler Renderer-Vertrag:

```javascript
export function render(container, context) {
    container.textContent = 'Metadata view';
}

export function dispose(container) {
    container.textContent = '';
}
```

Dabei gilt:

- `hash` bestimmt den View-Key in der SPA, zum Beispiel `#metadata`
- `entry` verweist auf das gebaute Bundle der Erweiterung
- `render()` rendert nur den Inhaltsbereich, nicht die gesamte Seite
- Die Basis-UI lädt Core- und Extension-Daten in einem gemeinsamen Refresh-Zyklus, typischerweise alle 30 Sekunden
- Erweiterungen starten dafür standardmäßig keine eigenen Polling-Timer
- `context` enthält nur kleine stabile Host-Helfer und den aktuellen Datenstand der Erweiterung, zum Beispiel `data`, `error`, `refreshNow()`, `fetchJson(url)`, `fetchText(url)`, `safe(value)` und einfache Formatierungsfunktionen
- Alles außerhalb dieses kleinen `context`-Vertrags gilt als intern und wird von Erweiterungen nicht direkt verwendet

Bewusst nicht Teil des Vertrags sind:

- direkte Imports interner Komponenten aus der Basis-SPA
- Abhängigkeit von einer exakt gleichen Host-Framework-Version
- eigene Topbar oder Sidebar innerhalb der Erweiterung
- eine separate, vollständig unabhängige Web-App für kleine Zusatzfunktionen

Damit bleibt die UI konsistent, und Updates des Basis-Images können erfolgen, ohne dass jede Erweiterung an interne Frontend-Details gekoppelt ist.

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
