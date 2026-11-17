# Changelog

Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), neueste
Version oben.

## [1.0.0] – 2026-11-17

### Hinzugefügt

- Erste Version.
- Regelmäßige Abfrage von `GET /athlete/activities` mit Rückblickfenster (`lookback_hours`).
- Stummschalten passender Sportarten per `PUT /activities/{id}` mit `hide_from_home: true`.
- Speicherung bearbeiteter IDs und des ersten Starts in `/data/state.json`; optionaler einmaliger `backfill`.
- `dry_run` (Standard an).
- OAuth-Autorisierung per `auth_code`, automatische Token-Erneuerung inklusive Speicherung neuer Refresh-Tokens.
- Rate-Limit-Behandlung (429 → nächstes 15-Minuten-Fenster, vorausschauend anhand der Antwort-Header), Wiederholung bei 5xx und Netzwerkfehlern.
- Schwärzung von Tokens, Secret und Code im Protokoll.
