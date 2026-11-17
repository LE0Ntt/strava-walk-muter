#!/usr/bin/env python3
"""Strava Walk Muter – Home Assistant add-on.

Fragt regelmäßig die neuesten Strava-Aktivitäten ab und setzt bei passenden
Sportarten (Standard: Walk) ``hide_from_home: true``, damit sie nicht im Feed
der Follower erscheinen. Die Sichtbarkeit (privat/öffentlich) wird nicht
angefasst – die Strava-API bietet dafür kein Feld.

Nur Standardbibliothek plus ``requests``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

VERSION = "1.0.0"

API_BASE = "https://www.strava.com/api/v3"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REDIRECT_URI = "http://localhost"
SCOPE = "activity:read_all,activity:write"
REQUIRED_SCOPES = ("activity:read_all", "activity:write")

PER_PAGE = 100
HTTP_TIMEOUT = 30
TOKEN_REFRESH_MARGIN = 300  # Sekunden vor Ablauf erneuern
RATE_WINDOW = 15 * 60
RATE_WINDOW_BUFFER = 5  # Sekunden Puffer nach Fensterwechsel
MAX_PROCESSED_IDS = 20000

DATA_DIR = os.environ.get("STRAVA_MUTER_DATA_DIR", "/data")

log = logging.getLogger("strava_walk_muter")


# --------------------------------------------------------------------------- #
# Logging mit Schwärzung von Geheimnissen
# --------------------------------------------------------------------------- #
class RedactingFormatter(logging.Formatter):
    """Ersetzt registrierte Geheimnisse (Tokens, Secret, Code) durch ***.

    Sicherheitsnetz: Der Code loggt Geheimnisse ohnehin nie. Die Schwärzung
    greift auf die fertig formatierte Zeile inklusive Tracebacks.
    """

    _secrets: set = set()

    @classmethod
    def register(cls, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if len(text) >= 6:
            cls._secrets.add(text)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, "***")
        return text


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    level = os.environ.get("STRAVA_MUTER_LOG_LEVEL", "").upper()
    if level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        log.setLevel(level)


# --------------------------------------------------------------------------- #
# Fehlerklassen
# --------------------------------------------------------------------------- #
class StravaError(Exception):
    """Basisklasse."""


class TransientError(StravaError):
    """Netzwerkfehler oder 5xx – im nächsten Zyklus erneut versuchen."""


class RateLimitError(StravaError):
    """429 oder Limit laut Headern erreicht."""

    def __init__(self, retry_at: float, reason: str = "15-Minuten-Limit"):
        super().__init__(reason)
        self.retry_at = retry_at
        self.reason = reason


class AuthError(StravaError):
    """Token ungültig/abgelehnt – Nutzer muss neu autorisieren."""


class ApiError(StravaError):
    """Sonstige 4xx-Antworten."""


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def next_rate_window(now: float) -> float:
    """Beginn des nächsten 15-Minuten-Fensters (xx:00, :15, :30, :45 UTC)."""
    return (int(now) // RATE_WINDOW + 1) * RATE_WINDOW + RATE_WINDOW_BUFFER


def next_utc_midnight(now: float) -> float:
    return (int(now) // 86400 + 1) * 86400 + RATE_WINDOW_BUFFER


def fmt_utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%M UTC")


def parse_strava_time(value: Any) -> Optional[dt.datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def activity_start_ts(activity: Dict[str, Any]) -> Optional[float]:
    parsed = parse_strava_time(activity.get("start_date"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def activity_label(activity: Dict[str, Any]) -> str:
    name = str(activity.get("name") or "(ohne Namen)")
    # start_date_local ist Lokalzeit, wird von Strava aber mit "Z" geliefert.
    local = parse_strava_time(activity.get("start_date_local")) or parse_strava_time(
        activity.get("start_date")
    )
    date = local.strftime("%d.%m.%Y %H:%M") if local else "unbekanntes Datum"
    return f"{name} ({date})"


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as err:
        log.warning("Konnte %s nicht lesen (%s) – verwende leeren Zustand", os.path.basename(path), type(err).__name__)
        return default


def write_json_atomic(path: str, data: Any, private: bool = False) -> None:
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def safe_error_text(response: Any) -> str:
    """Kurze Fehlerbeschreibung aus einer Strava-Antwort, ohne Request-Daten."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    parts = [str(body.get("message", ""))]
    for err in body.get("errors") or []:
        if isinstance(err, dict):
            parts.append("/".join(str(err.get(k, "")) for k in ("resource", "field", "code")))
    return "; ".join(p for p in parts if p)[:300]


