# Strava Walk Muter

Dieses Add-on (in Home Assistant heißen Add-ons inzwischen **Apps**) schaltet
deine Strava-Spaziergänge automatisch **stumm**: Es setzt bei jeder passenden
Aktivität `hide_from_home: true`. Die Aktivität erscheint dann nicht mehr im
Feed deiner Follower, bleibt aber in deinem Profil erhalten.

Die **Sichtbarkeit** (Jeder / Follower / Nur ich) wird **nicht** verändert.
Die Strava-API bietet dafür kein Feld, und das Add-on versucht es auch nicht.

---

## Einrichtung Schritt für Schritt

### 1. Strava-App anlegen

1. Öffne <https://www.strava.com/settings/api> (mit deinem Strava-Konto angemeldet).
2. Lege eine App an:
   - **Application Name:** frei wählbar, z. B. `HA Walk Muter`
   - **Category:** beliebig, z. B. „Other“
   - **Website:** beliebig, z. B. `http://localhost`
   - **Authorization Callback Domain:** `localhost`
3. Notiere dir **Client ID** (eine Zahl) und **Client Secret**.

Neue Strava-Apps laufen im „Single Player Mode“, sind also nur für dein eigenes
Konto nutzbar. Für dieses Add-on genügt das.

### 2. Ordner nach Home Assistant kopieren

Home Assistant sucht selbst gebaute Apps im Ordner **`/local_apps`**. In
älteren Versionen hieß er `/addons`. Der fertige Pfad lautet:

```
/local_apps/strava_walk_muter/config.yaml
/local_apps/strava_walk_muter/Dockerfile
/local_apps/strava_walk_muter/strava_walk_muter.py
...
```

**Mit „Terminal & SSH“:** Ist SSH mit Schlüssel oder Passwort eingerichtet,
kannst du den Ordner vom Rechner aus kopieren:

```bash
scp -r strava_walk_muter root@homeassistant.local:/local_apps/
```

Benutzer und Port richten sich nach der Konfiguration von „Terminal & SSH“.

**Mit „Samba share“:** Öffne die Freigabe `local_apps` (früher `addons`) und
ziehe den Ordner `strava_walk_muter` hinein.

Wichtig: `config.yaml` muss direkt im Ordner `strava_walk_muter` liegen und
nicht in einem weiteren Unterordner.

### 3. App im Store installieren

1. **Einstellungen → Apps → App installieren** (öffnet den App Store).
2. Oben rechts das Drei-Punkte-Menü → **„Nach Updates suchen“**.
3. Unten erscheint der Bereich **„Lokale Apps“** mit **Strava Walk Muter**.
4. Öffnen → **Installieren**. Das Image wird direkt auf dem Raspberry Pi gebaut,
   das dauert 1–3 Minuten.

Erscheint die App nicht, lade die Seite neu (Strg+F5 bzw. Cmd+Shift+R). Hilft das
nicht, sieh unter **Einstellungen → System → Protokolle → Supervisor** nach Fehlern.

### 4. Optionen eintragen, starten und autorisieren

1. Wechsle zum Tab **Konfiguration** und trage ein:
   - `client_id`: die Client ID aus Schritt 1
   - `client_secret`: das Client Secret aus Schritt 1
   - `dry_run` bleibt vorerst **an**
2. **Speichern**, dann zum Tab **Info** → **Starten**.
3. Öffne den Tab **Protokoll**. Dort steht eine Zeile wie:
   ```
   https://www.strava.com/oauth/authorize?client_id=12345&redirect_uri=http://localhost&response_type=code&approval_prompt=force&scope=activity:read_all,activity:write
   ```
4. Öffne diese URL im Browser und klicke **Autorisieren**. Lass dabei **beide
   Haken** gesetzt (alle Aktivitäten lesen, Aktivitäten bearbeiten).
5. Der Browser leitet auf eine Seite weiter, die nicht lädt. Das ist richtig so.
   In der Adresszeile steht dann etwa:
   ```
   http://localhost/?state=&code=abc123def456...&scope=read,activity:write,activity:read_all
   ```
   Prüfe, ob hinter `scope=` sowohl `activity:write` als auch
   `activity:read_all` steht. Kopiere den Wert hinter `code=` bis zum nächsten `&`.
