"""Unit-Tests mit gemockten HTTP-Antworten (nur unittest + requests).

Ausführen (im Add-on-Ordner):
    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import strava_walk_muter as swm  # noqa: E402

NOW = 1_763_380_800  # 2025-11-17 12:00:00 UTC (fester Testzeitpunkt, liegt auf xx:00)
FIRST_START = NOW - 7 * 86400


def iso(ts: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def activity(aid: int, sport: str = "Walk", start: float = NOW - 3600, name: str = "Runde", **extra: Any) -> Dict[str, Any]:
    data = {"id": aid, "name": name, "sport_type": sport, "type": sport,
            "start_date": iso(start), "start_date_local": iso(start + 3600)}
    data.update(extra)
    return data


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: Optional[Dict[str, str]] = None):
        self.status_code = status
        self._body = body
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Gibt vorbereitete Antworten in Reihenfolge zurück und protokolliert Aufrufe."""

    def __init__(self, responses: List[Any]):
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"Unerwarteter Request: {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)


class Clock:
    def __init__(self, now: float = NOW):
        self.now = now

    def __call__(self) -> float:
        return self.now


class RecordingStop(threading.Event):
    """Event, dessen wait() nicht blockiert, sondern die Wartezeit protokolliert."""

    def __init__(self, max_waits: int = 1):
        super().__init__()
        self.waits: List[Optional[float]] = []
        self.max_waits = max_waits

    def wait(self, timeout: Optional[float] = None) -> bool:  # type: ignore[override]
        self.waits.append(timeout)
        if timeout is None or len(self.waits) >= self.max_waits:
            self.set()
        return self.is_set()


VALID_TOKENS = {"access_token": "ACCESS-OLD-123456", "refresh_token": "REFRESH-OLD-123456",
                "expires_at": NOW + 6 * 3600}


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.clock = Clock()
        self.log_stream = io.StringIO()
        handler = logging.StreamHandler(self.log_stream)
        handler.setFormatter(swm.RedactingFormatter("%(levelname)s %(message)s"))
        root = logging.getLogger()
        self._old_handlers = root.handlers[:]
        root.handlers[:] = [handler]
        root.setLevel(logging.INFO)
        swm.log.setLevel(logging.NOTSET)

    def tearDown(self) -> None:
        logging.getLogger().handlers[:] = self._old_handlers
        self.tmp.cleanup()

    @property
    def logs(self) -> str:
        return self.log_stream.getvalue()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write_tokens(self, tokens: Dict[str, Any]) -> None:
        with open(self.path("tokens.json"), "w") as fh:
            json.dump(tokens, fh)

    def write_options(self, **overrides: Any) -> None:
        opts = {"client_id": "12345", "client_secret": "SECRET-abcdef", "auth_code": "",
                "poll_interval_minutes": 15, "lookback_hours": 48, "sport_types": ["Walk"],
                "backfill": False, "dry_run": False}
        opts.update(overrides)
        with open(self.path("options.json"), "w") as fh:
            json.dump(opts, fh)

    def make(self, responses: List[Any], tokens: Optional[Dict[str, Any]] = VALID_TOKENS, **opt: Any):
        if tokens is not None:
            self.write_tokens(tokens)
        session = FakeSession(responses)
        client = swm.StravaClient(session, "12345", "SECRET-abcdef", self.path("tokens.json"), self.clock)
        state = swm.State(self.path("state.json"), FIRST_START)
        options = swm.Options(client_id="12345", client_secret="SECRET-abcdef",
                              **{"dry_run": False, **opt})
        muter = swm.Muter(client, state, options, self.clock)
        return session, client, state, muter

    def puts(self, session: FakeSession) -> List[Dict[str, Any]]:
        return [c for c in session.calls if c["method"] == "PUT"]


