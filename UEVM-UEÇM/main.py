"""Core EPİAŞ UEVM / UEÇM services used by the unified Baha Enerji site.

The module intentionally uses only Python's standard library and is imported
by ``BAHA-ENERJI-WEBSITE/app.py``. EPİAŞ passwords are exchanged for temporary
TGT values and are never kept by the application.
"""

from __future__ import annotations

import io
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
TR_TZ = timezone(timedelta(hours=3))
MAX_RANGE_DAYS = 62
OFFICIAL_CAS_TGT_REQUESTS_PER_MINUTE = 100
OFFICIAL_CAS_TGT_BURST_PER_SECOND = 10
OFFICIAL_CAS_TGT_TTL_SECONDS = 8 * 60 * 60
DEFAULT_EPIAS_TGT_CACHE_SECONDS = 450 * 60
DEFAULT_AUTH_SESSION_MINUTES = 450
DEFAULT_EPIAS_TGT_REQUESTS_PER_MINUTE = 10
DEFAULT_EPIAS_TGT_BURST_PER_SECOND = 2
MAX_SAFE_EPIAS_TGT_REQUESTS_PER_MINUTE = 20
MAX_SAFE_EPIAS_TGT_BURST_PER_SECOND = 5
DEFAULT_EPIAS_API_REQUESTS_PER_MINUTE = 24
DEFAULT_EPIAS_API_BURST_PER_SECOND = 2
MAX_SAFE_EPIAS_API_REQUESTS_PER_MINUTE = 30
MAX_SAFE_EPIAS_API_BURST_PER_SECOND = 3
DEFAULT_EPIAS_JSON_CACHE_CURRENT_SECONDS = 300
DEFAULT_EPIAS_JSON_CACHE_LIVE_SECONDS = 120
DEFAULT_EPIAS_JSON_CACHE_RECENT_SECONDS = 900
DEFAULT_EPIAS_JSON_CACHE_HISTORY_SECONDS = 30 * 24 * 60 * 60
DEFAULT_EPIAS_FORCE_REFRESH_MIN_SECONDS = 60