6. Trage ihn in der Konfiguration bei `auth_code` ein, **speichere** und
   **starte die App neu**. Mach das zügig, denn der Code ist nur kurz gültig und
   lässt sich nur einmal verwenden.
7. Im Protokoll erscheint dann: `Autorisierung erfolgreich, Tokens gespeichert.`

Ab jetzt erneuert das Add-on die Tokens selbst. Den `auth_code` kannst du
danach wieder leeren, musst es aber nicht, denn ein bereits benutzter Code wird
nicht noch einmal verwendet.

### 5. Mit `dry_run` testen, dann scharf schalten

Mit `dry_run: true` ändert das Add-on nichts und schreibt nur ins Protokoll,
was es tun würde:

```
[Dry-Run] Würde stummschalten: Abendrunde (16.11.2026 18:05)
[Dry-Run] 3 Aktivitäten geprüft, 1 Walk würden stummgeschaltet: Abendrunde (16.11.2026 18:05)
```

Wenn das passt: `dry_run` **ausschalten**, speichern, App neu starten. Im
Protokoll steht dann `Modus: AKTIV`, und pro Zyklus zum Beispiel:

```
Stummgeschaltet: Abendrunde (16.11.2026 18:05)
3 Aktivitäten geprüft, 1 Walk stummgeschaltet: Abendrunde (16.11.2026 18:05)
```

Aktivitäten, die im Dry-Run nur angezeigt wurden, werden danach noch bearbeitet,
sofern sie innerhalb von `lookback_hours` liegen.

---

## Optionen

| Option | Standard | Bedeutung |
|---|---|---|
| `client_id` | – | Client ID deiner Strava-App |
| `client_secret` | – | Client Secret deiner Strava-App (wird nie geloggt) |
| `auth_code` | – | Einmal-Code aus der Weiterleitungs-URL (siehe Schritt 4) |
| `poll_interval_minutes` | `15` | Abstand zwischen zwei Abfragen (5–1440) |
| `lookback_hours` | `48` | Wie weit jede Abfrage zurückschaut (`after`). Damit werden auch spät hochgeladene Aktivitäten erfasst. |
| `sport_types` | `[Walk]` | Strava-`sport_type`-Werte, die stummgeschaltet werden, z. B. `Walk`, `Hike` |
| `backfill` | `false` | Auch Aktivitäten **vor** dem ersten Start bearbeiten (siehe unten) |
| `dry_run` | `true` | Nur loggen, nichts ändern |

Nach jeder Änderung der Optionen muss die App neu gestartet werden.

## So arbeitet das Add-on

- Alle `poll_interval_minutes` fragt es `GET /api/v3/athlete/activities` mit
  `after = jetzt − lookback_hours` ab.
- Für jede Aktivität, deren `sport_type` in `sport_types` steht, ruft es
  `PUT /api/v3/activities/{id}` mit `{"hide_from_home": true}` auf.
- Bearbeitete Aktivitäts-IDs werden in `/data/state.json` gespeichert. Diese
  Aktivitäten fasst das Add-on nie wieder an. Hast du eine stummgeschaltete
  Aktivität von Hand wieder sichtbar gemacht, bleibt das also so.
- **Erster Start:** Beim allerersten Start merkt sich das Add-on den Zeitpunkt.
  Ohne `backfill` bearbeitet es nur Aktivitäten, die **danach begonnen** haben
  (maßgeblich ist die Startzeit der Aktivität, nicht die Upload-Zeit).
- **Backfill:** Mit `backfill: true` lädt das Add-on einmalig **alle** deine
  Aktivitäten und schaltet alle passenden stumm. Danach arbeitet es wieder mit
  dem Rückblickfenster, ältere Aktivitäten darin werden aber ebenfalls
  bearbeitet. Bei vielen Spaziergängen stößt das an die Rate-Limits. Das
  Add-on wartet dann automatisch und macht weiter. Im Dry-Run wird der Backfill
  nur einmal pro Start durchgespielt und nicht als erledigt gespeichert.
- Ist eine Aktivität schon stumm (`hide_from_home: true` in der Liste), wird
  sie nur als erledigt vermerkt.

## Rate-Limits und Fehler

- Strava erlaubt standardmäßig 100 Lese- bzw. Nicht-Upload-Anfragen pro
  15 Minuten und 1000 pro Tag. Ein Zyklus braucht im Normalfall genau
  **eine** Anfrage, dazu eine pro stummgeschalteter Aktivität.
