# Strava Walk Muter (Home Assistant App)

Lokale Home-Assistant-App (früher „Add-on“), die Strava-Spaziergänge automatisch
stummschaltet (`hide_from_home`), damit sie nicht im Feed der Follower erscheinen.

- App-Code und Doku: [`strava_walk_muter/`](strava_walk_muter/)
- Einrichtung Schritt für Schritt: [`strava_walk_muter/DOCS.md`](strava_walk_muter/DOCS.md)

Installation: den Ordner `strava_walk_muter` nach `/local_apps/` auf Home Assistant
kopieren, dann im App Store „Nach Updates suchen“ und unter „Lokale Apps“ installieren.

Tests:

```bash
cd strava_walk_muter
python3 -m pip install requests
python3 -m unittest discover -s tests -v
```