# --------------------------------------------------------------------------- #
class TokenRefreshTests(BaseCase):
    def test_expired_token_is_refreshed_and_new_refresh_token_persisted(self) -> None:
        expired = {**VALID_TOKENS, "expires_at": NOW - 10}
        session, client, _, muter = self.make([
            FakeResponse(200, {"token_type": "Bearer", "access_token": "ACCESS-NEW-654321",
                               "refresh_token": "REFRESH-NEW-654321", "expires_at": NOW + 21600,
                               "expires_in": 21600}),
            FakeResponse(200, []),
        ], tokens=expired)

        muter.run_cycle()

        token_call = session.calls[0]
        self.assertEqual(token_call["url"], swm.TOKEN_URL)
        self.assertEqual(token_call["data"]["grant_type"], "refresh_token")
        self.assertEqual(token_call["data"]["refresh_token"], "REFRESH-OLD-123456")
        self.assertEqual(session.calls[1]["headers"]["Authorization"], "Bearer ACCESS-NEW-654321")
        with open(self.path("tokens.json")) as fh:
            stored = json.load(fh)
        self.assertEqual(stored["refresh_token"], "REFRESH-NEW-654321")
        self.assertEqual(stored["access_token"], "ACCESS-NEW-654321")
        self.assertEqual(os.stat(self.path("tokens.json")).st_mode & 0o777, 0o600)
        for secret in ("ACCESS-NEW-654321", "REFRESH-NEW-654321", "REFRESH-OLD-123456", "SECRET-abcdef"):
            self.assertNotIn(secret, self.logs)

    def test_refresh_keeps_old_refresh_token_if_none_returned(self) -> None:
        expired = {**VALID_TOKENS, "expires_at": NOW + 60}  # innerhalb der Marge
        session, client, _, _ = self.make([
            FakeResponse(200, {"access_token": "ACCESS-NEW-654321", "expires_at": NOW + 21600}),
        ], tokens=expired)
        self.assertEqual(client.access_token(), "ACCESS-NEW-654321")
        with open(self.path("tokens.json")) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "REFRESH-OLD-123456")

    def test_valid_token_is_not_refreshed(self) -> None:
        session, client, _, _ = self.make([])
        self.assertEqual(client.access_token(), "ACCESS-OLD-123456")
        self.assertEqual(session.calls, [])

    def test_401_triggers_refresh_and_retry(self) -> None:
        session, _, _, muter = self.make([
            FakeResponse(401, {"message": "Authorization Error"}),
            FakeResponse(200, {"access_token": "ACCESS-NEW-654321", "refresh_token": "REFRESH-NEW-654321",
                               "expires_at": NOW + 21600}),
            FakeResponse(200, []),
        ])
        muter.run_cycle()
        self.assertEqual(session.calls[2]["headers"]["Authorization"], "Bearer ACCESS-NEW-654321")

    def test_rejected_refresh_raises_auth_error(self) -> None:
        expired = {**VALID_TOKENS, "expires_at": NOW - 10}
        _, client, _, _ = self.make([
            FakeResponse(400, {"message": "Bad Request",
                               "errors": [{"resource": "RefreshToken", "field": "refresh_token", "code": "invalid"}]}),
        ], tokens=expired)
        with self.assertRaises(swm.AuthError):
            client.access_token()


class SportTypeFilterTests(BaseCase):
    def test_only_configured_sport_types_are_muted(self) -> None:
        acts = [activity(1, "Walk", name="Abendrunde"), activity(2, "Run"), activity(3, "Ride"),
                activity(4, "Hike")]
        session, _, state, muter = self.make([
            FakeResponse(200, acts),
            FakeResponse(200, {"id": 1, "hide_from_home": True}),
        ])
        result = muter.run_cycle()

        puts = self.puts(session)
        self.assertEqual(len(puts), 1)
        self.assertTrue(puts[0]["url"].endswith("/activities/1"))
        self.assertEqual(puts[0]["json"], {"hide_from_home": True})
        self.assertNotIn("private", puts[0]["json"])  # Sichtbarkeit wird nie angefasst
        self.assertEqual(result.checked, 4)
        self.assertEqual(len(result.muted), 1)
        self.assertTrue(state.is_processed(1))
        self.assertIn("4 Aktivitäten geprüft, 1 Walk stummgeschaltet: Abendrunde (17.11.2025 12:00)", self.logs)

    def test_multiple_sport_types(self) -> None:
        acts = [activity(1, "Walk"), activity(2, "Hike"), activity(3, "Run")]
        session, _, _, muter = self.make([
            FakeResponse(200, acts), FakeResponse(200, {"id": 1}), FakeResponse(200, {"id": 2}),
        ], sport_types=("Walk", "Hike"))
        muter.run_cycle()
        self.assertEqual([p["url"].rsplit("/", 1)[1] for p in self.puts(session)], ["1", "2"])

    def test_list_request_uses_after_lookback(self) -> None:
        session, _, _, muter = self.make([FakeResponse(200, [])], lookback_hours=48)
        muter.run_cycle()
        params = session.calls[0]["params"]
        self.assertEqual(params["after"], NOW - 48 * 3600)
        self.assertEqual(session.calls[0]["url"], f"{swm.API_BASE}/athlete/activities")

    def test_pagination(self) -> None:
        page1 = [activity(i, "Run") for i in range(swm.PER_PAGE)]
        session, _, _, muter = self.make([FakeResponse(200, page1), FakeResponse(200, [activity(999)]),
                                          FakeResponse(200, {"id": 999})])
        result = muter.run_cycle()
        self.assertEqual(result.checked, swm.PER_PAGE + 1)
        self.assertEqual(session.calls[1]["params"]["page"], 2)