- Bei **HTTP 429** wartet das Add-on bis zum nächsten 15-Minuten-Fenster
  (:00, :15, :30, :45 UTC). Melden die Antwort-Header, dass das Limit schon
  erreicht ist, wartet es vorab bis dahin. Beim Tageslimit wartet es bis
  Mitternacht UTC.
- Bei **5xx** und Netzwerkfehlern versucht es die Anfrage im nächsten Zyklus erneut.
- Der Container stürzt bei Fehlern nicht ab. Auch unerwartete Fehler werden
  geloggt, danach läuft der nächste Zyklus.

## Gespeicherte Dateien (`/data`)

| Datei | Inhalt |
|---|---|
| `tokens.json` | Access- und Refresh-Token (Dateirechte 600). Der Refresh-Token wird nach jeder Erneuerung neu gespeichert, weil Strava ihn austauschen kann. |
| `state.json` | Zeitpunkt des ersten Starts, bearbeitete IDs, Backfill-Status, Hash des zuletzt benutzten `auth_code` |

Tokens, Secret und Code erscheinen nie im Protokoll.

## Fehlerbehebung

| Meldung im Protokoll | Lösung |
|---|---|
| `Bitte 'client_id' (Zahl) und 'client_secret' … eintragen` | Optionen ausfüllen, speichern, neu starten |
| `Strava hat die Anfrage abgelehnt (Code-Tausch, HTTP 400) … AuthorizationCode/code/invalid` | Der Code war abgelaufen oder schon benutzt. Autorisierungs-URL aus dem Protokoll erneut öffnen, neuen Code eintragen und neu starten |
| `Keine Berechtigung … (HTTP 403)` | Beim Autorisieren fehlte ein Haken. Neu autorisieren (Schritt 4) |
| `… abgelehnt (Token-Erneuerung …)` / `Strava lehnt das Token ab` | Zugriff wurde widerrufen, z. B. unter strava.com/settings/apps. Neu autorisieren |
| `Strava-Rate-Limit erreicht … warte bis …` | Nichts tun, das Add-on macht selbstständig weiter |

**Zugriff entziehen:** <https://www.strava.com/settings/apps> → App → „Zugriff
widerrufen“. Danach in Home Assistant die App deinstallieren. Dabei wird auch
`/data` gelöscht.

---

## Für Entwickler: lokaler Test

### Unit-Tests

Die Tests nutzen gemockte HTTP-Antworten und brauchen kein Strava-Konto:

```bash
cd strava_walk_muter
python3 -m pip install requests
python3 -m unittest discover -s tests -v
```

Oder direkt im gebauten Image (siehe unten):

```bash
docker run --rm --entrypoint python3 -v "$PWD:/src" -w /src strava-walk-muter -m unittest discover -s tests -v
```

### Testlauf mit `docker build` und `dry_run: true`

Das Basis-Image `ghcr.io/home-assistant/base-python:3.14-alpine3.24` gibt es für
`amd64` und `aarch64`. Ein Build läuft also sowohl auf einem PC als auch auf
einem Apple-Silicon-Mac.

```bash
cd strava_walk_muter
docker build -t strava-walk-muter .

mkdir -p testdata
cat > testdata/options.json <<'EOF'
{
  "client_id": "12345",
  "client_secret": "DEIN_CLIENT_SECRET",
  "auth_code": "",
  "poll_interval_minutes": 15,
  "lookback_hours": 48,
  "sport_types": ["Walk"],
  "backfill": false,
  "dry_run": true
}
EOF

docker run --rm -it -v "$PWD/testdata:/data" strava-walk-muter
```

1. Der erste Lauf loggt die Autorisierungs-URL. Öffnen, autorisieren und den
   `code` in `testdata/options.json` bei `auth_code` eintragen.
2. `docker run …` erneut starten. Das Protokoll zeigt `Autorisierung
   erfolgreich` und danach eine Zeile `[Dry-Run] … Aktivitäten geprüft, …`.
3. Mit Strg+C beenden.

Danach `testdata/` löschen. Darin liegen echte Tokens und das Secret, also
niemals einchecken oder weitergeben. Home Assistant autorisiert sich separat
mit einem eigenen Code (Schritt 4).
