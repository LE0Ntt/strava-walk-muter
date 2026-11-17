# Strava Walk Muter (Home Assistant App)

Lokale Home-Assistant-App (früher „Add-on“), die Strava-Spaziergänge automatisch
stummschaltet (`hide_from_home`), damit sie nicht im Feed der Follower erscheinen.

- App-Code und Doku: [`strava_walk_muter/`](strava_walk_muter/)
- Einrichtung Schritt für Schritt: [`strava_walk_muter/DOCS.md`](strava_walk_muter/DOCS.md)

## Installation

**Als Repository (empfohlen):** In Home Assistant **Einstellungen → Apps → App installieren**,
oben rechts **⋮ → Repositories**, dann `https://github.com/LE0Ntt/strava-walk-muter` hinzufügen.
Danach erscheint „Strava Walk Muter“ im App Store. Das Image wird beim Installieren auf dem Gerät gebaut.

**Lokal:** den Ordner `strava_walk_muter` nach `/local_apps/` kopieren, dann im App Store
„Nach Updates suchen“ und unter „Lokale Apps“ installieren.

Tests:

```bash
cd strava_walk_muter
python3 -m pip install requests
python3 -m unittest discover -s tests -v
```