class ProcessedIdTests(BaseCase):
    def test_processed_ids_are_skipped_and_persisted(self) -> None:
        session, _, _, muter = self.make([FakeResponse(200, [activity(1)]), FakeResponse(200, {"id": 1})])
        muter.run_cycle()

        # neuer Prozess: Zustand aus Datei laden
        session2, _, state2, muter2 = self.make([FakeResponse(200, [activity(1), activity(2)]),
                                                 FakeResponse(200, {"id": 2})])
        self.assertTrue(state2.is_processed(1))
        result = muter2.run_cycle()
        self.assertEqual([p["url"].rsplit("/", 1)[1] for p in self.puts(session2)], ["2"])
        self.assertEqual(result.already_processed, 1)

    def test_already_hidden_activity_is_recorded_without_put(self) -> None:
        session, _, state, muter = self.make([FakeResponse(200, [activity(5, hide_from_home=True)])])
        muter.run_cycle()
        self.assertEqual(self.puts(session), [])
        self.assertTrue(state.is_processed(5))

    def test_activities_before_first_start_are_skipped_without_backfill(self) -> None:
        old = activity(7, start=FIRST_START - 3600)
        session, _, state, muter = self.make([FakeResponse(200, [old])])
        result = muter.run_cycle()
        self.assertEqual(self.puts(session), [])
        self.assertEqual(result.before_first_start, 1)
        self.assertFalse(state.is_processed(7))

    def test_backfill_scans_full_history_once(self) -> None:
        old = activity(7, start=FIRST_START - 30 * 86400)
        session, _, state, muter = self.make([FakeResponse(200, [old]), FakeResponse(200, {"id": 7}),
                                              FakeResponse(200, [])], backfill=True)
        muter.run_cycle()
        self.assertNotIn("after", session.calls[0]["params"])
        self.assertEqual(len(self.puts(session)), 1)
        self.assertTrue(state.backfill_done)
        muter.run_cycle()  # zweiter Zyklus wieder mit Rückblickfenster
        self.assertIn("after", session.calls[2]["params"])

    def test_failed_confirmation_is_not_marked(self) -> None:
        session, _, state, muter = self.make([FakeResponse(200, [activity(1)]),
                                              FakeResponse(200, {"id": 1, "hide_from_home": False})])
        muter.run_cycle()
        self.assertFalse(state.is_processed(1))


class DryRunTests(BaseCase):
    def test_dry_run_makes_no_changes(self) -> None:
        session, _, state, muter = self.make([FakeResponse(200, [activity(1, name="Spaziergang"), activity(2, "Run")])],
                                             dry_run=True)
        result = muter.run_cycle()
        self.assertEqual(self.puts(session), [])
        self.assertEqual(len(result.would_mute), 1)
        self.assertFalse(state.is_processed(1))  # später im echten Modus noch bearbeiten
        self.assertIn("[Dry-Run] Würde stummschalten: Spaziergang", self.logs)
        self.assertIn("[Dry-Run] 2 Aktivitäten geprüft, 1 Walk würden stummgeschaltet", self.logs)

    def test_dry_run_backfill_does_not_persist_done_flag(self) -> None:
        _, _, state, muter = self.make([FakeResponse(200, []), FakeResponse(200, [])], dry_run=True, backfill=True)
        muter.run_cycle()
        self.assertFalse(state.backfill_done)
        self.assertFalse(muter._backfill_pending())  # in diesem Prozess nicht wiederholen