# --------------------------------------------------------------------------- #
# Optionen
# --------------------------------------------------------------------------- #
@dataclass
class Options:
    client_id: str = ""
    client_secret: str = ""
    auth_code: str = ""
    poll_interval_minutes: int = 15
    lookback_hours: int = 48
    sport_types: Tuple[str, ...] = ("Walk",)
    backfill: bool = False
    dry_run: bool = True


def _to_int(value: Any, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def load_options(path: str) -> Options:
    raw = read_json(path, {})
    if not isinstance(raw, dict):
        raw = {}
    sport_types = raw.get("sport_types")
    if not isinstance(sport_types, list):
        sport_types = ["Walk"]
    sport_types = tuple(str(s).strip() for s in sport_types if str(s).strip())
    dry_run = raw.get("dry_run", True)
    return Options(
        client_id=str(raw.get("client_id") or "").strip(),
        client_secret=str(raw.get("client_secret") or "").strip(),
        auth_code=str(raw.get("auth_code") or "").strip(),
        poll_interval_minutes=_to_int(raw.get("poll_interval_minutes"), 15, 1),
        lookback_hours=_to_int(raw.get("lookback_hours"), 48, 1),
        sport_types=sport_types or ("Walk",),
        backfill=raw.get("backfill") is True,
        dry_run=dry_run is not False,  # im Zweifel sicher: dry_run
    )


# --------------------------------------------------------------------------- #
# Persistenter Zustand
# --------------------------------------------------------------------------- #
class State:
    """/data/state.json: erster Start, bearbeitete IDs, Backfill-Status."""

    def __init__(self, path: str, now: float):
        self.path = path
        data = read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        self.first_start: int = int(data.get("first_start") or 0)
        ids = data.get("processed_ids") or []
        self.processed_ids: List[int] = [int(i) for i in ids if isinstance(i, (int, str)) and str(i).isdigit()]
        self._processed = set(self.processed_ids)
        self.backfill_done: bool = bool(data.get("backfill_done", False))
        self.used_code_hash: str = str(data.get("used_code_hash") or "")
        self.is_new = not self.first_start
        if self.is_new:
            self.first_start = int(now)
            self.save()

    def is_processed(self, activity_id: int) -> bool:
        return activity_id in self._processed

    def mark_processed(self, activity_id: int) -> None:
        if activity_id in self._processed:
            return
        self._processed.add(activity_id)
        self.processed_ids.append(activity_id)
        if len(self.processed_ids) > MAX_PROCESSED_IDS:
            drop = self.processed_ids[: len(self.processed_ids) - MAX_PROCESSED_IDS]
            self.processed_ids = self.processed_ids[len(drop):]
            self._processed.difference_update(drop)

    def save(self) -> None:
        write_json_atomic(
            self.path,
            {
                "first_start": self.first_start,
                "processed_ids": self.processed_ids,
                "backfill_done": self.backfill_done,
                "used_code_hash": self.used_code_hash,
            },
        )


# --------------------------------------------------------------------------- #
# Strava-Client
# --------------------------------------------------------------------------- #
class StravaClient:
    def __init__(
        self,
        session: Any,
        client_id: str,
        client_secret: str,
        token_path: str,
        clock: Callable[[], float] = time.time,
    ):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        self.clock = clock
        self.tokens: Optional[Dict[str, Any]] = None
        # (Fensterbeginn, UTC-Tag, [(usage15, limit15, usageDay, limitDay), ...])
        self._rate_info: Optional[Tuple[int, int, List[Tuple[int, int, int, int]]]] = None
        RedactingFormatter.register(client_secret)
        loaded = read_json(token_path, None)
        if isinstance(loaded, dict) and loaded.get("refresh_token"):
            self.tokens = loaded
            self._register_tokens()

    # -- Tokens ------------------------------------------------------------ #
    def has_tokens(self) -> bool:
        return bool(self.tokens and self.tokens.get("refresh_token"))

    def authorization_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "approval_prompt": "force",
            "scope": SCOPE,
        }
        return f"{AUTHORIZE_URL}?{urlencode(params, safe=':,/')}"

    def _register_tokens(self) -> None:
        if self.tokens:
            RedactingFormatter.register(self.tokens.get("access_token"))
            RedactingFormatter.register(self.tokens.get("refresh_token"))

    def _token_request(self, data: Dict[str, Any], what: str) -> Dict[str, Any]:
        try:
            resp = self.session.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT)
        except requests.RequestException as err:
            raise TransientError(f"Netzwerkfehler ({what}): {type(err).__name__}") from None
        if resp.status_code == 429:
            raise RateLimitError(next_rate_window(self.clock()))
        if resp.status_code >= 500:
            raise TransientError(f"Strava-Serverfehler {resp.status_code} ({what})")
        if resp.status_code in (400, 401, 403):
            raise AuthError(f"Strava hat die Anfrage abgelehnt ({what}, HTTP {resp.status_code}) {safe_error_text(resp)}".strip())
        if resp.status_code >= 300:
            raise ApiError(f"Unerwartete Antwort ({what}): HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise TransientError(f"Ungültige JSON-Antwort ({what})") from None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise TransientError(f"Antwort ohne access_token ({what})")
        return payload

    def _store_tokens(self, payload: Dict[str, Any]) -> None:
        old = self.tokens or {}
        expires_at = payload.get("expires_at")
        if not expires_at and payload.get("expires_in"):
            expires_at = int(self.clock()) + int(payload["expires_in"])
        tokens = {
            "access_token": payload["access_token"],
            # Strava kann bei jedem Refresh einen neuen refresh_token liefern;
            # der alte ist dann sofort ungültig -> immer den neuesten speichern.
            "refresh_token": payload.get("refresh_token") or old.get("refresh_token"),
            "expires_at": int(expires_at or 0),
            "scope": payload.get("scope", old.get("scope")),
        }
        athlete = payload.get("athlete")
        if isinstance(athlete, dict) and athlete.get("id"):
            tokens["athlete_id"] = athlete["id"]
        elif old.get("athlete_id"):
            tokens["athlete_id"] = old["athlete_id"]
        self.tokens = tokens
        self._register_tokens()
        write_json_atomic(self.token_path, tokens, private=True)

    def exchange_code(self, code: str) -> Dict[str, Any]:
        RedactingFormatter.register(code)
        payload = self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            "Code-Tausch",
        )
        self._store_tokens(payload)
        return payload

    def refresh(self) -> None:
        if not self.has_tokens():
            raise AuthError("Kein Refresh-Token vorhanden")
        payload = self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.tokens["refresh_token"],  # type: ignore[index]
            },
            "Token-Erneuerung",
        )
        self._store_tokens(payload)
        log.info("Access-Token erneuert (gültig bis %s)", fmt_utc(self.tokens["expires_at"]))  # type: ignore[index]

    def access_token(self) -> str:
        if not self.has_tokens():
            raise AuthError("Kein Token vorhanden")
        if int(self.tokens.get("expires_at") or 0) - TOKEN_REFRESH_MARGIN <= self.clock():  # type: ignore[union-attr]
            self.refresh()
        return str(self.tokens["access_token"])  # type: ignore[index]

    # -- Rate-Limits --------------------------------------------------------- #
    def _update_rate_info(self, headers: Any) -> None:
        entries = []
        for prefix in ("X-RateLimit", "X-ReadRateLimit"):
            limit = headers.get(f"{prefix}-Limit")
            usage = headers.get(f"{prefix}-Usage")
            if not limit or not usage:
                continue
            try:
                l15, lday = (int(x) for x in str(limit).split(",")[:2])
                u15, uday = (int(x) for x in str(usage).split(",")[:2])
            except ValueError:
                continue
            entries.append((u15, l15, uday, lday))
            log.debug("%s: %s/%s (15 min), %s/%s (Tag)", prefix, u15, l15, uday, lday)
        if entries:
            now = int(self.clock())
            self._rate_info = (now // RATE_WINDOW, now // 86400, entries)

    def _check_rate_budget(self) -> None:
        """Vorab prüfen, ob das zuletzt gemeldete Limit bereits erreicht ist."""
        if not self._rate_info:
            return
        now = self.clock()
        window, day, entries = self._rate_info
        for u15, l15, uday, lday in entries:
            if day == int(now) // 86400 and lday and uday >= lday:
                raise RateLimitError(next_utc_midnight(now), "Tageslimit")
            if window == int(now) // RATE_WINDOW and l15 and u15 >= l15:
                raise RateLimitError(next_rate_window(now))

    # -- API ----------------------------------------------------------------- #
    def _api(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
             json_body: Optional[Dict[str, Any]] = None, _retried: bool = False) -> Any:
        self._check_rate_budget()
        token = self.access_token()
        try:
            resp = self.session.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as err:
            raise TransientError(f"Netzwerkfehler bei {method} {path}: {type(err).__name__}") from None
        self._update_rate_info(resp.headers)
        status = resp.status_code
        if status == 401 and not _retried:
            self.refresh()
            return self._api(method, path, params=params, json_body=json_body, _retried=True)
        if status == 401:
            raise AuthError(f"Strava lehnt das Token ab (HTTP 401) {safe_error_text(resp)}".strip())
        if status == 429:
            raise RateLimitError(next_rate_window(self.clock()))
        if status >= 500:
            raise TransientError(f"Strava-Serverfehler {status} bei {method} {path}")
        if status == 403:
            raise ApiError(
                f"Keine Berechtigung für {method} {path} (HTTP 403) – wurden beim Autorisieren "
                f"beide Haken (activity:read_all, activity:write) gesetzt? {safe_error_text(resp)}".strip()
            )
        if status >= 400:
            raise ApiError(f"HTTP {status} bei {method} {path} {safe_error_text(resp)}".strip())
        try:
            return resp.json()
        except ValueError:
            raise TransientError(f"Ungültige JSON-Antwort bei {method} {path}") from None

    def list_activities(self, after: Optional[float]) -> List[Dict[str, Any]]:
        activities: List[Dict[str, Any]] = []
        page = 1
        while True:
            params: Dict[str, Any] = {"page": page, "per_page": PER_PAGE}
            if after is not None:
                params["after"] = int(after)
            batch = self._api("GET", "/athlete/activities", params=params)
            if not isinstance(batch, list):
                raise TransientError("Unerwartete Antwort von /athlete/activities")
            activities.extend(a for a in batch if isinstance(a, dict))
            if len(batch) < PER_PAGE:
                return activities
            page += 1

    def hide_from_home(self, activity_id: int) -> Optional[bool]:
        """Setzt hide_from_home=true. Gibt den Wert aus der Antwort zurück (falls vorhanden)."""
        body = self._api("PUT", f"/activities/{activity_id}", json_body={"hide_from_home": True})
        if isinstance(body, dict) and "hide_from_home" in body:
            return bool(body["hide_from_home"])
        return None


# --------------------------------------------------------------------------- #
# Zyklus-Logik
# --------------------------------------------------------------------------- #
@dataclass
class CycleResult:
    checked: int = 0
    matched: int = 0
    muted: List[str] = field(default_factory=list)
    would_mute: List[str] = field(default_factory=list)
    already_processed: int = 0
    already_hidden: int = 0
    before_first_start: int = 0
    backfill: bool = False


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


class Muter:
    def __init__(self, client: StravaClient, state: State, options: Options,
                 clock: Callable[[], float] = time.time):
        self.client = client
        self.state = state
        self.options = options
        self.clock = clock
        self._dry_backfill_done = False

    def _backfill_pending(self) -> bool:
        if not self.options.backfill:
            return False
        if self.options.dry_run:
            return not self._dry_backfill_done
        return not self.state.backfill_done

    def run_cycle(self) -> CycleResult:
        opts = self.options
        now = self.clock()
        result = CycleResult(backfill=self._backfill_pending())
        after = None if result.backfill else now - opts.lookback_hours * 3600
        if result.backfill:
            log.info("Backfill: lade alle Aktivitäten (einmalig)…")
        activities = self.client.list_activities(after)
        result.checked = len(activities)
        try:
            for activity in sorted(activities, key=lambda a: activity_start_ts(a) or 0):
                self._handle(activity, result)
        finally:
            self.state.save()

        if result.backfill:
            if opts.dry_run:
                self._dry_backfill_done = True
            else:
                self.state.backfill_done = True
                self.state.save()
            log.info("Backfill abgeschlossen.")
        self._log_summary(result)
        return result

    def _handle(self, activity: Dict[str, Any], result: CycleResult) -> None:
        opts = self.options
        activity_id = activity.get("id")
        if not isinstance(activity_id, int):
            return
        if activity.get("sport_type") not in opts.sport_types:
            return
        result.matched += 1
        label = activity_label(activity)

        if self.state.is_processed(activity_id):
            result.already_processed += 1
            return
        if not opts.backfill:
            started = activity_start_ts(activity)
            if started is None or started < self.state.first_start:
                result.before_first_start += 1
                log.debug("Übersprungen (vor erstem Start): %s", label)
                return
        if activity.get("hide_from_home") is True:
            result.already_hidden += 1
            self.state.mark_processed(activity_id)
            log.info("Bereits stummgeschaltet: %s", label)
            return
        if opts.dry_run:
            result.would_mute.append(label)
            log.info("[Dry-Run] Würde stummschalten: %s", label)
            return

        confirmed = self.client.hide_from_home(activity_id)
        if confirmed is False:
            log.warning("Strava meldet hide_from_home=false nach dem Update: %s – erneuter Versuch im nächsten Zyklus", label)
            return
        self.state.mark_processed(activity_id)
        self.state.save()
        result.muted.append(label)
        log.info("Stummgeschaltet: %s", label)

    def _log_summary(self, r: CycleResult) -> None:
        kinds = "/".join(self.options.sport_types)
        checked = _plural(r.checked, "Aktivität", "Aktivitäten") + " geprüft"
        if self.options.dry_run:
            line = f"[Dry-Run] {checked}, {len(r.would_mute)} {kinds} würden stummgeschaltet"
            items = r.would_mute
        else:
            line = f"{checked}, {len(r.muted)} {kinds} stummgeschaltet"
            items = r.muted
        if items:
            line += ": " + "; ".join(items)
        extras = []
        if r.already_processed:
            extras.append(f"{r.already_processed} bereits bearbeitet")
        if r.already_hidden:
            extras.append(f"{r.already_hidden} waren schon stumm")
        if r.before_first_start:
            extras.append(f"{r.before_first_start} vor dem ersten Start (backfill aus)")
        if extras:
            line += " (" + ", ".join(extras) + ")"
        log.info(line)


# --------------------------------------------------------------------------- #
# Autorisierung und Hauptschleife
# --------------------------------------------------------------------------- #
def log_auth_instructions(client: StravaClient) -> None:
    log.warning("Strava ist noch nicht (oder nicht mehr) autorisiert. So geht's:")
    log.warning("1) Diese URL im Browser öffnen und 'Autorisieren' klicken (beide Haken gesetzt lassen):")
    log.warning("   %s", client.authorization_url())
    log.warning("2) Der Browser landet auf einer nicht ladbaren Seite http://localhost/?state=&code=...&scope=...")
    log.warning("   Den Wert hinter 'code=' (bis zum nächsten '&') in die Option 'auth_code' kopieren.")
    log.warning("3) Speichern und das Add-on neu starten.")


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def authorize(client: StravaClient, options: Options, state: State) -> bool:
    """True, wenn gültige Tokens vorliegen. Wirft TransientError/RateLimitError."""
    code = options.auth_code
    if code and code_hash(code) != state.used_code_hash:
        log.info("Neuer Autorisierungscode gefunden – tausche ihn gegen Tokens…")
        try:
            payload = client.exchange_code(code)
        except AuthError as err:
            log.error("%s", err)
            log.error("Der Code ist ungültig, abgelaufen oder schon benutzt (Codes gelten nur kurz und einmal).")
            log_auth_instructions(client)
            return False
        state.used_code_hash = code_hash(code)
        state.save()
        granted = payload.get("scope")
        if isinstance(granted, str):
            missing = [s for s in REQUIRED_SCOPES if s not in granted.replace(",", " ").split()]
            if missing:
                log.warning("Achtung: Folgende Berechtigungen fehlen: %s – bitte erneut autorisieren.", ", ".join(missing))
        log.info("Autorisierung erfolgreich, Tokens gespeichert. Die Option 'auth_code' kann geleert werden.")
        return True
    if client.has_tokens():
        return True
    log_auth_instructions(client)
    return False


def describe_start(options: Options, state: State) -> None:
    first = dt.datetime.fromtimestamp(state.first_start, dt.timezone.utc).strftime("%d.%m.%Y %H:%M UTC")
    log.info("Strava Walk Muter %s gestartet", VERSION)
    log.info(
        "Modus: %s | Sportarten: %s | Intervall: %s min | Rückblick: %s h | Backfill: %s",
        "DRY-RUN (es wird nichts geändert)" if options.dry_run else "AKTIV",
        ", ".join(options.sport_types),
        options.poll_interval_minutes,
        options.lookback_hours,
        "an" if options.backfill else "aus",
    )
    if state.is_new:
        log.info("Erster Start – bearbeitet werden Aktivitäten ab %s", first)
    else:
        log.info("Bearbeitet werden Aktivitäten ab dem ersten Start (%s)%s", first,
                 ", plus ältere (backfill)" if options.backfill else "")


def run(stop: threading.Event, data_dir: str = DATA_DIR, session: Any = None,
        clock: Callable[[], float] = time.time) -> None:
    options = load_options(os.path.join(data_dir, "options.json"))
    RedactingFormatter.register(options.client_secret)
    RedactingFormatter.register(options.auth_code)

    if not options.client_id.isdigit() or not options.client_secret:
        log.error("Bitte 'client_id' (Zahl) und 'client_secret' deiner Strava-App in den Optionen eintragen und neu starten.")
        stop.wait()
        return

    state = State(os.path.join(data_dir, "state.json"), clock())
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = f"strava-walk-muter-ha-addon/{VERSION}"
    client = StravaClient(session, options.client_id, options.client_secret,
                          os.path.join(data_dir, "tokens.json"), clock)
    muter = Muter(client, state, options, clock)
    describe_start(options, state)

    interval = options.poll_interval_minutes * 60
    authorized = False
    while not stop.is_set():
        delay: float = interval
        try:
            if not authorized:
                authorized = authorize(client, options, state)
                if not authorized:
                    log.info("Warte auf Neustart mit gültigem 'auth_code'.")
                    stop.wait()
                    return
            muter.run_cycle()
        except RateLimitError as err:
            delay = max(1.0, err.retry_at - clock())
            log.warning("Strava-Rate-Limit erreicht (%s) – warte bis %s", err.reason, fmt_utc(err.retry_at))
        except AuthError as err:
            log.error("%s", err)
            log_auth_instructions(client)
            log.info("Warte auf Neustart mit neuem 'auth_code'.")
            stop.wait()
            return
        except TransientError as err:
            log.warning("%s – nächster Versuch in %s min", err, options.poll_interval_minutes)
        except ApiError as err:
            log.error("%s – nächster Versuch in %s min", err, options.poll_interval_minutes)
        except Exception:  # noqa: BLE001 – der Container darf nicht abstürzen
            log.exception("Unerwarteter Fehler – nächster Versuch in %s min", options.poll_interval_minutes)
        stop.wait(delay)


def main() -> None:
    setup_logging()
    stop = threading.Event()

    def _handle_signal(signum: int, _frame: Any) -> None:
        log.info("Signal %s empfangen – beende.", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        run(stop)
    except Exception:  # noqa: BLE001
        log.exception("Schwerer Fehler beim Start – das Add-on bleibt aktiv, bitte Log prüfen und neu starten.")
        stop.wait()
    log.info("Beendet.")


if __name__ == "__main__":
    main()
