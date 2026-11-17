# Strava Walk Muter

Schaltet deine Strava-Spaziergänge automatisch stumm: Sie verschwinden aus dem
Feed deiner Follower (`hide_from_home`), bleiben aber in deinem Profil. Die
Sichtbarkeit (privat/öffentlich) wird nicht verändert.

- fragt alle 15 Minuten (einstellbar) deine neuesten Aktivitäten ab
- bearbeitet nur die gewählten Sportarten (Standard: `Walk`)
- ändert standardmäßig nichts (`dry_run`), bis du es freigibst
- beachtet die Strava-Rate-Limits und erneuert die Tokens selbst
- kein Webinterface, keine offenen Ports

Du brauchst eine eigene Strava-API-App (kostenlos unter
strava.com/settings/api). Die vollständige Anleitung steht im Tab
**Dokumentation**.