class RateLimitTests(BaseCase):
    def test_429_on_list_waits_until_next_window(self) -> None:
        self.clock.now = NOW + 7 * 60  # 12:07 -> nächstes Fenster 12:15
        _, _, _, muter = self.make([FakeResponse(429, {"message": "Rate Limit Exceeded"})])
        with self.assertRaises(swm.RateLimitError) as ctx:
            muter.run_cycle()
        self.assertEqual(ctx.exception.retry_at, NOW + 15 * 60 + swm.RATE_WINDOW_BUFFER)

    def test_429_during_put_keeps_progress(self) -> None:
        session, _, _, muter = self.make([
            FakeResponse(200, [activity(1, start=NOW - 7200), activity(2, start=NOW - 3600)]),
            FakeResponse(200, {"id": 1}),
            FakeResponse(429, {"message": "Rate Limit Exceeded"}),
        ])
        with self.assertRaises(swm.RateLimitError):
            muter.run_cycle()
        reloaded = swm.State(self.path("state.json"), NOW)
        self.assertTrue(reloaded.is_processed(1))
        self.assertFalse(reloaded.is_processed(2))

    def test_headers_at_limit_block_next_request_in_same_window(self) -> None:
        session, client, _, _ = self.make([
            FakeResponse(200, [], {"X-RateLimit-Limit": "200,2000", "X-RateLimit-Usage": "12,40",
                                   "X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": "100,140"}),
        ])
        client.list_activities(None)
        with self.assertRaises(swm.RateLimitError):
            client.list_activities(None)
        self.assertEqual(len(session.calls), 1)  # kein weiterer Request
        self.clock.now = NOW + 15 * 60 + 10  # neues Fenster -> wieder erlaubt
        session.responses.append(FakeResponse(200, []))
        client.list_activities(None)
        self.assertEqual(len(session.calls), 2)

    def test_daily_limit_waits_until_midnight(self) -> None:
        _, client, _, _ = self.make([
            FakeResponse(200, [], {"X-ReadRateLimit-Limit": "100,1000", "X-ReadRateLimit-Usage": "5,1000"}),
        ])
        client.list_activities(None)
        with self.assertRaises(swm.RateLimitError) as ctx:
            client.list_activities(None)
        self.assertEqual(ctx.exception.retry_at, (NOW // 86400 + 1) * 86400 + swm.RATE_WINDOW_BUFFER)

    def test_run_loop_sleeps_until_window_after_429(self) -> None:
        self.write_options()
        self.write_tokens(VALID_TOKENS)
        self.clock.now = NOW + 7 * 60
        session = FakeSession([FakeResponse(429, {"message": "Rate Limit Exceeded"})])
        stop = RecordingStop(max_waits=1)
        swm.run(stop, data_dir=self.dir, session=session, clock=self.clock)
        self.assertAlmostEqual(stop.waits[0], 8 * 60 + swm.RATE_WINDOW_BUFFER)
        self.assertIn("Rate-Limit", self.logs)


class RobustnessTests(BaseCase):
    def test_5xx_and_network_errors_do_not_crash_loop(self) -> None:
        self.write_options(poll_interval_minutes=15)
        self.write_tokens(VALID_TOKENS)
        swm.State(self.path("state.json"), FIRST_START)  # erster Start liegt eine Woche zurück
        session = FakeSession([
            FakeResponse(503, {"message": "Service Unavailable"}),
            requests.ConnectionError("boom"),
            FakeResponse(200, [activity(1)]),
            FakeResponse(200, {"id": 1}),
        ])
        stop = RecordingStop(max_waits=3)
        swm.run(stop, data_dir=self.dir, session=session, clock=self.clock)
        self.assertEqual(stop.waits, [900, 900, 900])
        self.assertIn("Serverfehler 503", self.logs)
        self.assertIn("Netzwerkfehler", self.logs)
        self.assertIn("1 Walk stummgeschaltet", self.logs)

    def test_unexpected_exception_does_not_crash_loop(self) -> None:
        self.write_options()
        self.write_tokens(VALID_TOKENS)
        session = FakeSession([FakeResponse(200, ValueError("kaputt"))])
        stop = RecordingStop(max_waits=1)
        swm.run(stop, data_dir=self.dir, session=session, clock=self.clock)
        self.assertEqual(stop.waits, [900])


class AuthorizationTests(BaseCase):
    def test_without_tokens_logs_authorization_url_and_waits(self) -> None:
        self.write_options()
        stop = RecordingStop()
        swm.run(stop, data_dir=self.dir, session=FakeSession([]), clock=self.clock)
        self.assertEqual(stop.waits, [None])
        self.assertIn("https://www.strava.com/oauth/authorize?client_id=12345&redirect_uri=http://localhost"
                      "&response_type=code&approval_prompt=force&scope=activity:read_all,activity:write", self.logs)

    def test_auth_code_is_exchanged_once(self) -> None:
        self.write_options(auth_code="CODE-xyz987654")
        session = FakeSession([
            FakeResponse(200, {"token_type": "Bearer", "access_token": "ACCESS-NEW-654321",
                               "refresh_token": "REFRESH-NEW-654321", "expires_at": NOW + 21600,
                               "athlete": {"id": 42}}),
            FakeResponse(200, []),
        ])
        swm.run(RecordingStop(), data_dir=self.dir, session=session, clock=self.clock)
        self.assertEqual(session.calls[0]["data"]["grant_type"], "authorization_code")
        self.assertEqual(session.calls[0]["data"]["code"], "CODE-xyz987654")
        with open(self.path("tokens.json")) as fh:
            self.assertEqual(json.load(fh)["refresh_token"], "REFRESH-NEW-654321")
        for secret in ("CODE-xyz987654", "ACCESS-NEW-654321", "REFRESH-NEW-654321", "SECRET-abcdef"):
            self.assertNotIn(secret, self.logs)

        # Neustart mit demselben (verbrauchten) Code: kein zweiter Tausch
        session2 = FakeSession([FakeResponse(200, [])])
        swm.run(RecordingStop(), data_dir=self.dir, session=session2, clock=self.clock)
        self.assertEqual([c["method"] for c in session2.calls], ["GET"])

    def test_invalid_code_logs_url_without_code(self) -> None:
        self.write_options(auth_code="CODE-bad123456")
        session = FakeSession([FakeResponse(400, {"message": "Bad Request",
                                                  "errors": [{"resource": "AuthorizationCode", "field": "code",
                                                              "code": "invalid"}]})])
        stop = RecordingStop()
        swm.run(stop, data_dir=self.dir, session=session, clock=self.clock)
        self.assertEqual(stop.waits, [None])
        self.assertIn("AuthorizationCode/code/invalid", self.logs)
        self.assertIn("oauth/authorize", self.logs)
        self.assertNotIn("CODE-bad123456", self.logs)

    def test_missing_client_credentials(self) -> None:
        self.write_options(client_id="", client_secret="")
        stop = RecordingStop()
        swm.run(stop, data_dir=self.dir, session=FakeSession([]), clock=self.clock)
        self.assertIn("client_id", self.logs)
        self.assertEqual(stop.waits, [None])


class RedactionTests(BaseCase):
    def test_registered_secrets_are_redacted_even_in_tracebacks(self) -> None:
        swm.RedactingFormatter.register("TOPSECRET-999")
        try:
            raise RuntimeError("leak TOPSECRET-999")
        except RuntimeError:
            logging.getLogger("x").exception("value=%s", "TOPSECRET-999")
        self.assertNotIn("TOPSECRET-999", self.logs)
        self.assertIn("***", self.logs)


class OptionsTests(BaseCase):
    def test_defaults_and_parsing(self) -> None:
        opts = swm.load_options(self.path("missing.json"))
        self.assertTrue(opts.dry_run)
        self.assertEqual(opts.sport_types, ("Walk",))
        self.assertEqual(opts.poll_interval_minutes, 15)
        self.assertEqual(opts.lookback_hours, 48)
        self.assertFalse(opts.backfill)


if __name__ == "__main__":
    unittest.main()