def load_env_file(path: Path) -> None:
    """Load a simple .env file without overriding process environment values."""

    if not path.is_file():
        return
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f".env dosyasında geçersiz satır: {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise RuntimeError(f".env dosyasında geçersiz anahtar: {line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file(ROOT / ".env")

SOURCE_DEFINITIONS: tuple[dict[str, str], ...] = (
    {"id": "sun", "label": "Güneş", "group": "renewable"},
    {"id": "wind", "label": "Rüzgâr", "group": "renewable"},
    {"id": "dam", "label": "Barajlı", "group": "renewable"},
    {"id": "river", "label": "Akarsu", "group": "renewable"},
    {"id": "biomass", "label": "Biyokütle", "group": "renewable"},
    {"id": "geothermal", "label": "Jeotermal", "group": "renewable"},
    {"id": "importedCoal", "label": "İthal kömür", "group": "thermal"},
    {"id": "lignite", "label": "Linyit", "group": "thermal"},
    {"id": "stoneCoal", "label": "Taş kömürü", "group": "thermal"},
    {"id": "asphaltite", "label": "Asfaltit", "group": "thermal"},
    {"id": "fueloil", "label": "Fuel-oil", "group": "thermal"},
    {"id": "lng", "label": "LNG", "group": "thermal"},
    {"id": "naphtha", "label": "Nafta", "group": "thermal"},
    {"id": "naturalGas", "label": "Doğal gaz", "group": "natural_gas"},
    {"id": "other", "label": "Diğer", "group": "other"},
    {
        "id": "internationalImport",
        "label": "Uluslararası ithalat",
        "group": "other",
    },
    {
        "id": "internationalExport",
        "label": "Uluslararası ihracat",
        "group": "other",
    },
)

GROUP_DEFINITIONS: tuple[dict[str, str], ...] = (
    {"id": "renewable", "label": "Yenilenebilir"},
    {"id": "thermal", "label": "Termik"},
    {"id": "natural_gas", "label": "Doğal gaz"},
    {"id": "other", "label": "Diğer / Uluslararası"},
)


class EpiasError(RuntimeError):
    """A safe-to-display error raised by the EPİAŞ integration."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _safe_int_env(name: str, default: int, *, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


class RollingRateLimiter:
    """Thread-safe rolling-window limiter for EPİAŞ calls."""

    def __init__(
        self,
        *,
        per_minute: int,
        per_second: int,
        time_fn: Any = time.monotonic,
        sleep_fn: Any = time.sleep,
    ) -> None:
        self.per_minute = max(1, int(per_minute))
        self.per_second = max(1, int(per_second))
        self._time = time_fn
        self._sleep = sleep_fn
        self._minute_window: deque[float] = deque()
        self._second_window: deque[float] = deque()
        self._cooldown_until = 0.0
        self._total_acquired = 0
        self._total_waits = 0
        self._total_wait_seconds = 0.0
        self._total_backoffs = 0
        self._last_backoff_seconds = 0.0
        self._last_backoff_reason = ""
        self._last_event_at = ""
        self._lock = threading.Lock()

    def wait(self) -> None:
        waited = 0.0
        while True:
            with self._lock:
                now = self._time()
                self._drop_old(now)
                cooldown_wait = max(0.0, self._cooldown_until - now)
                minute_wait = (
                    60.0 - (now - self._minute_window[0])
                    if len(self._minute_window) >= self.per_minute
                    else 0.0
                )
                second_wait = (
                    1.0 - (now - self._second_window[0])
                    if len(self._second_window) >= self.per_second
                    else 0.0
                )
                wait_for = max(cooldown_wait, minute_wait, second_wait)
                if wait_for <= 0:
                    self._minute_window.append(now)
                    self._second_window.append(now)
                    self._total_acquired += 1
                    if waited > 0:
                        self._total_waits += 1
                        self._total_wait_seconds += waited
                    self._last_event_at = datetime.now(TR_TZ).isoformat(
                        timespec="seconds"
                    )
                    return
            sleep_for = min(wait_for, 1.0)
            waited += sleep_for
            self._sleep(sleep_for)

    def backoff(self, seconds: float | None = None, *, reason: str = "") -> None:
        delay = seconds if seconds is not None and seconds > 0 else 60.0
        delay = min(float(delay), 300.0)
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._time() + delay)
            self._total_backoffs += 1
            self._last_backoff_seconds = delay
            self._last_backoff_reason = reason
            self._last_event_at = datetime.now(TR_TZ).isoformat(timespec="seconds")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._time()
            self._drop_old(now)
            return {
                "perMinute": self.per_minute,
                "perSecond": self.per_second,
                "lastMinute": len(self._minute_window),
                "lastSecond": len(self._second_window),
                "cooldownSeconds": max(0.0, self._cooldown_until - now),
                "totalAcquired": self._total_acquired,
                "totalWaits": self._total_waits,
                "totalWaitSeconds": round(self._total_wait_seconds, 3),
                "totalBackoffs": self._total_backoffs,
                "lastBackoffSeconds": self._last_backoff_seconds,
                "lastBackoffReason": self._last_backoff_reason,
                "lastEventAt": self._last_event_at,
            }

    def _drop_old(self, now: float) -> None:
        while self._minute_window and now - self._minute_window[0] >= 60.0:
            self._minute_window.popleft()
        while self._second_window and now - self._second_window[0] >= 1.0:
            self._second_window.popleft()


def _retry_after_seconds(headers: Any) -> float | None:
    if not headers:
        return None
    raw = None
    for name in (
        "Retry-After",
        "X-Rate-Limit-Retry-After-Seconds",
        "X-RateLimit-Retry-After-Seconds",
    ):
        try:
            raw = headers.get(name)
        except AttributeError:
            raw = None
        if raw:
            break
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


class EpiasProtectionMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.http_requests = 0
        self.http_errors = 0
        self.error_counts: dict[str, int] = {}
        self.last_error: dict[str, Any] | None = None
        self.singleflight_started = 0
        self.singleflight_joined = 0
        self.singleflight_active = 0
        self.last_singleflight_key = ""
        self.last_request_at = ""

    def record_http_request(self, url: str) -> None:
        with self._lock:
            self.http_requests += 1
            self.last_request_at = datetime.now(TR_TZ).isoformat(timespec="seconds")

    def record_http_error(self, *, url: str, status_code: int, detail: str = "") -> None:
        key = str(status_code)
        with self._lock:
            self.http_errors += 1
            self.error_counts[key] = self.error_counts.get(key, 0) + 1
            self.last_error = {
                "statusCode": status_code,
                "detail": detail[:180],
                "url": _safe_url_label(url),
                "at": datetime.now(TR_TZ).isoformat(timespec="seconds"),
            }

    def record_singleflight_start(self, key: str) -> None:
        with self._lock:
            self.singleflight_started += 1
            self.singleflight_active += 1
            self.last_singleflight_key = key[:180]

    def record_singleflight_join(self, key: str) -> None:
        with self._lock:
            self.singleflight_joined += 1
            self.last_singleflight_key = key[:180]

    def record_singleflight_end(self) -> None:
        with self._lock:
            self.singleflight_active = max(0, self.singleflight_active - 1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "httpRequests": self.http_requests,
                "httpErrors": self.http_errors,
                "errorCounts": dict(self.error_counts),
                "lastError": dict(self.last_error) if self.last_error else None,
                "singleflightStarted": self.singleflight_started,
                "singleflightJoined": self.singleflight_joined,
                "singleflightActive": self.singleflight_active,
                "lastSingleflightKey": self.last_singleflight_key,
                "lastRequestAt": self.last_request_at,
            }


class SingleFlight:
    def __init__(self, monitor: EpiasProtectionMonitor) -> None:
        self._monitor = monitor
        self._lock = threading.Lock()
        self._flights: dict[str, dict[str, Any]] = {}

    def run(self, key: str, func: Any) -> Any:
        with self._lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = {"event": threading.Event()}
                self._flights[key] = flight
                owner = True
                self._monitor.record_singleflight_start(key)
            else:
                owner = False
                self._monitor.record_singleflight_join(key)
        if not owner:
            flight["event"].wait()
            if "exc" in flight:
                raise flight["exc"]
            return flight.get("result")
        try:
            flight["result"] = func()
            return flight["result"]
        except BaseException as exc:
            flight["exc"] = exc
            raise
        finally:
            flight["event"].set()
            with self._lock:
                self._flights.pop(key, None)
            self._monitor.record_singleflight_end()


def _safe_url_label(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url[:180]
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _singleflight_key(endpoint: str, payload: dict[str, Any]) -> str:
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return f"{endpoint}:{normalized}"


def _safe_cache_seconds_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _json_cache_enabled() -> bool:
    return os.getenv("BAHA_EPIAS_JSON_CACHE_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
    }


def _force_refresh_min_seconds() -> int:
    return _safe_cache_seconds_env(
        "BAHA_EPIAS_FORCE_REFRESH_MIN_SECONDS",
        DEFAULT_EPIAS_FORCE_REFRESH_MIN_SECONDS,
    )


def _json_cache_dir() -> Path:
    raw = os.getenv("BAHA_EPIAS_JSON_CACHE_DIR")
    if raw:
        return Path(raw).expanduser()
    return ROOT / ".epias-json-cache"


def _json_copy(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _cache_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _parse_cache_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _payload_date_values(payload: dict[str, Any]) -> list[date]:
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    dates: list[date] = []
    for match in re.finditer(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            dates.append(date.fromisoformat(match.group(0)))
        except ValueError:
            continue
    return dates


def _epias_json_cache_ttl(endpoint: str, payload: dict[str, Any]) -> int:
    dates = _payload_date_values(payload)
    today = datetime.now(TR_TZ).date()
    if not dates:
        return _safe_cache_seconds_env(
            "BAHA_EPIAS_JSON_CACHE_CURRENT_SECONDS",
            DEFAULT_EPIAS_JSON_CACHE_CURRENT_SECONDS,
        )
    latest = max(dates)
    if latest >= today:
        return _safe_cache_seconds_env(
            "BAHA_EPIAS_JSON_CACHE_LIVE_SECONDS",
            DEFAULT_EPIAS_JSON_CACHE_LIVE_SECONDS,
        )
    if latest >= today - timedelta(days=1):
        return _safe_cache_seconds_env(
            "BAHA_EPIAS_JSON_CACHE_RECENT_SECONDS",
            DEFAULT_EPIAS_JSON_CACHE_RECENT_SECONDS,
        )
    return _safe_cache_seconds_env(
        "BAHA_EPIAS_JSON_CACHE_HISTORY_SECONDS",
        DEFAULT_EPIAS_JSON_CACHE_HISTORY_SECONDS,
    )


class EpiasJsonCache:
    """Persist raw EPİAŞ JSON responses so repeated reads do not hit EPİAŞ."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._writes = 0
        self._errors = 0
        self._last_hit_at = ""
        self._last_write_at = ""

    def _key(self, api_base: str, endpoint: str, payload: dict[str, Any]) -> str:
        normalized = json.dumps(
            {
                "apiBase": api_base.rstrip("/"),
                "endpoint": endpoint,
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _path(self, api_base: str, endpoint: str, payload: dict[str, Any]) -> Path:
        cache_key = self._key(api_base, endpoint, payload)
        return _json_cache_dir() / cache_key[:2] / f"{cache_key}.json"

    def get(
        self,
        api_base: str,
        endpoint: str,
        payload: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any] | None:
        if not _json_cache_enabled():
            return None
        path = self._path(api_base, endpoint, payload)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            expires_at = _parse_cache_iso(record.get("expiresAt"))
            fetched_at = _parse_cache_iso(record.get("fetchedAt"))
            response = record.get("response")
        except (OSError, json.JSONDecodeError, TypeError):
            with self._lock:
                self._misses += 1
            return None
        now = datetime.now(timezone.utc)
        force_refresh_guarded = (
            force_refresh
            and fetched_at is not None
            and isinstance(response, dict)
            and (now - fetched_at).total_seconds() < _force_refresh_min_seconds()
        )
        if force_refresh and not force_refresh_guarded:
            with self._lock:
                self._misses += 1
            return None
        if (
            not force_refresh_guarded
            and (
                expires_at is None
                or expires_at <= now
            )
        ) or not isinstance(response, dict):
            with self._lock:
                self._misses += 1
            return None
        with self._lock:
            self._hits += 1
            self._last_hit_at = datetime.now(TR_TZ).isoformat(timespec="seconds")
        return _json_copy(response)

    def put(
        self,
        api_base: str,
        endpoint: str,
        payload: dict[str, Any],
        response: dict[str, Any],
    ) -> None:
        if not _json_cache_enabled():
            return
        ttl = _epias_json_cache_ttl(endpoint, payload)
        if ttl <= 0:
            return
        path = self._path(api_base, endpoint, payload)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        record = {
            "endpoint": endpoint,
            "payload": _json_copy(payload),
            "fetchedAt": _cache_iso_now(),
            "expiresAt": expires_at.replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "ttlSeconds": ttl,
            "response": _json_copy(response),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
            temp_path.write_text(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temp_path.replace(path)
        except OSError:
            with self._lock:
                self._errors += 1
            return
        with self._lock:
            self._writes += 1
            self._last_write_at = datetime.now(TR_TZ).isoformat(timespec="seconds")

    def snapshot(self) -> dict[str, Any]:
        cache_dir = _json_cache_dir()
        with self._lock:
            return {
                "enabled": _json_cache_enabled(),
                "directory": str(cache_dir),
                "hits": self._hits,
                "misses": self._misses,
                "writes": self._writes,
                "errors": self._errors,
                "lastHitAt": self._last_hit_at,
                "lastWriteAt": self._last_write_at,
                "ttlSeconds": {
                    "current": _safe_cache_seconds_env(
                        "BAHA_EPIAS_JSON_CACHE_CURRENT_SECONDS",
                        DEFAULT_EPIAS_JSON_CACHE_CURRENT_SECONDS,
                    ),
                    "live": _safe_cache_seconds_env(
                        "BAHA_EPIAS_JSON_CACHE_LIVE_SECONDS",
                        DEFAULT_EPIAS_JSON_CACHE_LIVE_SECONDS,
                    ),
                    "recent": _safe_cache_seconds_env(
                        "BAHA_EPIAS_JSON_CACHE_RECENT_SECONDS",
                        DEFAULT_EPIAS_JSON_CACHE_RECENT_SECONDS,
                    ),
                    "history": _safe_cache_seconds_env(
                        "BAHA_EPIAS_JSON_CACHE_HISTORY_SECONDS",
                        DEFAULT_EPIAS_JSON_CACHE_HISTORY_SECONDS,
                    ),
                },
                "forceRefreshMinimumSeconds": _force_refresh_min_seconds(),
            }


EPIAS_PROTECTION_MONITOR = EpiasProtectionMonitor()
EPIAS_SINGLE_FLIGHT = SingleFlight(EPIAS_PROTECTION_MONITOR)
EPIAS_JSON_CACHE = EpiasJsonCache()


EPIAS_TGT_RATE_LIMITER = RollingRateLimiter(
    per_minute=_safe_int_env(
        "EPIAS_TGT_REQUESTS_PER_MINUTE",
        DEFAULT_EPIAS_TGT_REQUESTS_PER_MINUTE,
        maximum=MAX_SAFE_EPIAS_TGT_REQUESTS_PER_MINUTE,
    ),
    per_second=_safe_int_env(
        "EPIAS_TGT_BURST_PER_SECOND",
        DEFAULT_EPIAS_TGT_BURST_PER_SECOND,
        maximum=MAX_SAFE_EPIAS_TGT_BURST_PER_SECOND,
    ),
)
EPIAS_API_RATE_LIMITER = RollingRateLimiter(
    per_minute=_safe_int_env(
        "EPIAS_API_REQUESTS_PER_MINUTE",
        DEFAULT_EPIAS_API_REQUESTS_PER_MINUTE,
        maximum=MAX_SAFE_EPIAS_API_REQUESTS_PER_MINUTE,
    ),
    per_second=_safe_int_env(
        "EPIAS_API_BURST_PER_SECOND",
        DEFAULT_EPIAS_API_BURST_PER_SECOND,
        maximum=MAX_SAFE_EPIAS_API_BURST_PER_SECOND,
    ),
)


def epias_protection_snapshot() -> dict[str, Any]:
    tgt = EPIAS_TGT_RATE_LIMITER.snapshot()
    api = EPIAS_API_RATE_LIMITER.snapshot()
    monitor = EPIAS_PROTECTION_MONITOR.snapshot()
    return {
        "generatedAt": datetime.now(TR_TZ).isoformat(timespec="seconds"),
        "status": (
            "cooldown"
            if tgt["cooldownSeconds"] > 0 or api["cooldownSeconds"] > 0
            else "ok"
        ),
        "official": {
            "tgt": {
                "usernamePerMinute": OFFICIAL_CAS_TGT_REQUESTS_PER_MINUTE,
                "usernamePerSecond": OFFICIAL_CAS_TGT_BURST_PER_SECOND,
                "ipPerMinute": 1000,
                "ipPerSecond": 100,
                "failedAttemptsPerWindow": 5,
                "failedAttemptsWindowSeconds": 10,
                "ttlSeconds": OFFICIAL_CAS_TGT_TTL_SECONDS,
            },
            "st": {
                "usernamePerMinute": 1500,
                "usernamePerSecond": 60,
                "ipPerMinute": 5000,
                "ipPerSecond": 500,
                "failedAttemptsPerWindow": 3,
                "failedAttemptsWindowSeconds": 10,
            },
        },
        "configured": {
            "tgt": tgt,
            "api": api,
            "authSessionMinutes": DEFAULT_AUTH_SESSION_MINUTES,
            "tgtCacheSeconds": DEFAULT_EPIAS_TGT_CACHE_SECONDS,
            "usernameFailedAttempts": {
                "maxAttempts": 3,
                "windowSeconds": 10,
                "blockSeconds": 60,
            },
            "singleFlight": {
                "enabled": True,
                "scope": "endpoint + payload",
            },
            "jsonCache": EPIAS_JSON_CACHE.snapshot(),
        },
        "runtime": monitor,
        "notes": [
            "TGT 8 saat geçerlidir; uygulama 7,5 saatte yeniden giriş ister.",
            "Aynı EPİAŞ e-postası 10 saniyede 3 hatalı girişte uygulama tarafında bekletilir.",
            "EPİAŞ 429 veya geçici blok 403 sinyali verirse uygulama otomatik soğuma moduna geçer.",
            "Aynı endpoint ve payload eş zamanlı gelirse tek EPİAŞ isteği paylaşılır.",
            "Aynı endpoint ve tarih aralığı daha önce alındıysa ham EPİAŞ JSON cevabı dosyadan okunur.",
            (
                "Zorla yenileme aynı EPİAŞ sorgusunu "
                f"{_force_refresh_min_seconds()} saniyeden sık gönderemez."
            ),
        ],
    }


@dataclass(frozen=True)
class AuthSession:
    username: str
    tgt: str
    expires_at: float


class AuthService:
    """Keep temporary EPİAŞ tickets in server memory for active sessions."""

    cookie_name = "baha_uretim_session"

    def __init__(
        self,
        *,
        ttl_minutes: float | None = None,
    ) -> None:
        self.ttl_seconds = int(
            60
            * (
                ttl_minutes
                if ttl_minutes is not None
                else float(
                    os.getenv(
                        "BAHA_URETIM_SESSION_MINUTES",
                        str(DEFAULT_AUTH_SESSION_MINUTES),
                    )
                )
            )
        )
        self.cookie_secure = os.getenv("BAHA_URETIM_COOKIE_SECURE", "auto").lower()
        self._sessions: dict[str, AuthSession] = {}
        self._lock = threading.Lock()

    def create_session(self, username: str, tgt: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            self._sessions[token] = AuthSession(
                username=username,
                tgt=tgt,
                expires_at=now + self.ttl_seconds,
            )
        return token

    def get_session(self, token: str | None) -> AuthSession | None:
        if not token:
            return None
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            return self._sessions.get(token)

    def get_username(self, token: str | None) -> str | None:
        session = self.get_session(token)
        return session.username if session else None

    def latest_session(self) -> AuthSession | None:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            if not self._sessions:
                return None
            return max(self._sessions.values(), key=lambda session: session.expires_at)

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def _purge_expired(self, now: float) -> None:
        expired = [
            token
            for token, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._sessions.pop(token, None)

    def cookie_header(self, token: str, *, secure_request: bool) -> str:
        secure = self.cookie_secure == "true" or (
            self.cookie_secure == "auto" and secure_request
        )
        parts = [
            f"{self.cookie_name}={token}",
            "Path=/",
            f"Max-Age={self.ttl_seconds}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self, *, secure_request: bool) -> str:
        secure = self.cookie_secure == "true" or (
            self.cookie_secure == "auto" and secure_request
        )
        parts = [
            f"{self.cookie_name}=",
            "Path=/",
            "Max-Age=0",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def epias_payload(self) -> dict[str, str]:
        start_at = datetime.combine(self.start, dt_time.min, TR_TZ)
        end_at = datetime.combine(self.end, dt_time.max.replace(microsecond=0), TR_TZ)
        return {
            "startDate": start_at.isoformat(timespec="seconds"),
            "endDate": end_at.isoformat(timespec="seconds"),
        }


class EpiasClient:
    """Small EPİAŞ Transparency Platform client with an in-memory TGT cache."""

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        tgt: str | None = None,
    ) -> None:
        self.username = username.strip()
        self.password = password
        self.cas_url = os.getenv(
            "EPIAS_CAS_URL", "https://giris.epias.com.tr/cas/v1/tickets"
        ).rstrip("/")
        self.api_base = os.getenv(
            "EPIAS_API_BASE", "https://seffaflik.epias.com.tr/electricity-service"
        ).rstrip("/")
        self.timeout = float(os.getenv("EPIAS_TIMEOUT_SECONDS", "25"))
        self._tgt = tgt
        self._tgt_expires_at = (
            time.monotonic() + DEFAULT_EPIAS_TGT_CACHE_SECONDS if tgt else 0.0
        )
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._tgt or (self.username and self.password))

    def _limiter_for_url(self, url: str) -> RollingRateLimiter | None:
        if url.startswith(self.cas_url):
            return EPIAS_TGT_RATE_LIMITER
        if url.startswith(self.api_base):
            return EPIAS_API_RATE_LIMITER
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname and parsed.hostname.endswith("epias.com.tr"):
            return EPIAS_API_RATE_LIMITER
        return None

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        limiter = self._limiter_for_url(url)
        if limiter is not None:
            limiter.wait()
            EPIAS_PROTECTION_MONITOR.record_http_request(url)
        request = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                if limiter is not None:
                    limiter.backoff(
                        _retry_after_seconds(exc.headers),
                        reason="EPİAŞ 429 throttling",
                    )
                exc.read()
                EPIAS_PROTECTION_MONITOR.record_http_error(
                    url=url, status_code=exc.code, detail="EPİAŞ 429 throttling"
                )
                raise EpiasError(
                    "EPİAŞ istek sınırına ulaşıldı. Kayıtlı dönemler önbellekten "
                    "gösterilmeye devam edecek; yeni veri için yaklaşık bir dakika "
                    "sonra tekrar deneyin.",
                    status_code=exc.code,
                ) from exc
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            banned = exc.code == HTTPStatus.FORBIDDEN and (
                "client is banned" in detail.casefold()
                or "blocked" in detail.casefold()
            )
            if banned:
                if limiter is not None:
                    limiter.backoff(
                        _retry_after_seconds(exc.headers) or 120.0,
                        reason="EPİAŞ 403 temporary block",
                    )
                EPIAS_PROTECTION_MONITOR.record_http_error(
                    url=url,
                    status_code=exc.code,
                    detail="EPİAŞ 403 temporary block",
                )
                raise EpiasError(
                    "EPÄ°AÅ geÃ§ici blok sinyali dÃ¶ndÃ¼. Uygulama yeni "
                    "EPÄ°AÅ isteklerini otomatik olarak bekletiyor.",
                    status_code=HTTPStatus.TOO_MANY_REQUESTS,
                ) from exc
            EPIAS_PROTECTION_MONITOR.record_http_error(
                url=url, status_code=exc.code, detail=detail
            )
            raise EpiasError(
                f"EPİAŞ servisi {exc.code} yanıtını verdi"
                + (f": {detail}" if detail else "."),
                status_code=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            raise EpiasError(f"EPİAŞ servisine ulaşılamadı: {reason}") from exc

    def get_tgt(self, force_refresh: bool = False) -> str:
        with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._tgt and now < self._tgt_expires_at:
                return self._tgt
            if not self.username or not self.password:
                raise EpiasError(
                    "EPİAŞ oturumunun süresi doldu. Lütfen yeniden giriş yapın.",
                    status_code=HTTPStatus.UNAUTHORIZED,
                )

            body = urllib.parse.urlencode(
                {"username": self.username, "password": self.password}
            ).encode("utf-8")
            status, raw = self._request(
                self.cas_url,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "text/plain",
                    "User-Agent": "Baha-Uretim-Epias-Dashboard/1.0",
                },
            )
            token = raw.decode("utf-8", errors="replace").strip().strip('"')
            if status not in (HTTPStatus.OK, HTTPStatus.CREATED) or not token.startswith(
                "TGT-"
            ):
                raise EpiasError("EPİAŞ oturum bileti alınamadı.")

            self._tgt = token
            # EPİAŞ TGT is valid for 8 hours; stop using it at 7.5 hours.
            self._tgt_expires_at = now + DEFAULT_EPIAS_TGT_CACHE_SECONDS
            return token

    def _post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        retry_auth: bool = True,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        def work() -> dict[str, Any]:
            cached = EPIAS_JSON_CACHE.get(
                self.api_base,
                endpoint,
                payload,
                force_refresh=force_refresh,
            )
            if cached is not None:
                return cached
            response = self._post_json_uncached(
                endpoint,
                payload,
                retry_auth=retry_auth,
            )
            EPIAS_JSON_CACHE.put(self.api_base, endpoint, payload, response)
            return response

        if retry_auth:
            return EPIAS_SINGLE_FLIGHT.run(
                _singleflight_key(endpoint, payload),
                work,
            )
        return work()

    def _post_json_uncached(
        self, endpoint: str, payload: dict[str, Any], *, retry_auth: bool = True
    ) -> dict[str, Any]:
        token = self.get_tgt()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status, raw = self._request(
                f"{self.api_base}{endpoint}",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "TGT": token,
                    "User-Agent": "Baha-Uretim-Epias-Dashboard/1.0",
                },
            )
        except EpiasError as exc:
            if (
                retry_auth
                and self.username
                and self.password
                and exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}
            ):
                self.get_tgt(force_refresh=True)
                return self._post_json_uncached(endpoint, payload, retry_auth=False)
            raise

        if status != HTTPStatus.OK:
            raise EpiasError(
                f"EPİAŞ servisi beklenmeyen {status} yanıtını verdi.",
                status_code=status,
            )
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpiasError("EPİAŞ servisi geçersiz JSON döndürdü.") from exc
        if not isinstance(decoded, dict):
            raise EpiasError("EPİAŞ servis yanıtı beklenen yapıda değil.")
        return decoded

    def _fetch_paginated(
        self, endpoint: str, date_range: DateRange
    ) -> list[dict[str, Any]]:
        page_number = 1
        page_size = 100
        all_items: list[dict[str, Any]] = []

        while page_number <= 50:
            payload: dict[str, Any] = {
                **date_range.epias_payload(),
                "page": {
                    "number": page_number,
                    "size": page_size,
                },
            }
            response = self._post_json(endpoint, payload)
            container = response
            for wrapper_name in ("body", "data"):
                wrapped = response.get(wrapper_name)
                if isinstance(wrapped, dict) and isinstance(wrapped.get("items"), list):
                    container = wrapped
                    break

            items = container.get("items") or []
            if not isinstance(items, list):
                raise EpiasError("EPİAŞ sayfalı yanıtındaki items alanı geçersiz.")
            all_items.extend(item for item in items if isinstance(item, dict))

            page = container.get("page") or {}
            total = _number(page.get("total")) if isinstance(page, dict) else None
            if not items or len(items) < page_size:
                break
            if total is not None and len(all_items) >= total:
                break
            page_number += 1
        else:
            raise EpiasError("EPİAŞ veri aralığı beklenenden fazla sayfa döndürdü.")

        return all_items

    def fetch_uevm(self, date_range: DateRange) -> list[dict[str, Any]]:
        return self._fetch_paginated(
            "/v1/generation/data/injection-quantity", date_range
        )

    def fetch_uecm(self, date_range: DateRange) -> list[dict[str, Any]]:
        return self._fetch_paginated("/v1/consumption/data/uecm", date_range)

def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso_hour(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TR_TZ)
    return parsed.astimezone(TR_TZ).replace(minute=0, second=0, microsecond=0)


def _uevm_timestamp(item: dict[str, Any], *, one_based_hours: bool = True) -> datetime | None:
    base = _iso_hour(item.get("date"))
    if base is None:
        return None
    hour = _number(item.get("hour"))
    if hour is None:
        return base
    hour_int = int(hour)
    # EPİAŞ tables generally number market hours 1–24.
    offset = hour_int - 1 if one_based_hours and 1 <= hour_int <= 24 else hour_int
    return base.replace(hour=0) + timedelta(hours=max(0, min(offset, 23)))


def _uecm_timestamp(item: dict[str, Any]) -> datetime | None:
    return _iso_hour(item.get("hour")) or _iso_hour(item.get("period"))


def _filter_items_by_date_range(
    items: Iterable[dict[str, Any]],
    date_range: DateRange,
    timestamp_getter: Any,
) -> list[dict[str, Any]]:
    """Discard records that an EPİAŞ endpoint returns outside the request."""
    filtered: list[dict[str, Any]] = []
    for item in items:
        timestamp = timestamp_getter(item)
        if timestamp and date_range.start <= timestamp.date() <= date_range.end:
            filtered.append(item)
    return filtered


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def normalize_epias_data(
    uevm_items: Iterable[dict[str, Any]],
    uecm_items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    uevm_items = list(uevm_items)
    raw_hours = [
        int(hour)
        for item in uevm_items
        if (hour := _number(item.get("hour"))) is not None
    ]
    one_based_hours = not any(hour == 0 for hour in raw_hours)

    uecm_by_hour: dict[str, float] = {}
    for item in uecm_items:
        timestamp = _uecm_timestamp(item)
        value = _number(item.get("swv"))
        if timestamp and value is not None:
            uecm_by_hour[timestamp.isoformat()] = value

    uevm_by_hour: dict[str, dict[str, Any]] = {}
    for item in uevm_items:
        timestamp = _uevm_timestamp(item, one_based_hours=one_based_hours)
        if timestamp is None:
            continue
        source_values = {
            source["id"]: _number(item.get(source["id"])) or 0.0
            for source in SOURCE_DEFINITIONS
        }
        groups = {
            group["id"]: sum(
                source_values[source["id"]]
                for source in SOURCE_DEFINITIONS
                if source["group"] == group["id"]
            )
            for group in GROUP_DEFINITIONS
        }
        # EPİAŞ bu alanı her saat için doğrudan yayımlar. Eksik olduğunda
        # kaynakları toplayıp yeni bir "toplam" üretmeyiz.
        total = _number(item.get("total"))
        iso_timestamp = timestamp.isoformat()
        uevm_by_hour[iso_timestamp] = {
            "uevm": total,
            "sources": source_values,
            "groups": groups,
        }

    empty_sources = {source["id"]: 0.0 for source in SOURCE_DEFINITIONS}
    empty_groups = {group["id"]: 0.0 for group in GROUP_DEFINITIONS}
    rows: list[dict[str, Any]] = []
    for iso_timestamp in sorted(set(uevm_by_hour) | set(uecm_by_hour)):
        uevm_row = uevm_by_hour.get(iso_timestamp)
        rows.append(
            {
                "timestamp": iso_timestamp,
                "uevm": uevm_row["uevm"] if uevm_row else None,
                "uecm": uecm_by_hour.get(iso_timestamp),
                "sources": uevm_row["sources"] if uevm_row else empty_sources.copy(),
                "groups": uevm_row["groups"] if uevm_row else empty_groups.copy(),
            }
        )

    return rows


def build_dashboard(
    rows: list[dict[str, Any]],
    date_range: DateRange,
    *,
    warning: str | None = None,
) -> dict[str, Any]:
    if not rows:
        raise EpiasError("Seçilen tarih aralığında veri bulunamadı.")

    uevm_rows = [row for row in rows if row["uevm"] is not None]
    uecm_rows = [row for row in rows if row["uecm"] is not None]
    comparable = [
        row
        for row in rows
        if row["uevm"] is not None and row["uecm"] is not None
    ]
    if not uevm_rows:
        raise EpiasError("Seçilen tarih aralığında geçerli UEVM toplamı bulunamadı.")

    uevm_total = sum(row["uevm"] for row in uevm_rows)
    uecm_total = sum(row["uecm"] for row in uecm_rows)
    comparable_uevm = sum(row["uevm"] for row in comparable)
    comparable_uecm = sum(row["uecm"] for row in comparable)
    difference = comparable_uevm - comparable_uecm if comparable else None
    deviation = (
        difference / comparable_uecm * 100
        if difference is not None and comparable_uecm
        else None
    )

    source_totals = {
        source["id"]: sum(row["sources"][source["id"]] for row in uevm_rows)
        for source in SOURCE_DEFINITIONS
    }
    group_totals = {
        group["id"]: sum(row["groups"][group["id"]] for row in uevm_rows)
        for group in GROUP_DEFINITIONS
    }

    def share(value: float) -> float | None:
        return _round(value / uevm_total * 100) if uevm_total else None

    source_payload = [
        {
            **source,
            "value": _round(source_totals[source["id"]]),
            "share": share(source_totals[source["id"]]),
        }
        for source in SOURCE_DEFINITIONS
    ]
    group_payload = [
        {
            **group,
            "value": _round(group_totals[group["id"]]),
            "share": share(group_totals[group["id"]]),
            "sources": [
                source["label"]
                for source in SOURCE_DEFINITIONS
                if source["group"] == group["id"]
            ],
        }
        for group in GROUP_DEFINITIONS
    ]

    hydro = source_totals["dam"] + source_totals["river"]
    thermal = group_totals["thermal"]
    source_cards = (
        ("sun", "Güneş", source_totals["sun"]),
        ("wind", "Rüzgâr", source_totals["wind"]),
        ("hydro", "Hidroelektrik", hydro),
        ("thermal", "Termik", thermal),
        ("natural_gas", "Doğal gaz", source_totals["naturalGas"]),
    )
    available_dates = [
        datetime.fromisoformat(row["timestamp"]).date() for row in uevm_rows
    ]
    earliest_available_date = min(available_dates)
    latest_available_date = max(available_dates)

    return {
        "meta": {
            "source": "epias",
            "warning": warning,
            "availableStartDate": earliest_available_date.isoformat(),
            "availableEndDate": latest_available_date.isoformat(),
            "latestAvailableDate": latest_available_date.isoformat(),
            "timezone": "Europe/Istanbul",
            "generatedAt": datetime.now(TR_TZ).isoformat(timespec="seconds"),
            "methodology": (
                "UEVM, EPİAŞ Uzlaştırma Esas Veriş Miktarı servisindeki total; "
                "UEÇM ise Uzlaştırmaya Esas Çekiş Miktarı servisindeki swv alanıdır. "
                "Fark yalnızca iki serviste de bulunan aynı saatler üzerinden hesaplanır."
            ),
        },
        "period": {
            "start": date_range.start.isoformat(),
            "end": date_range.end.isoformat(),
            "days": date_range.days,
            "hours": len(rows),
            "uevmHours": len(uevm_rows),
            "uecmHours": len(uecm_rows),
            "comparableHours": len(comparable),
        },
        "summary": {
            "uevmTotal": _round(uevm_total),
            "uecmTotal": _round(uecm_total) if uecm_rows else None,
            "comparableUevmTotal": _round(comparable_uevm) if comparable else None,
            "comparableUecmTotal": _round(comparable_uecm) if comparable else None,
            "difference": _round(difference),
            "deviationPct": _round(deviation),
            "hourlyAverage": _round(uevm_total / len(uevm_rows)),
        },
        "sourceCards": [
            {
                "id": card_id,
                "label": label,
                "value": _round(value),
                "share": share(value),
            }
            for card_id, label, value in source_cards
        ],
        "groups": group_payload,
        "sources": source_payload,
        "series": [
            {
                "timestamp": row["timestamp"],
                "uevm": _round(row["uevm"]),
                "uecm": _round(row["uecm"]),
                "renewable": (
                    _round(row["groups"]["renewable"])
                    if row["uevm"] is not None
                    else None
                ),
                "sun": _round(row["sources"]["sun"]) if row["uevm"] is not None else None,
                "wind": (
                    _round(row["sources"]["wind"]) if row["uevm"] is not None else None
                ),
                "hydro": (
                    _round(row["sources"]["dam"] + row["sources"]["river"])
                    if row["uevm"] is not None
                    else None
                ),
                "thermal": (
                    _round(row["groups"]["thermal"]) if row["uevm"] is not None else None
                ),
                "naturalGas": (
                    _round(row["groups"]["natural_gas"])
                    if row["uevm"] is not None
                    else None
                ),
            }
            for row in rows
        ],
    }


def _xlsx_column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx_cell(reference: str, value: Any, style: int = 0) -> str:
    style_attribute = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{style_attribute}/>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{reference}"{style_attribute}><v>{value}</v></c>'
    text = escape(str(value))
    return (
        f'<c r="{reference}" t="inlineStr"{style_attribute}>'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _xlsx_sheet(
    rows: list[list[tuple[Any, int]]],
    *,
    widths: list[float],
    freeze_row: int = 0,
    auto_filter: bool = False,
) -> str:
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            _xlsx_cell(
                f"{_xlsx_column_name(column_index)}{row_index}",
                value,
                style,
            )
            for column_index, (value, style) in enumerate(row, start=1)
        )
        row_xml.append(f'<row r="{row_index}">{cells}</row>')

    columns = "".join(
        (
            f'<col min="{index}" max="{index}" width="{width}" '
            'customWidth="1"/>'
        )
        for index, width in enumerate(widths, start=1)
    )
    last_column = _xlsx_column_name(max(len(widths), 1))
    last_row = max(len(rows), 1)
    pane = (
        (
            f'<pane ySplit="{freeze_row}" topLeftCell="A{freeze_row + 1}" '
            'activePane="bottomLeft" state="frozen"/>'
        )
        if freeze_row
        else ""
    )
    filter_xml = (
        f'<autoFilter ref="A1:{last_column}{last_row}"/>' if auto_filter else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        f"<sheetViews><sheetView workbookViewId=\"0\">{pane}</sheetView></sheetViews>"
        '<sheetFormatPr defaultRowHeight="15"/>'
        f"<cols>{columns}</cols>"
        f"<sheetData>{''.join(row_xml)}</sheetData>"
        f"{filter_xml}"
        "</worksheet>"
    )


def build_xlsx(dashboard: dict[str, Any]) -> bytes:
    """Build a dependency-free XLSX workbook from a dashboard response."""

    period = dashboard["period"]
    summary = dashboard["summary"]
    group_labels = {
        group["id"]: group["label"] for group in GROUP_DEFINITIONS
    }

    # Styles: 0 normal, 1 header, 2 number, 3 percentage, 4 title.
    summary_rows: list[list[tuple[Any, int]]] = [
        [("Baha Üretim — EPİAŞ UEVM / UEÇM Raporu", 4), (None, 0)],
        [("Başlangıç", 1), (period["start"], 0)],
        [("Bitiş", 1), (period["end"], 0)],
        [("Kapsanan saat", 1), (period["hours"], 0)],
        [("UEVM saati", 1), (period["uevmHours"], 0)],
        [("UEÇM saati", 1), (period["uecmHours"], 0)],
        [("Karşılaştırılabilir saat", 1), (period["comparableHours"], 0)],
        [("Gösterge", 1), ("Değer", 1)],
        [("Toplam UEVM (MWh)", 0), (summary["uevmTotal"], 2)],
        [("Toplam UEÇM (MWh)", 0), (summary["uecmTotal"], 2)],
        [("UEVM − UEÇM farkı (MWh)", 0), (summary["difference"], 2)],
        [
            ("Yüzdesel sapma", 0),
            (
                summary["deviationPct"] / 100
                if summary["deviationPct"] is not None
                else None,
                3,
            ),
        ],
        [("Saatlik ortalama UEVM (MWh)", 0), (summary["hourlyAverage"], 2)],
    ]

    source_rows: list[list[tuple[Any, int]]] = [
        [
            ("Ana grup", 1),
            ("Kaynak", 1),
            ("UEVM (MWh)", 1),
            ("Pay", 1),
        ]
    ]
    source_rows.extend(
        [
            (group_labels[source["group"]], 0),
            (source["label"], 0),
            (source["value"], 2),
            (
                source["share"] / 100 if source["share"] is not None else None,
                3,
            ),
        ]
        for source in dashboard["sources"]
    )

    hourly_rows: list[list[tuple[Any, int]]] = [
        [
            ("Tarih / saat", 1),
            ("UEVM (MWh)", 1),
            ("UEÇM (MWh)", 1),
            ("Yenilenebilir (MWh)", 1),
            ("Güneş (MWh)", 1),
            ("Rüzgâr (MWh)", 1),
            ("Hidroelektrik (MWh)", 1),
            ("Termik (MWh)", 1),
            ("Doğal gaz (MWh)", 1),
        ]
    ]
    hourly_rows.extend(
        [
            (row["timestamp"], 0),
            (row["uevm"], 2),
            (row["uecm"], 2),
            (row["renewable"], 2),
            (row["sun"], 2),
            (row["wind"], 2),
            (row["hydro"], 2),
            (row["thermal"], 2),
            (row["naturalGas"], 2),
        ]
        for row in dashboard["series"]
    )

    sheets = (
        _xlsx_sheet(summary_rows, widths=[39, 24]),
        _xlsx_sheet(
            source_rows,
            widths=[22, 19, 18, 13],
            freeze_row=1,
            auto_filter=True,
        ),
        _xlsx_sheet(
            hourly_rows,
            widths=[29, 18, 18, 23, 18, 18, 24, 18, 20],
            freeze_row=1,
            auto_filter=True,
        ),
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.styles+xml"/>'
        + "".join(
            (
                f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.worksheet+xml"/>'
            )
            for index in range(1, 4)
        )
        + "</Types>"
    )
    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<sheets>"
        '<sheet name="Özet" sheetId="1" r:id="rId1"/>'
        '<sheet name="Kaynaklar" sheetId="2" r:id="rId2"/>'
        '<sheet name="Saatlik Veri" sheetId="3" r:id="rId3"/>'
        "</sheets></workbook>"
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="2">'
        '<numFmt numFmtId="164" formatCode="#,##0.00"/>'
        '<numFmt numFmtId="165" formatCode="0.00%"/>'
        "</numFmts>"
        '<fonts count="3">'
        '<font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FF0B1D39"/><sz val="15"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2D70EE"/>'
        '<bgColor indexed="64"/></patternFill></fill>'
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="5">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1">'
        '<alignment horizontal="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_relationships)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr("xl/_rels/workbook.xml.rels", workbook_relationships)
        workbook_zip.writestr("xl/styles.xml", styles)
        for index, sheet in enumerate(sheets, start=1):
            workbook_zip.writestr(f"xl/worksheets/sheet{index}.xml", sheet)
    return output.getvalue()


def parse_date_range(query: dict[str, list[str]]) -> DateRange:
    yesterday = datetime.now(TR_TZ).date() - timedelta(days=1)
    default_start = yesterday - timedelta(days=6)
    raw_start = (query.get("start") or [default_start.isoformat()])[0]
    raw_end = (query.get("end") or [yesterday.isoformat()])[0]
    try:
        start = date.fromisoformat(raw_start)
        end = date.fromisoformat(raw_end)
    except ValueError as exc:
        raise ValueError("Tarihler YYYY-AA-GG biçiminde olmalıdır.") from exc
    result = DateRange(start=start, end=end)
    if start > end:
        raise ValueError("Başlangıç tarihi bitiş tarihinden sonra olamaz.")
    if result.days > MAX_RANGE_DAYS:
        raise ValueError(f"Tek sorguda en fazla {MAX_RANGE_DAYS} gün seçilebilir.")
    if end > datetime.now(TR_TZ).date():
        raise ValueError("Gelecek tarihli veri istenemez.")
    return result


class DashboardService:
    def __init__(self, *, cache_ttl_seconds: float | None = None) -> None:
        configured_ttl = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else float(os.getenv("BAHA_URETIM_DATA_CACHE_SECONDS", "300"))
        )
        self.cache_ttl_seconds = max(0.0, configured_ttl)
        self._cache: dict[DateRange, tuple[float, dict[str, Any]]] = {}
        self._cache_lock = threading.Lock()

    def _get_cached(self, date_range: DateRange) -> dict[str, Any] | None:
        if self.cache_ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(date_range)
            if cached is None:
                return None
            stored_at, payload = cached
            if now - stored_at >= self.cache_ttl_seconds:
                self._cache.pop(date_range, None)
                return None
            return payload

    def _store_cached(
        self, date_range: DateRange, payload: dict[str, Any]
    ) -> None:
        if self.cache_ttl_seconds <= 0:
            return
        now = time.monotonic()
        with self._cache_lock:
            expired = [
                key
                for key, (stored_at, _) in self._cache.items()
                if now - stored_at >= self.cache_ttl_seconds
            ]
            for key in expired:
                self._cache.pop(key, None)
            if len(self._cache) >= 64:
                oldest = min(self._cache, key=lambda key: self._cache[key][0])
                self._cache.pop(oldest, None)
            self._cache[date_range] = (now, payload)

    @staticmethod
    def _latest_uevm_date(items: list[dict[str, Any]]) -> date | None:
        timestamps = [
            timestamp
            for item in items
            if (timestamp := _uevm_timestamp(item)) is not None
        ]
        return max(timestamp.date() for timestamp in timestamps) if timestamps else None

    def dashboard(
        self, date_range: DateRange, *, client: EpiasClient
    ) -> dict[str, Any]:
        cached = self._get_cached(date_range)
        if cached is not None:
            return cached

        effective_range = date_range
        warning = None
        uevm = _filter_items_by_date_range(
            client.fetch_uevm(effective_range),
            effective_range,
            _uevm_timestamp,
        )
        latest_date = self._latest_uevm_date(uevm)

        if not uevm:
            probe_days = max(date_range.days, 7)
            searched_days = 0
            probe_end = date_range.start - timedelta(days=1)
            while searched_days < 180:
                current_probe_days = min(probe_days, 180 - searched_days)
                probe = DateRange(
                    start=probe_end - timedelta(days=current_probe_days - 1),
                    end=probe_end,
                )
                probe_items = _filter_items_by_date_range(
                    client.fetch_uevm(probe),
                    probe,
                    _uevm_timestamp,
                )
                latest_date = self._latest_uevm_date(probe_items)
                if latest_date is not None:
                    break
                searched_days += current_probe_days
                probe_end = probe.start - timedelta(days=1)

        if latest_date is None:
            raise EpiasError(
                "Seçilen tarih aralığında ve önceki 180 günde UEVM verisi bulunamadı."
            )

        if not uevm:
            effective_range = DateRange(
                start=latest_date - timedelta(days=date_range.days - 1),
                end=latest_date,
            )
            uevm = _filter_items_by_date_range(
                client.fetch_uevm(effective_range),
                effective_range,
                _uevm_timestamp,
            )
            warning = (
                "Seçilen döneme ait uzlaştırma verisi EPİAŞ'ta henüz "
                "yayımlanmadığı için en yakın kullanılabilir dönem gösteriliyor."
            )
        elif latest_date < date_range.end:
            warning = (
                "Seçilen dönemin henüz yayımlanmamış günleri atlandı; "
                "yalnızca EPİAŞ'ta bulunan veriler gösteriliyor."
            )

        if not uevm:
            raise EpiasError("En yakın kullanılabilir UEVM dönemi alınamadı.")

        uecm = _filter_items_by_date_range(
            client.fetch_uecm(effective_range),
            effective_range,
            _uecm_timestamp,
        )
        rows = normalize_epias_data(uevm, uecm)
        payload = build_dashboard(
            rows,
            effective_range,
            warning=warning,
        )
        self._store_cached(date_range, payload)
        return payload
