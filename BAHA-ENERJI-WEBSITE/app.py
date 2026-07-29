"""Baha Enerji'nin EPİAŞ panellerini tek web sitesinde birleştirir.

Bu uygulama mevcut modüllerin görsel dosyalarını değiştirmeden kullanır:

* /piyasa/  - PTF, SMF, YAL ve YAT
* /baraj/   - Baraj aktif doluluk
* /uretim/  - UEVM ve UEÇM
* /tuketim/ - Gerçek zamanlı tüketim

Kullanıcı bir kez giriş yapar. EPİAŞ parolası saklanmaz; geçici TGT yalnızca
sunucu belleğindeki ortak oturumda tutulur.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape

try:
    import numpy as np
except ImportError:  # The historical model remains available as a safe fallback.
    np = None


ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
URETIM_DIR = WORKSPACE / "UEVM-UEÇM"
PIYASA_DIR = WORKSPACE / "SMF-PTF-YAL-YAT" / "public"
BARAJ_DIR = WORKSPACE / "BARAJ AKTİF"
PORTAL_DIR = ROOT / "static"
BARAJ_ARCHIVE_XLSX = ROOT / "Aktif_Doluluk-Delta - Kopya.xlsx"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from baha_suite.config import asset_url  # noqa: E402
from baha_suite.html_tools import SUITE_FAVICON_LINKS  # noqa: E402
from baha_suite.html_tools import rewrite_paths as _rewrite_paths  # noqa: E402
from baha_suite.market_freshness import market_smf_is_published  # noqa: E402
from baha_suite.security import LoginRateLimiter  # noqa: E402
from baha_suite.shell import module_sidebar as _module_sidebar  # noqa: E402
from baha_suite.shell import suite_footer as _suite_footer  # noqa: E402
from baha_suite.shell import suite_navigation as _suite_navigation  # noqa: E402


def _load_uretim_module():
    """UEVM/UEÇM'nin sınanmış servis, oturum ve XLSX kodunu ortak çekirdeğe yükle."""

    module_path = URETIM_DIR / "main.py"
    spec = importlib.util.spec_from_file_location("baha_uretim_core", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Üretim modülü yüklenemedi: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


URETIM = _load_uretim_module()
AUTH = URETIM.AuthService()
URETIM_SERVICE = URETIM.DashboardService()

MARKET_CACHE: dict[str, dict[str, Any]] = {}
MARKET_CACHE_LOCK = threading.Lock()
NEXT_DAY_PTF_CACHE: dict[str, dict[str, Any]] = {}
NEXT_DAY_PTF_CACHE_LOCK = threading.Lock()
SYSTEM_DIRECTION_HISTORY_CACHE: dict[str, dict[str, Any]] = {}
SYSTEM_DIRECTION_HISTORY_CACHE_LOCK = threading.Lock()
SYSTEM_DIRECTION_FORECAST_CACHE: dict[str, dict[str, Any]] = {}
SYSTEM_DIRECTION_FORECAST_CACHE_LOCK = threading.Lock()
SYSTEM_DIRECTION_MODEL_CACHE: dict[str, dict[str, Any]] = {}
SYSTEM_DIRECTION_MODEL_CACHE_LOCK = threading.Lock()
SYSTEM_DIRECTION_WEATHER_CACHE: dict[str, dict[str, Any]] = {}
SYSTEM_DIRECTION_WEATHER_CACHE_LOCK = threading.Lock()
CONSUMPTION_CACHE: dict[str, dict[str, Any]] = {}
CONSUMPTION_CACHE_LOCK = threading.Lock()
CONSUMPTION_FORECAST_CACHE: dict[str, dict[str, Any]] = {}
CONSUMPTION_FORECAST_CACHE_LOCK = threading.Lock()
CONSUMPTION_FORECAST_LOCKED_ROWS: dict[str, dict[int, dict[str, Any]]] = {}
CONSUMPTION_FORECAST_LOCKED_ROWS_LOCK = threading.Lock()
BARAJ_ARCHIVE_CACHE: dict[str, Any] = {
    "mtime": None,
    "payload": {"byDate": {}, "availableDates": [], "recordCount": 0},
}
BARAJ_ARCHIVE_LOCK = threading.Lock()
ACTIVE_FULLNESS_CACHE: dict[str, dict[str, Any]] = {}
ACTIVE_FULLNESS_CACHE_LOCK = threading.Lock()
BARAJ_BASIN_HISTORY_CACHE: dict[str, Any] = {
    "archive_mtime": None,
    "client_key": None,
    "payload": None,
    "expires": 0.0,
}
BARAJ_BASIN_HISTORY_CACHE_LOCK = threading.Lock()
EXECUTIVE_REPORT_CACHE: dict[str, dict[str, Any]] = {}
EXECUTIVE_REPORT_CACHE_LOCK = threading.Lock()
STATIC_FILE_CACHE: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
STATIC_FILE_CACHE_LOCK = threading.Lock()
LOGIN_LIMITER = LoginRateLimiter(
    max_attempts=int(os.getenv("BAHA_LOGIN_MAX_ATTEMPTS", "5")),
    window_seconds=int(os.getenv("BAHA_LOGIN_WINDOW_SECONDS", "600")),
    block_seconds=int(os.getenv("BAHA_LOGIN_BLOCK_SECONDS", "300")),
)
LOGIN_USERNAME_LIMITER = LoginRateLimiter(
    max_attempts=int(os.getenv("BAHA_LOGIN_USERNAME_MAX_ATTEMPTS", "3")),
    window_seconds=int(os.getenv("BAHA_LOGIN_USERNAME_WINDOW_SECONDS", "10")),
    block_seconds=int(os.getenv("BAHA_LOGIN_USERNAME_BLOCK_SECONDS", "60")),
)
COMPRESSIBLE_CONTENT_TYPES = {
    "application/geo+json",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
}
GZIP_MIN_BYTES = 1024


class BackgroundRefreshService:
    """Warm dashboard caches with one active EPİAŞ session, without storing passwords."""

    def __init__(self) -> None:
        self.enabled = os.getenv("BAHA_BACKGROUND_REFRESH_ENABLED", "true").lower() not in {
            "0",
            "false",
            "no",
        }
        self.tick_seconds = max(
            5,
            min(60, int(os.getenv("BAHA_BACKGROUND_REFRESH_TICK_SECONDS", "15"))),
        )
        self.task_intervals = {
            "piyasa": max(60, int(os.getenv("BAHA_BG_MARKET_SECONDS", "120"))),
            "ertesiPtf": max(120, int(os.getenv("BAHA_BG_NEXT_PTF_SECONDS", "180"))),
            "tuketim": max(120, int(os.getenv("BAHA_BG_CONSUMPTION_SECONDS", "180"))),
            "baraj": max(180, int(os.getenv("BAHA_BG_BARAJ_SECONDS", "300"))),
            "uretim": max(900, int(os.getenv("BAHA_BG_URETIM_SECONDS", "1800"))),
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_task_run: dict[str, float] = {}
        self._state: dict[str, Any] = {
            "enabled": self.enabled,
            "running": False,
            "startedAt": "",
            "lastWakeAt": "",
            "nextWakeAt": "",
            "lastSessionUser": "",
            "lastStatus": "Beklemede",
            "wakeCount": 0,
            "tasks": {},
        }

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="baha-epias-background-refresh",
            daemon=True,
        )
        with self._lock:
            self._state["running"] = True
            self._state["startedAt"] = datetime.now(URETIM.TR_TZ).isoformat(
                timespec="seconds"
            )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            self._state["running"] = False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._state,
                "taskIntervals": dict(self.task_intervals),
                "tasks": {
                    name: dict(value)
                    for name, value in (self._state.get("tasks") or {}).items()
                },
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake()
            if self._stop.wait(self.tick_seconds):
                break

    def _wake(self) -> None:
        now = time.time()
        next_wake = datetime.now(URETIM.TR_TZ) + timedelta(seconds=self.tick_seconds)
        with self._lock:
            self._state["wakeCount"] += 1
            self._state["lastWakeAt"] = datetime.now(URETIM.TR_TZ).isoformat(
                timespec="seconds"
            )
            self._state["nextWakeAt"] = next_wake.isoformat(timespec="seconds")

        session = AUTH.latest_session()
        if session is None:
            with self._lock:
                self._state["lastSessionUser"] = ""
                self._state["lastStatus"] = "Aktif EPİAŞ oturumu yok; arka plan bekliyor."
            return

        client = URETIM.EpiasClient(tgt=session.tgt)
        today = datetime.now(URETIM.TR_TZ).date()
        today_text = today.isoformat()
        yesterday = today - timedelta(days=1)
        tasks: tuple[tuple[str, Any], ...] = (
            ("piyasa", lambda: _market_dashboard(today_text, client)),
            ("ertesiPtf", lambda: _next_day_ptf_dashboard(today_text, client)),
            ("tuketim", lambda: _consumption_dashboard(today_text, client)),
            ("baraj", lambda: _active_fullness(client)),
            (
                "uretim",
                lambda: URETIM_SERVICE.dashboard(
                    URETIM.DateRange(yesterday, yesterday),
                    client=client,
                ),
            ),
        )

        any_due = False
        for name, runner in tasks:
            interval = self.task_intervals[name]
            if now - self._last_task_run.get(name, 0.0) < interval:
                continue
            any_due = True
            self._run_task(name, session.username, runner)
            self._last_task_run[name] = time.time()
        if not any_due:
            with self._lock:
                self._state["lastSessionUser"] = session.username
                self._state["lastStatus"] = "Cache taze; EPİAŞ isteği gerekmedi."

    def _run_task(self, name: str, username: str, runner: Any) -> None:
        started = time.time()
        try:
            payload = runner()
            status = "cached" if isinstance(payload, dict) and payload.get("cached") else "refreshed"
            error = ""
        except Exception as exc:
            status = "error"
            error = str(exc)[:220]
        finished_at = datetime.now(URETIM.TR_TZ).isoformat(timespec="seconds")
        with self._lock:
            tasks = self._state.setdefault("tasks", {})
            previous = tasks.get(name, {})
            tasks[name] = {
                "status": status,
                "error": error,
                "lastRunAt": finished_at,
                "durationMs": round((time.time() - started) * 1000, 1),
                "successCount": int(previous.get("successCount", 0)) + (0 if status == "error" else 1),
                "errorCount": int(previous.get("errorCount", 0)) + (1 if status == "error" else 0),
            }
            self._state["lastSessionUser"] = username
            self._state["lastStatus"] = (
                f"{name} cache yenilendi." if status != "error" else f"{name} hata verdi."
            )


BACKGROUND_REFRESH = BackgroundRefreshService()


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    candidates = (
        payload.get("items"),
        (payload.get("body") or {}).get("items"),
        (payload.get("data") or {}).get("items"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _section(payload: Any, *names: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    containers = (payload, payload.get("body") or {}, payload.get("data") or {})
    for container in containers:
        if not isinstance(container, dict):
            continue
        for name in names:
            value = container.get(name)
            if isinstance(value, dict):
                return value
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
    return {}


def _number(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = row.get(name)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _hour_key(row: dict[str, Any], index: int) -> int:
    raw_hour = row.get("hour", row.get("saat"))
    if raw_hour is not None:
        text = str(raw_hour).strip()
        match = re.search(r"T(\d{2}):", text) or re.match(
            r"^(\d{1,2})(?::|\D|$)", text
        )
        if match:
            hour = int(match.group(1))
            return 23 if hour == 24 else hour
    source = str(
        row.get("date")
        or row.get("tarih")
        or row.get("effectiveDate")
        or row.get("time")
        or ""
    )
    match = re.search(r"T(\d{2}):", source)
    return int(match.group(1)) if match else index


def _coded_quantities(
    payload: dict[str, Any], coded_fields: tuple[str, ...]
) -> dict[int, float | None]:
    result: dict[int, float | None] = {}
    for index, row in enumerate(_items(payload)):
        values = [_number(row, field) for field in coded_fields]
        valid = [value for value in values if value is not None]
        result[_hour_key(row, index)] = sum(valid) if valid else None
    return result


def _market_smf_is_published(value: Any) -> bool:
    """EPİAŞ sometimes keeps unpublished trailing SMF hours as 0/null."""

    return market_smf_is_published(value)


def _epias_post_json(
    client: Any,
    endpoint: str,
    payload: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    if force_refresh:
        try:
            return client._post_json(endpoint, payload, force_refresh=True)
        except TypeError:
            pass
    return client._post_json(endpoint, payload)


def _market_dashboard(
    selected_date: str,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    try:
        selected_day = date.fromisoformat(selected_date)
    except ValueError as exc:
        raise ValueError("Geçerli bir tarih seçin.") from exc
    today = datetime.now(URETIM.TR_TZ).date()
    if selected_day > today:
        raise ValueError("Bugünden ileri bir tarih seçilemez.")

    now = time.time()
    if not force_refresh:
        with MARKET_CACHE_LOCK:
            cached = MARKET_CACHE.get(selected_date)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    body = {
        "startDate": f"{selected_date}T00:00:00+03:00",
        "endDate": f"{selected_date}T00:00:00+03:00",
        "page": {"number": 1, "size": 100},
    }
    warnings: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    price_endpoints = {
        "PTF": "/v1/markets/dam/data/mcp",
        "SMF": "/v1/markets/bpm/data/system-marginal-price",
    }
    for label, endpoint in price_endpoints.items():
        try:
            payloads[label] = _epias_post_json(
                client,
                endpoint,
                body,
                force_refresh=force_refresh,
            )
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            warnings.append(f"{label} verisi alınamadı.")

    quantities: dict[str, dict[int, float | None]] = {}
    quantity_totals: dict[str, float | None] = {}
    quantity_sources: dict[str, str] = {}
    quantity_definitions = {
        "YAL": (
            "/v1/markets/bpm/data/order-summary-up",
            (
                "upRegulationZeroCoded",
                "upRegulationOneCoded",
                "upRegulationTwoCoded",
            ),
            "upRegulation",
        ),
        "YAT": (
            "/v1/markets/bpm/data/order-summary-down",
            (
                "downRegulationZeroCoded",
                "downRegulationOneCoded",
                "downRegulationTwoCoded",
            ),
            "downRegulation",
        ),
    }
    for label, (endpoint, fields, prefix) in quantity_definitions.items():
        try:
            payload = _epias_post_json(
                client,
                endpoint,
                body,
                force_refresh=force_refresh,
            )
            values = _coded_quantities(payload, fields)
            quantities[label] = values
            stats = _section(payload, "statistics", "statistic")
            official_parts = [
                _number(stats, f"{prefix}{code}CodedTotal")
                for code in ("Zero", "One", "Two")
            ]
            valid_official = [
                value for value in official_parts if value is not None
            ]
            if valid_official:
                quantity_totals[label] = sum(valid_official)
                quantity_sources[label] = "EPİAŞ resmî toplamı"
            else:
                valid_values = [
                    value for value in values.values() if value is not None
                ]
                quantity_totals[label] = (
                    sum(valid_values) if valid_values else None
                )
                quantity_sources[label] = "Saatlik kodlu alanların toplamı"
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            quantities[label] = {}
            warnings.append(f"{label} verisi alınamadı.")

    direction_by_hour: dict[int, Any] = {}
    try:
        direction_payload = _epias_post_json(
            client,
            "/v1/markets/bpm/data/system-direction",
            body,
            force_refresh=force_refresh,
        )
        direction_by_hour = {
            _hour_key(row, index): row.get("systemDirection")
            for index, row in enumerate(_items(direction_payload))
        }
    except URETIM.EpiasError as exc:
        if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise
        warnings.append("Sistem yönü alınamadı.")

    ptf_items = _items(payloads.get("PTF", {}))
    ptf_stats = _section(payloads.get("PTF", {}), "statistic", "statistics")
    ptf_rows = {
        _hour_key(row, index): row
        for index, row in enumerate(ptf_items)
    }
    smf_rows = {
        _hour_key(row, index): row
        for index, row in enumerate(_items(payloads.get("SMF", {})))
    }
    rows: list[dict[str, Any]] = []
    for hour in sorted(set(ptf_rows) | set(smf_rows)):
        ptf_row = ptf_rows.get(hour, {})
        smf_row = smf_rows.get(hour, {})
        ptf_try = _number(ptf_row, "price")
        smf_try = _number(smf_row, "systemMarginalPrice")
        ptf_eur = _number(ptf_row, "priceEur")
        ptf_usd = _number(ptf_row, "priceUsd")
        rows.append(
            {
                "hour": hour,
                "time": f"{hour:02}:00",
                "ptf": ptf_try,
                "smf": smf_try,
                "ptfByCurrency": {
                    "TRY": ptf_try,
                    "EUR": ptf_eur,
                    "USD": ptf_usd,
                },
                "smfByCurrency": {
                    "TRY": smf_try,
                },
                "yal": quantities.get("YAL", {}).get(hour),
                "yat": quantities.get("YAT", {}).get(hour),
                "direction": direction_by_hour.get(hour),
            }
        )

    common_price_rows = [
        row
        for row in rows
        if row.get("ptf") is not None
        and _market_smf_is_published(row.get("smf"))
    ]
    common_ptf_average = (
        sum(float(row["ptf"]) for row in common_price_rows)
        / len(common_price_rows)
        if common_price_rows
        else None
    )
    common_smf_average = (
        sum(float(row["smf"]) for row in common_price_rows)
        / len(common_price_rows)
        if common_price_rows
        else None
    )

    def average(key: str) -> float | None:
        values = [
            row[key]
            for row in rows
            if row[key] is not None
            and (key != "smf" or _market_smf_is_published(row[key]))
        ]
        return sum(values) / len(values) if values else None

    def currency_average(price_key: str, currency: str) -> float | None:
        values = [
            row.get(price_key, {}).get(currency)
            for row in rows
            if row.get(price_key, {}).get(currency) is not None
        ]
        return sum(values) / len(values) if values else None

    smf_stats = _section(payloads.get("SMF", {}), "statistics", "statistic")
    epias_ptf = _number(ptf_stats, "priceAvg")
    epias_ptf_eur = _number(ptf_stats, "priceEurAvg")
    epias_ptf_usd = _number(ptf_stats, "priceUsdAvg")
    epias_smf = _number(smf_stats, "smpArithmeticalAverage")
    direct_ptf_currency_values = {
        "EUR": (
            epias_ptf_eur is not None
            or any(
                row["ptfByCurrency"].get("EUR") is not None
                for row in rows
            )
        ),
        "USD": (
            epias_ptf_usd is not None
            or any(
                row["ptfByCurrency"].get("USD") is not None
                for row in rows
            )
        ),
    }
    validation = {
        "ptf": {"field": "price", "items": len(ptf_rows)},
        "smf": {"field": "systemMarginalPrice", "items": len(smf_rows)},
        "yal": {
            "field": "0+1+2 kodlu YAL",
            "items": len(quantities.get("YAL", {})),
        },
        "yat": {
            "field": "0+1+2 kodlu YAT",
            "items": len(quantities.get("YAT", {})),
        },
        "direction": {
            "field": "systemDirection",
            "items": len(direction_by_hour),
        },
    }
    today = datetime.now(URETIM.TR_TZ).date().isoformat()
    smf_published_hours = sum(
        1 for row in rows if _market_smf_is_published(row.get("smf"))
    )
    smf_incomplete_today = selected_date == today and smf_published_hours < 24
    payload = {
        "date": selected_date,
        "rows": rows,
        "currencyInfo": {
            "default": "TRY",
            "available": [
                currency
                for currency in ("TRY", "EUR", "USD")
                if currency == "TRY"
                or direct_ptf_currency_values.get(currency, False)
            ],
            "appliesTo": "PTF",
            "mode": "epias-ptf-direct",
            "source": "EPİAŞ PTF price / priceEur / priceUsd",
        },
        "freshness": {
            "smfPublishedHours": smf_published_hours,
            "smfIncomplete": smf_incomplete_today,
            "clientCacheMs": 15_000 if smf_incomplete_today else 120_000,
            "nextRefreshMs": 45_000 if smf_incomplete_today else None,
        },
        "summary": {
            "ptfAverage": epias_ptf if epias_ptf is not None else average("ptf"),
            "smfAverage": epias_smf if epias_smf is not None else average("smf"),
            "ptfAverageByCurrency": {
                "TRY": (
                    epias_ptf if epias_ptf is not None else average("ptf")
                ),
                "EUR": (
                    epias_ptf_eur
                    if epias_ptf_eur is not None
                    else currency_average("ptfByCurrency", "EUR")
                ),
                "USD": (
                    epias_ptf_usd
                    if epias_ptf_usd is not None
                    else currency_average("ptfByCurrency", "USD")
                ),
            },
            "smfAverageByCurrency": {
                "TRY": (
                    epias_smf if epias_smf is not None else average("smf")
                ),
            },
            "ptfSmfCommonHours": len(common_price_rows),
            "ptfCommonAverage": common_ptf_average,
            "smfCommonAverage": common_smf_average,
            "smfPtfAverageDifference": (
                common_smf_average - common_ptf_average
                if common_smf_average is not None
                and common_ptf_average is not None
                else None
            ),
            "yalTotal": quantity_totals.get("YAL"),
            "yatTotal": (
                abs(quantity_totals["YAT"])
                if quantity_totals.get("YAT") is not None
                else None
            ),
            "ptfAverageSource": (
                "EPİAŞ statistic.priceAvg"
                if epias_ptf is not None
                else "Saatlik veriler"
            ),
            "smfAverageSource": (
                "EPİAŞ statistics.smpArithmeticalAverage"
                if epias_smf is not None
                else "Saatlik veriler"
            ),
            "yalTotalSource": quantity_sources.get("YAL"),
            "yatTotalSource": quantity_sources.get("YAT"),
        },
        "warnings": warnings,
        "validation": validation,
        "updatedAt": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "cached": False,
    }
    if rows and not warnings:
        ttl = 20 if smf_incomplete_today else 300 if selected_date == today else 21_600
        with MARKET_CACHE_LOCK:
            MARKET_CACHE[selected_date] = {
                "payload": payload,
                "expires": time.time() + ttl,
            }
    return payload


DIRECTION_CATEGORIES: tuple[str, ...] = ("deficit", "surplus", "balanced")
DIRECTION_LABELS = {
    "deficit": "Enerji Açığı",
    "surplus": "Enerji Fazlası",
    "balanced": "Dengede",
    "missing": "Veri yok",
}
DIRECTION_SOURCE_WEIGHTS = {
    "recent": 1.0,
    "weekly": 1.12,
    "monthly": 0.58,
    "calendar_yearly": 0.84,
    "seasonal_yearly": 0.54,
    "yearly": 0.34,
}
DIRECTION_SOURCE_LABELS = {
    "recent": "Son günler",
    "weekly": "Aynı hafta günü",
    "monthly": "Aylık benzerlik",
    "calendar_yearly": "Geçmiş yıllar aynı ay/hafta/gün",
    "seasonal_yearly": "Geçmiş yıllar mevsim çevresi",
    "yearly": "Yıllık referans",
}


def _system_direction_schedule(
    now_tr: datetime | None = None,
) -> dict[str, Any]:
    """Select today's or tomorrow's forecast at the 18:00 Turkey-time cutoff."""

    current = now_tr or datetime.now(URETIM.TR_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=URETIM.TR_TZ)
    else:
        current = current.astimezone(URETIM.TR_TZ)
    today = current.date()
    switch_at = datetime.combine(
        today,
        datetime.min.time(),
        tzinfo=URETIM.TR_TZ,
    ) + timedelta(hours=18)
    before_switch = current < switch_at
    target_day = today if before_switch else today + timedelta(days=1)
    return {
        "phase": "today" if before_switch else "tomorrow",
        "targetDate": target_day.isoformat(),
        "targetLabel": "Bugün" if before_switch else "Yarın",
        "validationDate": today.isoformat(),
        "switchHour": 18,
        "switchAt": switch_at.isoformat(),
        "currentTime": current.replace(microsecond=0).isoformat(),
        "headline": (
            "Bugünün sistem yönü tahmini"
            if before_switch
            else "Yarının sistem yönü tahmini"
        ),
        "detail": (
            "Saat 18:00'e kadar bugünün tahmini ve gerçekleşen saatlerin başarısı izlenir."
            if before_switch
            else "Saat 18:00 sonrası yarının tahmini gösterilir; bugünün sonucu hemen altında doğrulanır."
        ),
    }


SYSTEM_DIRECTION_WEATHER_POINTS = (
    ("Marmara", 41.01, 28.98, 0.22),
    ("İç Anadolu", 39.93, 32.86, 0.18),
    ("Ege", 38.42, 27.14, 0.14),
    ("Akdeniz", 36.90, 30.70, 0.12),
    ("Çukurova", 37.00, 35.32, 0.12),
    ("Karadeniz", 41.29, 36.33, 0.10),
    ("Güneydoğu", 37.91, 40.24, 0.07),
    ("Doğu Anadolu", 39.90, 41.27, 0.05),
)
SYSTEM_DIRECTION_WEATHER_FIELDS = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "cloud_cover",
    "wind_speed_10m",
    "wind_speed_80m",
    "wind_gusts_10m",
    "shortwave_radiation",
)


def _bounded(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _weather_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _verified_https_context() -> ssl.SSLContext:
    """Create a verified HTTPS context compatible with Python 3.13+ on Windows.

    Python 3.13 enabled OpenSSL's strict X.509 checks by default. Some otherwise
    trusted corporate/Windows certificate chains omit the Authority Key
    Identifier extension, which makes verified requests fail before hostname
    validation. Disabling only that strict extension check keeps certificate
    and hostname verification enabled.
    """

    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


def _system_direction_weather(
    target_day: date,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return a weighted, nationwide hourly weather profile for the target day."""

    cache_key = target_day.isoformat()
    now = time.time()
    if not force_refresh:
        with SYSTEM_DIRECTION_WEATHER_CACHE_LOCK:
            cached = SYSTEM_DIRECTION_WEATHER_CACHE.get(cache_key)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    latitudes = ",".join(str(point[1]) for point in SYSTEM_DIRECTION_WEATHER_POINTS)
    longitudes = ",".join(str(point[2]) for point in SYSTEM_DIRECTION_WEATHER_POINTS)
    query = urllib.parse.urlencode(
        {
            "latitude": latitudes,
            "longitude": longitudes,
            "hourly": ",".join(SYSTEM_DIRECTION_WEATHER_FIELDS),
            "timezone": "Europe/Istanbul",
            "start_date": target_day.isoformat(),
            "end_date": target_day.isoformat(),
        }
    )
    request = urllib.request.Request(
        f"https://api.open-meteo.com/v1/forecast?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "BahaEnerji-SystemDirection/1.0",
        },
    )
    with urllib.request.urlopen(
        request,
        timeout=8,
        context=_verified_https_context(),
    ) as response:
        raw_payload = json.loads(response.read().decode("utf-8"))
    locations = raw_payload if isinstance(raw_payload, list) else [raw_payload]

    accumulators: dict[int, dict[str, float]] = {
        hour: {field: 0.0 for field in SYSTEM_DIRECTION_WEATHER_FIELDS}
        for hour in range(24)
    }
    weights: dict[int, dict[str, float]] = {
        hour: {field: 0.0 for field in SYSTEM_DIRECTION_WEATHER_FIELDS}
        for hour in range(24)
    }
    point_count = 0
    for index, location in enumerate(locations):
        if not isinstance(location, dict):
            continue
        hourly = location.get("hourly")
        if not isinstance(hourly, dict):
            continue
        point_weight = (
            SYSTEM_DIRECTION_WEATHER_POINTS[index][3]
            if index < len(SYSTEM_DIRECTION_WEATHER_POINTS)
            else 1 / max(1, len(locations))
        )
        times = hourly.get("time") or []
        if not isinstance(times, list):
            continue
        used_point = False
        for row_index, timestamp in enumerate(times):
            timestamp_text = str(timestamp)
            if not timestamp_text.startswith(target_day.isoformat()):
                continue
            match = re.search(r"T(\d{2}):", timestamp_text)
            if not match:
                continue
            hour = int(match.group(1))
            if not 0 <= hour <= 23:
                continue
            for field in SYSTEM_DIRECTION_WEATHER_FIELDS:
                values = hourly.get(field)
                if not isinstance(values, list) or row_index >= len(values):
                    continue
                value = _weather_number(values[row_index])
                if value is None:
                    continue
                accumulators[hour][field] += value * point_weight
                weights[hour][field] += point_weight
                used_point = True
        if used_point:
            point_count += 1

    rows = []
    for hour in range(24):
        row: dict[str, Any] = {"hour": hour, "time": f"{hour:02}:00"}
        for field in SYSTEM_DIRECTION_WEATHER_FIELDS:
            total_weight = weights[hour][field]
            row[field] = (
                round(accumulators[hour][field] / total_weight, 2)
                if total_weight
                else None
            )
        rows.append(row)

    available_hours = sum(
        1 for row in rows if row.get("temperature_2m") is not None
    )
    result = {
        "date": target_day.isoformat(),
        "rows": rows,
        "availableHours": available_hours,
        "pointCount": point_count,
        "source": "Open-Meteo",
        "sourceDetail": "8 bölge ağırlıklı saatlik hava tahmini",
        "cached": False,
    }
    if available_hours:
        with SYSTEM_DIRECTION_WEATHER_CACHE_LOCK:
            SYSTEM_DIRECTION_WEATHER_CACHE[cache_key] = {
                "payload": result,
                "expires": time.time() + 1800,
            }
    return result


def _system_direction_kgup(
    target_day: date,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Read target-day KGÜP supply mix without making more than one EPİAŞ call."""

    target_text = target_day.isoformat()
    response = _epias_post_json(
        client,
        "/v1/generation/data/dpp",
        {
            "startDate": f"{target_text}T00:00:00+03:00",
            "endDate": f"{target_text}T00:00:00+03:00",
            "region": "TR1",
            "page": {"number": 1, "size": 100},
        },
        force_refresh=force_refresh,
    )
    by_hour: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(_items(response)):
        hour = _hour_key(item, index)
        if not 0 <= hour <= 23:
            continue
        wind = _number(item, "ruzgar", "wind")
        solar = _number(item, "gunes", "sun", "solar")
        hydro_values = [
            _number(item, "akarsu", "river"),
            _number(item, "barajli", "dammedHydro"),
        ]
        valid_hydro = [value for value in hydro_values if value is not None]
        hydro = sum(valid_hydro) if valid_hydro else None
        other_renewable_values = [
            _number(item, "biokutle", "biomass"),
            _number(item, "jeotermal", "geothermal"),
        ]
        valid_other_renewable = [
            value for value in other_renewable_values if value is not None
        ]
        other_renewable = (
            sum(valid_other_renewable)
            if valid_other_renewable
            else None
        )
        total = _number(item, "toplam", "total")
        consumption = _number(item, "tuketim", "consumption")
        renewable_parts = [
            value
            for value in (wind, solar, hydro, other_renewable)
            if value is not None
        ]
        renewable = sum(renewable_parts) if renewable_parts else None
        by_hour[hour] = {
            "hour": hour,
            "time": f"{hour:02}:00",
            "total": total,
            "consumption": consumption,
            "wind": wind,
            "solar": solar,
            "hydro": round(hydro, 2) if hydro is not None else None,
            "renewable": (
                round(renewable, 2)
                if renewable is not None
                else None
            ),
            "renewableShare": (
                round(renewable / total * 100, 2)
                if renewable is not None and total and total > 0
                else None
            ),
        }
    rows = [
        by_hour.get(hour, {"hour": hour, "time": f"{hour:02}:00"})
        for hour in range(24)
    ]
    return {
        "date": target_text,
        "rows": rows,
        "availableHours": sum(
            1 for row in rows if row.get("total") is not None
        ),
        "source": "EPİAŞ KGÜP",
    }


def _system_direction_ptf(
    target_day: date,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Read target-day PTF as a bounded market scarcity signal."""

    target_text = target_day.isoformat()
    response = _epias_post_json(
        client,
        "/v1/markets/dam/data/mcp",
        {
            "startDate": f"{target_text}T00:00:00+03:00",
            "endDate": f"{target_text}T00:00:00+03:00",
            "page": {"number": 1, "size": 100},
        },
        force_refresh=force_refresh,
    )
    by_hour: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(_items(response)):
        hour = _hour_key(item, index)
        price = _number(item, "price")
        if 0 <= hour <= 23 and price is not None:
            by_hour[hour] = {
                "hour": hour,
                "time": f"{hour:02}:00",
                "price": price,
            }
    rows = [
        by_hour.get(hour, {"hour": hour, "time": f"{hour:02}:00"})
        for hour in range(24)
    ]
    return {
        "date": target_text,
        "rows": rows,
        "availableHours": len(by_hour),
        "source": "EPİAŞ PTF",
    }


def _system_direction_context(
    target_day: date,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Collect optional forward-looking inputs while keeping graceful fallbacks."""

    warnings: list[str] = []
    weather: dict[str, Any] = {}
    kgup: dict[str, Any] = {}
    consumption: dict[str, Any] = {}
    market: dict[str, Any] = {}

    try:
        weather = _system_direction_weather(
            target_day,
            force_refresh=force_refresh,
        )
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        warnings.append("Hava tahmini alınamadı; tarihsel model kullanıldı.")

    try:
        kgup = _system_direction_kgup(
            target_day,
            client,
            force_refresh=force_refresh,
        )
    except Exception:
        warnings.append("EPİAŞ KGÜP alınamadı; üretim planı etkisi kullanılmadı.")

    try:
        consumption = _consumption_forecast(
            datetime.now(URETIM.TR_TZ).date().isoformat(),
            client,
            force_refresh=force_refresh,
            target_date=target_day.isoformat(),
        )
    except Exception:
        warnings.append("Tüketim profili alınamadı; talep etkisi kullanılmadı.")

    try:
        market = _system_direction_ptf(
            target_day,
            client,
            force_refresh=force_refresh,
        )
    except Exception:
        warnings.append("PTF profili alınamadı; fiyat sinyali kullanılmadı.")

    inputs = [
        {
            "key": "history",
            "label": "Sistem yönü geçmişi",
            "status": "ready",
            "detail": "Gün · hafta · ay · yıl",
        },
        {
            "key": "weather",
            "label": "Hava ve sıcaklık",
            "status": "ready" if weather.get("availableHours") else "fallback",
            "detail": (
                f"{weather.get('pointCount', 0)} bölge · rüzgâr · güneş · yağış"
                if weather.get("availableHours")
                else "Tarihsel yedek"
            ),
        },
        {
            "key": "kgup",
            "label": "EPİAŞ üretim planı",
            "status": "ready" if kgup.get("availableHours") else "fallback",
            "detail": (
                f"{kgup.get('availableHours', 0)} saat KGÜP"
                if kgup.get("availableHours")
                else "Tarihsel yedek"
            ),
        },
        {
            "key": "consumption",
            "label": "Tüketim profili",
            "status": (
                "ready"
                if (consumption.get("summary") or {}).get("forecastHours")
                else "fallback"
            ),
            "detail": (
                f"{(consumption.get('summary') or {}).get('forecastHours', 0)} saat"
                if (consumption.get("summary") or {}).get("forecastHours")
                else "Tarihsel yedek"
            ),
        },
        {
            "key": "market",
            "label": "Piyasa fiyat sinyali",
            "status": "ready" if market.get("availableHours") else "fallback",
            "detail": (
                f"{market.get('availableHours', 0)} saat EPİAŞ PTF"
                if market.get("availableHours")
                else "Tarihsel yedek"
            ),
        },
    ]
    return {
        "weather": weather,
        "kgup": kgup,
        "consumption": consumption,
        "market": market,
        "inputs": inputs,
        "warnings": warnings,
    }


def _apply_system_direction_context(
    hourly_scores: dict[int, dict[str, float]],
    context: dict[str, Any],
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, Any]]]:
    """Apply bounded demand, supply and weather pressure to historical scores."""

    adjusted = {
        hour: dict(hourly_scores[hour])
        for hour in range(24)
    }
    weather_by_hour = {
        int(row["hour"]): row
        for row in (context.get("weather") or {}).get("rows", [])
        if row.get("hour") is not None
    }
    kgup_rows = (context.get("kgup") or {}).get("rows", [])
    kgup_by_hour = {
        int(row["hour"]): row
        for row in kgup_rows
        if row.get("hour") is not None
    }
    consumption_rows = (context.get("consumption") or {}).get("rows", [])
    consumption_by_hour = {
        int(row["hour"]): row
        for row in consumption_rows
        if row.get("hour") is not None
    }
    market_rows = (context.get("market") or {}).get("rows", [])
    market_by_hour = {
        int(row["hour"]): row
        for row in market_rows
        if row.get("hour") is not None
    }

    demand_values = [
        float(row["forecast"])
        for row in consumption_rows
        if row.get("forecast") is not None
    ]
    demand_mean = (
        sum(demand_values) / len(demand_values)
        if demand_values
        else None
    )
    demand_variance = (
        sum((value - demand_mean) ** 2 for value in demand_values)
        / len(demand_values)
        if demand_values and demand_mean is not None
        else 0.0
    )
    demand_std = math.sqrt(demand_variance) or None

    renewable_shares = [
        float(row["renewableShare"])
        for row in kgup_rows
        if row.get("renewableShare") is not None
    ]
    renewable_mean = (
        sum(renewable_shares) / len(renewable_shares)
        if renewable_shares
        else None
    )
    renewable_variance = (
        sum((value - renewable_mean) ** 2 for value in renewable_shares)
        / len(renewable_shares)
        if renewable_shares and renewable_mean is not None
        else 0.0
    )
    renewable_std = math.sqrt(renewable_variance) or None

    price_values = [
        float(row["price"])
        for row in market_rows
        if row.get("price") is not None
    ]
    price_mean = (
        sum(price_values) / len(price_values)
        if price_values
        else None
    )
    price_variance = (
        sum((value - price_mean) ** 2 for value in price_values)
        / len(price_values)
        if price_values and price_mean is not None
        else 0.0
    )
    price_std = math.sqrt(price_variance) or None

    details: dict[int, dict[str, Any]] = {}
    for hour in range(24):
        pressure = 0.0  # positive: açık, negative: fazla
        signals: list[dict[str, Any]] = []
        weather = weather_by_hour.get(hour, {})
        plan = kgup_by_hour.get(hour, {})
        demand = consumption_by_hour.get(hour, {})
        market = market_by_hour.get(hour, {})

        demand_value = _weather_number(demand.get("forecast"))
        if (
            demand_value is not None
            and demand_mean is not None
            and demand_std is not None
        ):
            demand_z = _bounded((demand_value - demand_mean) / demand_std, -2, 2)
            impact = demand_z * 0.12
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {
                        "label": "Yüksek tüketim" if impact > 0 else "Düşük tüketim",
                        "impact": round(impact, 3),
                    }
                )

        renewable_share = _weather_number(plan.get("renewableShare"))
        if (
            renewable_share is not None
            and renewable_mean is not None
            and renewable_std is not None
        ):
            renewable_z = _bounded(
                (renewable_share - renewable_mean) / renewable_std,
                -2,
                2,
            )
            impact = -renewable_z * 0.13
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {
                        "label": (
                            "Yüksek yenilenebilir planı"
                            if impact < 0
                            else "Düşük yenilenebilir planı"
                        ),
                        "impact": round(impact, 3),
                    }
                )

        planned_total = _weather_number(plan.get("total"))
        planned_consumption = _weather_number(plan.get("consumption"))
        if (
            planned_total is not None
            and planned_consumption is not None
            and planned_consumption > 1000
        ):
            margin = (planned_total - planned_consumption) / planned_consumption
            impact = -_bounded(margin * 3.0, -0.22, 0.22)
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {
                        "label": "KGÜP arz marjı" if impact < 0 else "KGÜP açık baskısı",
                        "impact": round(impact, 3),
                    }
                )

        ptf = _weather_number(market.get("price"))
        if ptf is not None and price_mean is not None and price_std is not None:
            price_z = _bounded((ptf - price_mean) / price_std, -2, 2)
            impact = price_z * 0.07
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {
                        "label": "Yüksek fiyat sinyali" if impact > 0 else "Düşük fiyat sinyali",
                        "impact": round(impact, 3),
                    }
                )

        temperature = _weather_number(weather.get("temperature_2m"))
        apparent_temperature = _weather_number(weather.get("apparent_temperature"))
        humidity = _weather_number(weather.get("relative_humidity_2m"))
        wind_speed = _weather_number(weather.get("wind_speed_10m"))
        wind_speed_80m = _weather_number(weather.get("wind_speed_80m"))
        gust_speed = _weather_number(weather.get("wind_gusts_10m"))
        cloud_cover = _weather_number(weather.get("cloud_cover"))
        precipitation = _weather_number(weather.get("precipitation"))
        radiation = _weather_number(weather.get("shortwave_radiation"))

        thermal_temperature = (
            apparent_temperature
            if apparent_temperature is not None
            else temperature
        )
        if thermal_temperature is not None:
            cooling = max(0.0, thermal_temperature - 25.0)
            heating = max(0.0, 10.0 - thermal_temperature)
            humidity_load = (
                max(0.0, (humidity or 50.0) - 55.0) * 0.0012
                if cooling > 0
                else 0.0
            )
            impact = _bounded(
                cooling * 0.012 + heating * 0.010 + humidity_load,
                0.0,
                0.18,
            )
            pressure += impact
            if impact >= 0.035:
                signals.append(
                    {
                        "label": "Sıcaklık kaynaklı talep",
                        "impact": round(impact, 3),
                    }
                )

        turbine_wind = wind_speed_80m if wind_speed_80m is not None else wind_speed
        if turbine_wind is not None:
            effective_wind = max(
                turbine_wind,
                (gust_speed or turbine_wind) * 0.45,
            )
            wind_signal = _bounded((effective_wind - 12.0) / 18.0, -1, 1)
            impact = -wind_signal * 0.10
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {
                        "label": "Kuvvetli rüzgâr" if impact < 0 else "Zayıf rüzgâr",
                        "impact": round(impact, 3),
                    }
                )

        if radiation is not None and radiation > 20:
            solar_signal = _bounded(radiation / 750.0, 0, 1)
            cloud_penalty = _bounded((cloud_cover or 0) / 100.0, 0, 1)
            impact = -(solar_signal * (1 - 0.45 * cloud_penalty)) * 0.11
            pressure += impact
            if abs(impact) >= 0.035:
                signals.append(
                    {"label": "Güneş üretim potansiyeli", "impact": round(impact, 3)}
                )

        if precipitation is not None and precipitation >= 0.4 and 7 <= hour <= 18:
            impact = min(0.05, precipitation * 0.012)
            pressure += impact
            signals.append({"label": "Yağış ve bulut baskısı", "impact": round(impact, 3)})

        pressure = _bounded(pressure, -0.55, 0.55)
        adjusted[hour]["deficit"] *= math.exp(pressure)
        adjusted[hour]["surplus"] *= math.exp(-pressure)
        adjusted[hour]["balanced"] *= math.exp(-abs(pressure) * 0.30)
        details[hour] = {
            "balancePressure": round(pressure, 3),
            "impact": (
                "Açık yönünde"
                if pressure >= 0.08
                else "Fazla yönünde"
                if pressure <= -0.08
                else "Nötr"
            ),
            "signals": signals,
            "weather": {
                "temperature": temperature,
                "apparentTemperature": apparent_temperature,
                "humidity": humidity,
                "windSpeed": turbine_wind,
                "windSpeed10m": wind_speed,
                "windSpeed80m": wind_speed_80m,
                "windGust": gust_speed,
                "cloudCover": cloud_cover,
                "precipitation": precipitation,
                "radiation": radiation,
            },
            "plan": {
                "total": planned_total,
                "wind": plan.get("wind"),
                "solar": plan.get("solar"),
                "hydro": plan.get("hydro"),
                "renewableShare": renewable_share,
            },
            "consumptionForecast": demand_value,
            "ptf": ptf,
        }
    return adjusted, details


def _direction_category(value: Any) -> str:
    text = str(value or "").casefold()
    if "aç" in text or "deficit" in text or "aşağı" in text:
        return "deficit"
    if "fazla" in text or "surplus" in text or "yukarı" in text:
        return "surplus"
    if "denge" in text or "balanced" in text:
        return "balanced"
    return "missing"


def _direction_label(category: str) -> str:
    return DIRECTION_LABELS.get(category, DIRECTION_LABELS["missing"])


def _row_date_key(row: dict[str, Any], fallback: date | None = None) -> date | None:
    source = str(
        row.get("date")
        or row.get("deliveryDate")
        or row.get("effectiveDate")
        or row.get("time")
        or row.get("hour")
        or ""
    )
    match = re.search(r"(\d{4}-\d{2}-\d{2})", source)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return fallback
    return fallback


def _month_end(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _shift_month(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, _month_end(year, month)))


def _same_month_weekday_in_year(target_day: date, target_year: int) -> date:
    """Return the same weekday occurrence inside the same month for another year.

    Example: if the target is the 2nd Tuesday of July, this returns the 2nd
    Tuesday of July in ``target_year``. If that year does not have the same
    5th occurrence, the last matching weekday in that month is used.
    """

    occurrence = ((target_day.day - 1) // 7) + 1
    first_day = date(target_year, target_day.month, 1)
    first_matching_weekday = first_day + timedelta(
        days=(target_day.weekday() - first_day.weekday()) % 7
    )
    candidate = first_matching_weekday + timedelta(days=7 * (occurrence - 1))
    if candidate.month != target_day.month:
        candidate -= timedelta(days=7)
    return candidate


def _system_direction_range(
    start_day: date,
    end_day: date,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    if end_day < start_day:
        raise ValueError("Başlangıç tarihi bitiş tarihinden sonra olamaz.")
    today = datetime.now(URETIM.TR_TZ).date()
    if end_day > today:
        raise ValueError("Sistem yönü geçmiş verisi bugünden ileri olamaz.")

    cache_key = f"{start_day.isoformat()}:{end_day.isoformat()}"
    now = time.time()
    if not force_refresh:
        with SYSTEM_DIRECTION_HISTORY_CACHE_LOCK:
            cached = SYSTEM_DIRECTION_HISTORY_CACHE.get(cache_key)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    body = {
        "startDate": f"{start_day.isoformat()}T00:00:00+03:00",
        "endDate": f"{end_day.isoformat()}T00:00:00+03:00",
        "page": {"number": 1, "size": 2500},
    }
    payload = _epias_post_json(
        client,
        "/v1/markets/bpm/data/system-direction",
        body,
        force_refresh=force_refresh,
    )
    by_date: dict[str, dict[int, dict[str, Any]]] = {}
    for index, row in enumerate(_items(payload)):
        guessed_day = start_day + timedelta(days=index // 24)
        row_day = _row_date_key(row, guessed_day)
        if row_day is None or row_day < start_day or row_day > end_day:
            continue
        hour = _hour_key(row, index % 24)
        raw_direction = row.get("systemDirection")
        category = _direction_category(raw_direction)
        by_date.setdefault(row_day.isoformat(), {})[hour] = {
            "hour": hour,
            "time": f"{hour:02}:00",
            "direction": raw_direction,
            "category": category,
            "label": _direction_label(category),
        }

    normalized = {
        key: [hours[hour] for hour in sorted(hours)]
        for key, hours in sorted(by_date.items())
    }
    result = {
        "startDate": start_day.isoformat(),
        "endDate": end_day.isoformat(),
        "byDate": normalized,
        "recordCount": sum(len(rows) for rows in normalized.values()),
        "cached": False,
        "updatedAt": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
    }
    ttl = 300 if end_day == today else 21_600
    with SYSTEM_DIRECTION_HISTORY_CACHE_LOCK:
        SYSTEM_DIRECTION_HISTORY_CACHE[cache_key] = {
            "payload": result,
            "expires": time.time() + ttl,
        }
    return result


def _system_direction_sample_specs(
    target_day: date,
    cutoff_day: date,
) -> dict[date, set[str]]:
    specs: dict[date, set[str]] = {}

    def add(day: date, source: str) -> None:
        if day < cutoff_day - timedelta(days=1460) or day > cutoff_day:
            return
        specs.setdefault(day, set()).add(source)

    # Son dört hafta kısa dönem rejim değişimlerini yakalar.
    for offset in range(1, 29):
        add(cutoff_day - timedelta(days=offset - 1), "recent")

    # Aynı hafta günü, gün içi sistem yönü davranışında en güçlü örneklerden biridir.
    for week in range(1, 13):
        add(target_day - timedelta(days=7 * week), "weekly")

    # Aylık ve yıllık örnekler mevsimselliği modele taşır.
    for month in range(1, 13):
        add(_shift_month(target_day, -month), "monthly")

    for year in range(1, 4):
        calendar_match = _same_month_weekday_in_year(
            target_day,
            target_day.year - year,
        )
        add(calendar_match, "calendar_yearly")
        add(calendar_match - timedelta(days=7), "seasonal_yearly")
        add(calendar_match + timedelta(days=7), "seasonal_yearly")
        try:
            add(target_day.replace(year=target_day.year - year), "yearly")
        except ValueError:
            add(date(target_day.year - year, 2, 28), "yearly")

    return specs


def _system_direction_sample_weight(
    target_day: date,
    sample_day: date,
    sources: set[str],
) -> float:
    days_ago = max(1, (target_day - sample_day).days)
    source_weight = sum(DIRECTION_SOURCE_WEIGHTS.get(source, 0.0) for source in sources)
    recency = max(0.30, 1 / (1 + days_ago / 56))
    if sample_day.weekday() == target_day.weekday():
        weekday_factor = 1.24
    elif (sample_day.weekday() >= 5) == (target_day.weekday() >= 5):
        weekday_factor = 1.03
    else:
        weekday_factor = 0.68
    month_factor = 1.12 if sample_day.month == target_day.month else 1.0
    return round(
        min(2.8, source_weight * recency * weekday_factor * month_factor),
        4,
    )


def _system_direction_rows_by_hour(
    rows: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    return {
        int(row["hour"]): row
        for row in rows
        if (
            row.get("hour") is not None
            and row.get("category") in DIRECTION_CATEGORIES
            and 0 <= int(row["hour"]) <= 23
        )
    }


def _summarize_system_direction_quality(
    records: list[dict[str, Any]],
    target_day: date,
) -> dict[str, Any]:
    """Summarize leakage-safe backtest results by window, class and hour."""

    def summarize(
        selected: list[dict[str, Any]],
    ) -> dict[str, Any]:
        compared = len(selected)
        correct = sum(1 for row in selected if row.get("correct"))
        days = {
            row.get("date")
            for row in selected
            if row.get("date")
        }
        return {
            "dayCount": len(days),
            "comparedHours": compared,
            "correctHours": correct,
            "accuracy": (
                round(correct / compared * 100, 1)
                if compared
                else None
            ),
        }

    last_7_floor = target_day - timedelta(days=7)
    last_30_floor = target_day - timedelta(days=30)
    last_7 = [
        row
        for row in records
        if last_7_floor <= row["sampleDay"] < target_day
    ]
    last_30 = [
        row
        for row in records
        if last_30_floor <= row["sampleDay"] < target_day
    ]
    categories = {}
    for category in DIRECTION_CATEGORIES:
        category_rows = [
            row for row in last_30 if row.get("actual") == category
        ]
        categories[category] = {
            "label": _direction_label(category),
            **summarize(category_rows),
        }

    hours = []
    for hour in range(24):
        hour_rows = [
            row for row in last_30 if row.get("hour") == hour
        ]
        hours.append({"hour": hour, **summarize(hour_rows)})

    confidence_rows = [
        row
        for row in last_30
        if row.get("confidence") is not None
    ]
    average_confidence = (
        sum(float(row["confidence"]) for row in confidence_rows)
        / len(confidence_rows)
        if confidence_rows
        else None
    )
    last_30_summary = summarize(last_30)
    accuracy = last_30_summary.get("accuracy")
    return {
        "windows": {
            "last7": summarize(last_7),
            "last30": last_30_summary,
            "available": summarize(records),
        },
        "categories": categories,
        "hours": hours,
        "averageConfidence": (
            round(average_confidence, 1)
            if average_confidence is not None
            else None
        ),
        "confidenceGap": (
            round(abs(average_confidence - accuracy), 1)
            if average_confidence is not None and accuracy is not None
            else None
        ),
    }


def _system_direction_backtest_calibration(
    target_day: date,
    history_by_date: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Backtest recent days and learn bounded hour/transition corrections.

    This uses the same 84-day system-direction response already fetched for the
    forecast. It therefore improves the model without making another EPİAŞ
    request and never uses the target day's actual direction.
    """

    dated_history: list[tuple[date, dict[int, dict[str, Any]]]] = []
    for day_text, rows in history_by_date.items():
        try:
            sample_day = date.fromisoformat(day_text)
        except (TypeError, ValueError):
            continue
        if sample_day >= target_day:
            continue
        rows_by_hour = _system_direction_rows_by_hour(rows)
        if rows_by_hour:
            dated_history.append((sample_day, rows_by_hour))
    dated_history.sort(key=lambda item: item[0])

    confusion: dict[int, dict[str, dict[str, float]]] = {
        hour: {
            predicted: {actual: 0.0 for actual in DIRECTION_CATEGORIES}
            for predicted in DIRECTION_CATEGORIES
        }
        for hour in range(24)
    }
    transition_holds: dict[int, dict[str, float]] = {
        hour: {category: 0.0 for category in DIRECTION_CATEGORIES}
        for hour in range(24)
    }
    tested_days = 0
    tested_hours = 0
    correct_hours = 0
    test_records: list[dict[str, Any]] = []

    validation_candidates = dated_history[-30:]
    for validation_day, actual_by_hour in validation_candidates:
        prior_days = [
            (sample_day, rows_by_hour)
            for sample_day, rows_by_hour in dated_history
            if (
                validation_day - timedelta(days=35)
                <= sample_day
                < validation_day
            )
        ]
        if len(prior_days) < 7:
            continue

        predicted_by_hour: dict[int, str] = {}
        confidence_by_hour: dict[int, float] = {}
        probabilities_by_hour: dict[int, dict[str, float]] = {}
        for hour in range(24):
            scores = {category: 0.0 for category in DIRECTION_CATEGORIES}
            support = 0
            for sample_day, sample_by_hour in prior_days:
                sample_row = sample_by_hour.get(hour)
                if not sample_row:
                    continue
                sample_category = sample_row.get("category")
                if sample_category not in DIRECTION_CATEGORIES:
                    continue
                days_ago = max(1, (validation_day - sample_day).days)
                recency = max(0.28, 1 / (1 + days_ago / 14))
                if sample_day.weekday() == validation_day.weekday():
                    weekday_factor = 1.32
                elif (
                    sample_day.weekday() >= 5
                ) == (
                    validation_day.weekday() >= 5
                ):
                    weekday_factor = 1.04
                else:
                    weekday_factor = 0.72
                weight = recency * weekday_factor
                scores[sample_category] += weight
                support += 1
                for neighbor in (hour - 1, hour + 1):
                    neighbor_row = sample_by_hour.get(neighbor)
                    neighbor_category = (
                        neighbor_row.get("category")
                        if neighbor_row
                        else None
                    )
                    if neighbor_category in DIRECTION_CATEGORIES:
                        scores[neighbor_category] += weight * 0.08
            if support >= 7 and sum(scores.values()) > 0:
                score_total = sum(scores.values())
                predicted = max(scores, key=scores.get)
                predicted_by_hour[hour] = predicted
                probabilities_by_hour[hour] = {
                    category: scores[category] / score_total
                    for category in DIRECTION_CATEGORIES
                }
                confidence_by_hour[hour] = round(
                    scores[predicted] / score_total * 100,
                    2,
                )

        day_compared = 0
        validation_weight = max(
            0.48,
            1 / (1 + max(1, (target_day - validation_day).days) / 28),
        )
        for hour, actual_row in actual_by_hour.items():
            predicted_category = predicted_by_hour.get(hour)
            actual_category = actual_row.get("category")
            if (
                predicted_category not in DIRECTION_CATEGORIES
                or actual_category not in DIRECTION_CATEGORIES
            ):
                continue
            confusion[hour][predicted_category][actual_category] += (
                validation_weight
            )
            tested_hours += 1
            day_compared += 1
            if predicted_category == actual_category:
                correct_hours += 1
            test_records.append(
                {
                    "sampleDay": validation_day,
                    "date": validation_day.isoformat(),
                    "hour": hour,
                    "predicted": predicted_category,
                    "previousPredicted": predicted_by_hour.get(hour - 1),
                    "actual": actual_category,
                    "probabilities": probabilities_by_hour.get(hour, {}),
                    "confidence": confidence_by_hour.get(hour),
                    "correct": predicted_category == actual_category,
                }
            )

            if hour <= 0:
                continue
            previous_prediction = predicted_by_hour.get(hour - 1)
            previous_actual_row = actual_by_hour.get(hour - 1)
            previous_actual = (
                previous_actual_row.get("category")
                if previous_actual_row
                else None
            )
            if (
                previous_prediction in DIRECTION_CATEGORIES
                and previous_actual in DIRECTION_CATEGORIES
                and predicted_category != previous_prediction
                and previous_prediction == previous_actual == actual_category
            ):
                transition_holds[hour][actual_category] += validation_weight
        if day_compared:
            tested_days += 1

    return {
        "testedDays": tested_days,
        "testedHours": tested_hours,
        "correctHours": correct_hours,
        "accuracy": (
            round(correct_hours / tested_hours * 100, 1)
            if tested_hours
            else None
        ),
        "qualityMetrics": _summarize_system_direction_quality(
            test_records,
            target_day,
        ),
        "records": test_records,
        "confusion": confusion,
        "transitionHolds": transition_holds,
    }


def _apply_system_direction_calibration(
    hourly_scores: dict[int, dict[str, float]],
    calibration: dict[str, Any],
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, Any]]]:
    """Blend recent backtest errors into scores without letting them dominate."""

    adjusted = {
        hour: dict(hourly_scores[hour])
        for hour in range(24)
    }
    original_predictions = {
        hour: (
            max(hourly_scores[hour], key=hourly_scores[hour].get)
            if sum(hourly_scores[hour].values()) > 0
            else "missing"
        )
        for hour in range(24)
    }
    confusion = calibration.get("confusion") or {}
    transition_holds = calibration.get("transitionHolds") or {}
    details: dict[int, dict[str, Any]] = {}

    for hour in range(24):
        scores = adjusted[hour]
        score_total = sum(scores.values())
        predicted = original_predictions[hour]
        if score_total <= 0 or predicted not in DIRECTION_CATEGORIES:
            details[hour] = {"applied": False, "evidence": 0}
            continue

        hour_confusion = confusion.get(hour) or confusion.get(str(hour)) or {}
        predicted_results = hour_confusion.get(predicted) or {}
        evidence = sum(
            float(predicted_results.get(category, 0.0))
            for category in DIRECTION_CATEGORIES
        )
        correct_evidence = float(predicted_results.get(predicted, 0.0))
        historic_accuracy = (
            correct_evidence / evidence
            if evidence > 0
            else 1.0
        )
        applied = False
        blend = 0.0

        if evidence >= 3.0 and historic_accuracy < 0.72:
            evidence_factor = min(1.0, evidence / 8.0)
            error_rate = 1.0 - historic_accuracy
            blend = min(
                0.26,
                0.06 + 0.22 * error_rate * evidence_factor,
            )
            prior_strength = {
                category: 1.6 if category == predicted else 0.7
                for category in DIRECTION_CATEGORIES
            }
            prior_total = sum(prior_strength.values())
            current_probabilities = {
                category: scores[category] / score_total
                for category in DIRECTION_CATEGORIES
            }
            learned_probabilities = {
                category: (
                    float(predicted_results.get(category, 0.0))
                    + prior_strength[category]
                )
                / (evidence + prior_total)
                for category in DIRECTION_CATEGORIES
            }
            for category in DIRECTION_CATEGORIES:
                blended_probability = (
                    current_probabilities[category] * (1 - blend)
                    + learned_probabilities[category] * blend
                )
                scores[category] = blended_probability * score_total
            applied = True

        transition_blend = 0.0
        previous_prediction = original_predictions.get(hour - 1)
        if (
            hour > 0
            and predicted != previous_prediction
            and previous_prediction in DIRECTION_CATEGORIES
        ):
            hour_holds = (
                transition_holds.get(hour)
                or transition_holds.get(str(hour))
                or {}
            )
            hold_evidence = float(hour_holds.get(previous_prediction, 0.0))
            all_hold_evidence = sum(
                float(hour_holds.get(category, 0.0))
                for category in DIRECTION_CATEGORIES
            )
            if hold_evidence >= 2.0 and all_hold_evidence > 0:
                transition_blend = min(
                    0.12,
                    0.12
                    * min(1.0, hold_evidence / 5.0)
                    * (hold_evidence / all_hold_evidence),
                )
                current_total = sum(scores.values())
                for category in DIRECTION_CATEGORIES:
                    current_probability = scores[category] / current_total
                    target_probability = (
                        1.0 if category == previous_prediction else 0.0
                    )
                    scores[category] = (
                        current_probability * (1 - transition_blend)
                        + target_probability * transition_blend
                    ) * current_total
                applied = True

        calibrated_prediction = max(scores, key=scores.get)
        details[hour] = {
            "applied": applied,
            "evidence": round(evidence, 1),
            "historicalAccuracy": round(historic_accuracy * 100, 1),
            "blend": round(blend, 3),
            "transitionBlend": round(transition_blend, 3),
            "before": predicted,
            "after": calibrated_prediction,
            "changedDirection": calibrated_prediction != predicted,
        }
    return adjusted, details


def _direction_history_index(
    history_by_date: dict[str, list[dict[str, Any]]],
) -> dict[date, dict[int, dict[str, Any]]]:
    index: dict[date, dict[int, dict[str, Any]]] = {}
    for day_text, rows in history_by_date.items():
        try:
            sample_day = date.fromisoformat(day_text)
        except (TypeError, ValueError):
            continue
        rows_by_hour = _system_direction_rows_by_hour(rows)
        if rows_by_hour:
            index[sample_day] = rows_by_hour
    return index


def _direction_one_hot(category: str | None) -> list[float]:
    return [
        1.0 if category == expected else 0.0
        for expected in DIRECTION_CATEGORIES
    ]


def _direction_regime_length(
    rows_by_hour: dict[int, dict[str, Any]],
    hour: int,
) -> int:
    category = (rows_by_hour.get(hour) or {}).get("category")
    if category not in DIRECTION_CATEGORIES:
        return 0
    length = 0
    for check_hour in range(hour, -1, -1):
        if (rows_by_hour.get(check_hour) or {}).get("category") != category:
            break
        length += 1
    return length


def _direction_day_features(
    sample_day: date,
    hour: int,
    history_index: dict[date, dict[int, dict[str, Any]]],
    *,
    observed_by_hour: dict[int, dict[str, Any]] | None = None,
    observed_cutoff: int | None = None,
) -> list[float]:
    """Build leakage-safe calendar, lag and regime features."""

    hour_angle = 2 * math.pi * hour / 24
    weekday_angle = 2 * math.pi * sample_day.weekday() / 7
    year_angle = 2 * math.pi * sample_day.timetuple().tm_yday / 366
    features = [
        math.sin(hour_angle),
        math.cos(hour_angle),
        math.sin(weekday_angle),
        math.cos(weekday_angle),
        math.sin(year_angle),
        math.cos(year_angle),
        1.0 if sample_day.weekday() >= 5 else 0.0,
    ]

    previous_day_rows = history_index.get(sample_day - timedelta(days=1), {})
    previous_week_rows = history_index.get(sample_day - timedelta(days=7), {})
    features.extend(
        _direction_one_hot(
            (previous_day_rows.get(hour) or {}).get("category")
        )
    )
    features.extend(
        _direction_one_hot(
            (previous_day_rows.get((hour - 1) % 24) or {}).get("category")
        )
    )
    features.extend(
        _direction_one_hot(
            (previous_week_rows.get(hour) or {}).get("category")
        )
    )

    prior_days = sorted(
        day
        for day in history_index
        if sample_day - timedelta(days=28) <= day < sample_day
    )
    for window in (7, 28):
        window_days = prior_days[-window:]
        counts = {category: 0 for category in DIRECTION_CATEGORIES}
        total = 0
        for prior_day in window_days:
            category = (
                history_index[prior_day].get(hour) or {}
            ).get("category")
            if category in DIRECTION_CATEGORIES:
                counts[category] += 1
                total += 1
        features.extend(
            counts[category] / total if total else 0.0
            for category in DIRECTION_CATEGORIES
        )

    previous_regime = _direction_regime_length(previous_day_rows, hour)
    previous_transitions = sum(
        1
        for check_hour in range(1, 24)
        if (
            (previous_day_rows.get(check_hour) or {}).get("category")
            in DIRECTION_CATEGORIES
            and (previous_day_rows.get(check_hour - 1) or {}).get("category")
            in DIRECTION_CATEGORIES
            and (previous_day_rows.get(check_hour) or {}).get("category")
            != (previous_day_rows.get(check_hour - 1) or {}).get("category")
        )
    )
    features.extend(
        [
            min(previous_regime, 12) / 12,
            min(previous_transitions, 12) / 12,
        ]
    )

    if observed_by_hour is not None and observed_cutoff is not None:
        observed_category = (
            observed_by_hour.get(observed_cutoff) or {}
        ).get("category")
        observed_regime = _direction_regime_length(
            observed_by_hour,
            observed_cutoff,
        )
        horizon = max(1, hour - observed_cutoff)
        cutoff_angle = 2 * math.pi * observed_cutoff / 24
        features.extend(_direction_one_hot(observed_category))
        features.extend(
            [
                min(observed_regime, 12) / 12,
                min(horizon, 12) / 12,
                math.sin(cutoff_angle),
                math.cos(cutoff_angle),
            ]
        )
    return [float(value) for value in features]


def _softmax_direction_model(
    features: list[list[float]],
    labels: list[str],
    sample_importance: list[float] | None = None,
) -> dict[str, Any]:
    """Train a small regularized multinomial model with chronological holdout."""

    if sample_importance is not None and len(sample_importance) != len(features):
        raise ValueError("Örnek ağırlığı sayısı eğitim örneği sayısıyla eşleşmiyor.")
    if np is None or len(features) < 120 or len(set(labels)) < 2:
        return {
            "available": False,
            "reason": "Makine öğrenmesi için yeterli eğitim verisi yok.",
            "sampleCount": len(features),
        }
    matrix = np.asarray(features, dtype=np.float64)
    targets = np.asarray(
        [DIRECTION_CATEGORIES.index(label) for label in labels],
        dtype=np.int64,
    )
    split = max(96, min(len(features) - 24, int(len(features) * 0.82)))
    train_x, validation_x = matrix[:split], matrix[split:]
    train_y, validation_y = targets[:split], targets[split:]
    importance = (
        np.asarray(sample_importance, dtype=np.float64)
        if sample_importance is not None
        else np.ones(len(features), dtype=np.float64)
    )
    train_importance = np.clip(importance[:split], 0.25, 4.0)
    mean = train_x.mean(axis=0)
    scale = train_x.std(axis=0)
    scale[scale < 1e-7] = 1.0
    train_scaled = (train_x - mean) / scale
    validation_scaled = (validation_x - mean) / scale
    train_design = np.column_stack(
        [np.ones(len(train_scaled)), train_scaled]
    )
    validation_design = np.column_stack(
        [np.ones(len(validation_scaled)), validation_scaled]
    )
    weights = np.zeros(
        (train_design.shape[1], len(DIRECTION_CATEGORIES)),
        dtype=np.float64,
    )
    one_hot = np.eye(len(DIRECTION_CATEGORIES), dtype=np.float64)[train_y]
    class_counts = np.bincount(
        train_y,
        minlength=len(DIRECTION_CATEGORIES),
    ).astype(np.float64)
    class_weights = np.sqrt(
        len(train_y) / np.maximum(1.0, len(DIRECTION_CATEGORIES) * class_counts)
    )
    class_weights = np.clip(class_weights, 0.65, 2.4)
    sample_weights = class_weights[train_y] * train_importance
    sample_weight_total = sample_weights.sum()

    for epoch in range(180):
        logits = train_design @ weights
        logits -= logits.max(axis=1, keepdims=True)
        probabilities = np.exp(logits)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        error = (probabilities - one_hot) * sample_weights[:, None]
        gradient = (train_design.T @ error) / sample_weight_total
        gradient[1:] += 0.0025 * weights[1:]
        learning_rate = 0.14 / math.sqrt(1 + epoch / 32)
        weights -= learning_rate * gradient

    validation_logits = validation_design @ weights
    validation_predictions = validation_logits.argmax(axis=1)
    validation_accuracy = (
        float((validation_predictions == validation_y).mean() * 100)
        if len(validation_y)
        else None
    )
    return {
        "available": True,
        "weights": weights,
        "mean": mean,
        "scale": scale,
        "sampleCount": len(features),
        "trainingSamples": int(split),
        "validationSamples": int(len(features) - split),
        "validationAccuracy": (
            round(validation_accuracy, 1)
            if validation_accuracy is not None
            else None
        ),
    }


def _softmax_direction_probabilities(
    model: dict[str, Any],
    features: list[float],
) -> dict[str, float]:
    if np is None or not model.get("available"):
        return {category: 0.0 for category in DIRECTION_CATEGORIES}
    vector = np.asarray(features, dtype=np.float64)
    scaled = (vector - model["mean"]) / model["scale"]
    design = np.concatenate(([1.0], scaled))
    logits = design @ model["weights"]
    logits -= logits.max()
    probabilities = np.exp(logits)
    probabilities /= probabilities.sum()
    return {
        category: float(probabilities[index])
        for index, category in enumerate(DIRECTION_CATEGORIES)
    }


def _direction_error_learning_features(
    sample_day: date,
    hour: int,
    probabilities: dict[str, Any],
    predicted: str | None,
    previous_predicted: str | None,
) -> list[float]:
    """Describe a model decision so a second model can learn its error pattern."""

    normalized = {
        category: max(0.0, float(probabilities.get(category, 0.0) or 0.0))
        for category in DIRECTION_CATEGORIES
    }
    probability_total = sum(normalized.values())
    if probability_total <= 0 and predicted in DIRECTION_CATEGORIES:
        normalized = {
            category: 0.76 if category == predicted else 0.12
            for category in DIRECTION_CATEGORIES
        }
        probability_total = 1.0
    if probability_total > 0:
        normalized = {
            category: value / probability_total
            for category, value in normalized.items()
        }
    if predicted not in DIRECTION_CATEGORIES and probability_total > 0:
        predicted = max(normalized, key=normalized.get)

    ranked = sorted(normalized.values(), reverse=True)
    confidence = ranked[0] if ranked else 0.0
    margin = ranked[0] - ranked[1] if len(ranked) >= 2 else confidence
    hour_angle = 2 * math.pi * hour / 24
    weekday_angle = 2 * math.pi * sample_day.weekday() / 7
    return [
        math.sin(hour_angle),
        math.cos(hour_angle),
        math.sin(weekday_angle),
        math.cos(weekday_angle),
        1.0 if sample_day.weekday() >= 5 else 0.0,
        *(normalized[category] for category in DIRECTION_CATEGORIES),
        *_direction_one_hot(predicted),
        *_direction_one_hot(previous_predicted),
        confidence,
        margin,
        1.0
        if (
            predicted in DIRECTION_CATEGORIES
            and previous_predicted in DIRECTION_CATEGORIES
            and predicted != previous_predicted
        )
        else 0.0,
    ]


def _system_direction_error_learning_model(
    target_day: date,
    calibration: dict[str, Any],
) -> dict[str, Any]:
    """Train a leakage-safe meta-model from recent walk-forward mistakes.

    The base prediction for each historical day was produced only from days
    before that day. Wrong decisions receive more training weight, allowing
    repeated hour and transition errors to influence future forecasts without
    exposing the target day's actual system direction to the model.
    """

    training_rows: list[tuple[date, dict[str, Any]]] = []
    for row in calibration.get("records") or []:
        sample_day = row.get("sampleDay")
        if isinstance(sample_day, str):
            try:
                sample_day = date.fromisoformat(sample_day)
            except ValueError:
                continue
        if not isinstance(sample_day, date) or sample_day >= target_day:
            continue
        predicted = row.get("predicted")
        actual = row.get("actual")
        if (
            predicted not in DIRECTION_CATEGORIES
            or actual not in DIRECTION_CATEGORIES
        ):
            continue
        training_rows.append((sample_day, row))
    training_rows.sort(key=lambda item: (item[0], int(item[1].get("hour", 0))))

    mistake_count = sum(
        1 for _, row in training_rows if row.get("predicted") != row.get("actual")
    )
    if len(training_rows) < 120 or mistake_count < 8:
        return {
            "available": False,
            "reason": "Sapma öğrenimi için yeterli doğrulanmış hata yok.",
            "sampleCount": len(training_rows),
            "mistakeCount": mistake_count,
            "correctCount": len(training_rows) - mistake_count,
        }

    features: list[list[float]] = []
    labels: list[str] = []
    importance: list[float] = []
    for sample_day, row in training_rows:
        predicted = str(row.get("predicted"))
        actual = str(row.get("actual"))
        features.append(
            _direction_error_learning_features(
                sample_day,
                int(row.get("hour", 0)),
                row.get("probabilities") or {},
                predicted,
                row.get("previousPredicted"),
            )
        )
        labels.append(actual)
        # The model still sees correct decisions, but recurrent mistakes carry
        # more gradient so they are not drowned out by the majority class.
        importance.append(2.1 if predicted != actual else 0.9)

    model = _softmax_direction_model(
        features,
        labels,
        sample_importance=importance,
    )
    model.update(
        {
            "sampleCount": len(training_rows),
            "mistakeCount": mistake_count,
            "correctCount": len(training_rows) - mistake_count,
            "trainedThrough": training_rows[-1][0].isoformat(),
            "mistakeWeight": 2.1,
        }
    )
    return model


def _direction_ml_training_data(
    target_day: date,
    history_index: dict[date, dict[int, dict[str, Any]]],
    mode: str,
) -> tuple[list[list[float]], list[str]]:
    features: list[list[float]] = []
    labels: list[str] = []
    training_days = sorted(
        day for day in history_index if day < target_day
    )[-63:]
    for sample_day in training_days:
        actual_by_hour = history_index[sample_day]
        prior_count = sum(1 for day in history_index if day < sample_day)
        if prior_count < 14:
            continue
        for hour in range(24):
            category = (actual_by_hour.get(hour) or {}).get("category")
            if category not in DIRECTION_CATEGORIES:
                continue
            if mode == "day_ahead":
                features.append(
                    _direction_day_features(
                        sample_day,
                        hour,
                        history_index,
                    )
                )
                labels.append(category)
                continue
            for horizon in (1, 2, 3, 4, 6):
                cutoff = hour - horizon
                if cutoff < 0:
                    continue
                cutoff_category = (
                    actual_by_hour.get(cutoff) or {}
                ).get("category")
                if cutoff_category not in DIRECTION_CATEGORIES:
                    continue
                features.append(
                    _direction_day_features(
                        sample_day,
                        hour,
                        history_index,
                        observed_by_hour=actual_by_hour,
                        observed_cutoff=cutoff,
                    )
                )
                labels.append(category)
    return features, labels


def _direction_model_history_signature(
    target_day: date,
    history_index: dict[date, dict[int, dict[str, Any]]],
    mode: str,
) -> str:
    """Create a stable, cheap signature for the rows used to train a model."""

    digest = hashlib.sha256()
    digest.update(f"v1:{mode}:{target_day.isoformat()}".encode("ascii"))
    for sample_day in sorted(day for day in history_index if day < target_day):
        digest.update(sample_day.isoformat().encode("ascii"))
        rows_by_hour = history_index[sample_day]
        for hour in range(24):
            category = (rows_by_hour.get(hour) or {}).get("category")
            digest.update(
                f"|{hour}:{category if category in DIRECTION_CATEGORIES else '-'}"
                .encode("ascii")
            )
    return digest.hexdigest()


def _cached_direction_ml_model(
    target_day: date,
    history_index: dict[date, dict[int, dict[str, Any]]],
    mode: str,
) -> dict[str, Any]:
    """Reuse trained NumPy models until their underlying history changes."""

    signature = _direction_model_history_signature(
        target_day,
        history_index,
        mode,
    )
    cache_key = f"v1:{mode}:{target_day.isoformat()}:{signature}"
    now = time.time()
    with SYSTEM_DIRECTION_MODEL_CACHE_LOCK:
        cached = SYSTEM_DIRECTION_MODEL_CACHE.get(cache_key)
        if cached and cached["expires"] > now:
            return {**cached["model"], "trainingCacheHit": True}

    features, labels = _direction_ml_training_data(
        target_day,
        history_index,
        mode,
    )
    model = _softmax_direction_model(features, labels)
    model["trainingCacheHit"] = False

    with SYSTEM_DIRECTION_MODEL_CACHE_LOCK:
        expired_keys = [
            key
            for key, item in SYSTEM_DIRECTION_MODEL_CACHE.items()
            if item.get("expires", 0) <= now
        ]
        for key in expired_keys:
            SYSTEM_DIRECTION_MODEL_CACHE.pop(key, None)
        if len(SYSTEM_DIRECTION_MODEL_CACHE) >= 8:
            oldest_key = min(
                SYSTEM_DIRECTION_MODEL_CACHE,
                key=lambda key: SYSTEM_DIRECTION_MODEL_CACHE[key].get(
                    "created",
                    0,
                ),
            )
            SYSTEM_DIRECTION_MODEL_CACHE.pop(oldest_key, None)
        SYSTEM_DIRECTION_MODEL_CACHE[cache_key] = {
            "model": model,
            "created": now,
            "expires": now + 21600,
        }
    return model


def _regime_duration_bin(duration: int) -> int:
    if duration <= 1:
        return 1
    if duration <= 3:
        return 3
    if duration <= 6:
        return 6
    return 12


def _system_direction_transition_model(
    target_day: date,
    history_index: dict[date, dict[int, dict[str, Any]]],
) -> dict[str, Any]:
    local_counts: dict[tuple[int, str, int], dict[str, float]] = {}
    global_counts: dict[tuple[str, int], dict[str, float]] = {}
    tested = 0
    correct = 0
    days = sorted(day for day in history_index if day < target_day)[-63:]

    def add_day(sample_day: date) -> None:
        rows = history_index[sample_day]
        previous_day_rows = history_index.get(
            sample_day - timedelta(days=1),
            {},
        )
        for hour in range(24):
            previous_rows = rows if hour else previous_day_rows
            previous_hour = hour - 1 if hour else 23
            previous = (
                previous_rows.get(previous_hour) or {}
            ).get("category")
            actual = (rows.get(hour) or {}).get("category")
            if (
                previous not in DIRECTION_CATEGORIES
                or actual not in DIRECTION_CATEGORIES
            ):
                continue
            duration_bin = _regime_duration_bin(
                _direction_regime_length(
                    previous_rows,
                    previous_hour,
                )
            )
            local_key = (hour, previous, duration_bin)
            global_key = (previous, duration_bin)
            local = local_counts.setdefault(
                local_key,
                {category: 0.0 for category in DIRECTION_CATEGORIES},
            )
            overall = global_counts.setdefault(
                global_key,
                {category: 0.0 for category in DIRECTION_CATEGORIES},
            )
            local[actual] += 1.0
            overall[actual] += 1.0

    # Keep the validation tail outside the fit so ensemble weights are honest.
    validation_days = days[-14:] if len(days) >= 28 else []
    fit_days = days[:-14] if validation_days else days
    for sample_day in fit_days:
        add_day(sample_day)

    for sample_day in validation_days:
        rows = history_index[sample_day]
        previous_day_rows = history_index.get(
            sample_day - timedelta(days=1),
            {},
        )
        for hour in range(24):
            previous_rows = rows if hour else previous_day_rows
            previous_hour = hour - 1 if hour else 23
            previous = (
                previous_rows.get(previous_hour) or {}
            ).get("category")
            actual = (rows.get(hour) or {}).get("category")
            if (
                previous not in DIRECTION_CATEGORIES
                or actual not in DIRECTION_CATEGORIES
            ):
                continue
            probabilities = _transition_probabilities(
                {
                    "localCounts": local_counts,
                    "globalCounts": global_counts,
                },
                hour,
                previous,
                _direction_regime_length(
                    previous_rows,
                    previous_hour,
                ),
            )
            tested += 1
            if max(probabilities, key=probabilities.get) == actual:
                correct += 1

    # Refit with every published historical day after measuring quality.
    for sample_day in validation_days:
        add_day(sample_day)
    return {
        "localCounts": local_counts,
        "globalCounts": global_counts,
        "testedHours": tested,
        "accuracy": round(correct / tested * 100, 1) if tested else None,
    }


def _transition_probabilities(
    model: dict[str, Any],
    hour: int,
    previous_category: str,
    duration: int,
) -> dict[str, float]:
    duration_bin = _regime_duration_bin(duration)
    local = (model.get("localCounts") or {}).get(
        (hour, previous_category, duration_bin),
        {},
    )
    overall = (model.get("globalCounts") or {}).get(
        (previous_category, duration_bin),
        {},
    )

    def distribution(counts: dict[str, float]) -> dict[str, float]:
        total = sum(float(counts.get(category, 0.0)) for category in DIRECTION_CATEGORIES)
        return {
            category: (
                float(counts.get(category, 0.0))
                + (1.8 if category == previous_category else 0.6)
            )
            / (total + 3.0)
            for category in DIRECTION_CATEGORIES
        }

    local_distribution = distribution(local)
    global_distribution = distribution(overall)
    local_evidence = sum(float(value) for value in local.values())
    local_weight = min(0.62, local_evidence / 16)
    return {
        category: (
            local_distribution[category] * local_weight
            + global_distribution[category] * (1 - local_weight)
        )
        for category in DIRECTION_CATEGORIES
    }


def _system_direction_transition_series(
    target_day: date,
    history_index: dict[date, dict[int, dict[str, Any]]],
    transition_model: dict[str, Any],
    *,
    observed_by_hour: dict[int, dict[str, Any]] | None = None,
) -> dict[int, dict[str, float]]:
    previous_day_rows = history_index.get(target_day - timedelta(days=1), {})
    previous_category = (
        previous_day_rows.get(23) or {}
    ).get("category")
    if previous_category not in DIRECTION_CATEGORIES:
        previous_category = "balanced"
    duration = max(1, _direction_regime_length(previous_day_rows, 23))
    observed_hours = sorted(
        hour
        for hour, row in (observed_by_hour or {}).items()
        if row.get("category") in DIRECTION_CATEGORIES
    )
    observed_cutoff = max(observed_hours) if observed_hours else -1
    result: dict[int, dict[str, float]] = {}
    for hour in range(24):
        observed_category = (
            (observed_by_hour or {}).get(hour) or {}
        ).get("category")
        if hour <= observed_cutoff and observed_category in DIRECTION_CATEGORIES:
            probabilities = {
                category: 1.0 if category == observed_category else 0.0
                for category in DIRECTION_CATEGORIES
            }
            if observed_category == previous_category:
                duration += 1
            else:
                previous_category = observed_category
                duration = 1
        else:
            probabilities = _transition_probabilities(
                transition_model,
                hour,
                previous_category,
                duration,
            )
            predicted = max(probabilities, key=probabilities.get)
            if predicted == previous_category:
                duration += 1
            else:
                previous_category = predicted
                duration = 1
        result[hour] = probabilities
    return result


def _system_direction_ensemble_weights(
    history_accuracy: float | None,
    ml_accuracy: float | None,
    transition_accuracy: float | None,
    *,
    learning_accuracy: float | None = None,
    history_available: bool = True,
    ml_available: bool = True,
    transition_available: bool = True,
    learning_available: bool = False,
    operational_available: bool = False,
) -> dict[str, float]:
    raw = {
        "history": (
            max(0.18, ((history_accuracy or 55.0) / 100) ** 2)
            if history_available
            else 0.0
        ),
        "ml": (
            max(0.18, ((ml_accuracy or 50.0) / 100) ** 2)
            if ml_available
            else 0.0
        ),
        "transition": (
            max(
                0.14,
                ((transition_accuracy or 50.0) / 100) ** 2 * 0.82,
            )
            if transition_available
            else 0.0
        ),
        "learning": (
            max(
                0.12,
                ((learning_accuracy or 50.0) / 100) ** 2 * 0.72,
            )
            if learning_available
            else 0.0
        ),
        "operational": 0.16 if operational_available else 0.0,
    }
    total = sum(raw.values()) or 1.0
    return {
        key: round(value / total, 4)
        for key, value in raw.items()
    }


def _system_direction_operational_probabilities(
    rows: list[dict[str, Any]],
    observed_cutoff: int,
    target_hour: int,
    *,
    consumption_rows: list[dict[str, Any]] | None = None,
) -> dict[str, float] | None:
    available = []
    for row in rows:
        try:
            row_hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if (
            row_hour <= observed_cutoff
            and any(
                row.get(field) is not None
                for field in ("yal", "yat", "ptf", "smf")
            )
        ):
            available.append(row)
    consumption_available = []
    for row in consumption_rows or []:
        try:
            row_hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if (
            row_hour <= observed_cutoff
            and row.get("actual") is not None
            and row.get("forecast") is not None
        ):
            consumption_available.append(row)
    if not available and not consumption_available:
        return None
    latest = (
        max(available, key=lambda row: int(row.get("hour", -1)))
        if available
        else {}
    )
    yal = _weather_number(latest.get("yal")) or 0.0
    yat = abs(_weather_number(latest.get("yat")) or 0.0)
    ptf = _weather_number(latest.get("ptf"))
    smf = _weather_number(latest.get("smf"))
    quantity_total = yal + yat
    quantity_signal = (
        (yal - yat) / quantity_total
        if quantity_total > 0
        else 0.0
    )
    price_signal = (
        _bounded((smf - ptf) / max(abs(ptf), 500.0) * 3.0, -1, 1)
        if smf is not None and ptf is not None
        else 0.0
    )
    demand_signal = 0.0
    if consumption_available:
        latest_consumption = max(
            consumption_available,
            key=lambda row: int(row.get("hour", -1)),
        )
        actual_consumption = _weather_number(
            latest_consumption.get("actual")
        )
        forecast_consumption = _weather_number(
            latest_consumption.get("forecast")
        )
        if (
            actual_consumption is not None
            and forecast_consumption not in (None, 0)
        ):
            demand_signal = _bounded(
                (actual_consumption - forecast_consumption)
                / abs(forecast_consumption)
                * 5.0,
                -1,
                1,
            )
    horizon_decay = math.exp(-max(1, target_hour - observed_cutoff) / 5.0)
    net_signal = _bounded(
        (
            quantity_signal * 0.56
            + price_signal * 0.27
            + demand_signal * 0.17
        )
        * horizon_decay,
        -1,
        1,
    )
    deficit = 0.34 + max(0.0, net_signal) * 0.48
    surplus = 0.34 + max(0.0, -net_signal) * 0.48
    balanced = max(0.08, 1.0 - deficit - surplus)
    total = deficit + surplus + balanced
    return {
        "deficit": deficit / total,
        "surplus": surplus / total,
        "balanced": balanced / total,
    }


def _system_direction_forecast(
    target_date: str | None,
    client: Any,
    *,
    force_refresh: bool = False,
    allow_past: bool = False,
    context_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_tr = datetime.now(URETIM.TR_TZ)
    today = current_tr.date()
    automatic_schedule = _system_direction_schedule(current_tr)
    if not target_date:
        target_day = date.fromisoformat(automatic_schedule["targetDate"])
    else:
        try:
            target_day = date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError("Geçerli bir tahmin tarihi seçin.") from exc
    if allow_past:
        if target_day > today:
            raise ValueError("Gerçek değerlerle karşılaştırmak için bugünden ileri tarih seçilemez.")
        if target_day < today - timedelta(days=30):
            raise ValueError("Tahmin doğrulaması son 30 gün için hazırlanır.")
    elif target_day < today:
        raise ValueError("Tahmin tarihi bugünden önce olamaz.")
    if not allow_past and target_day > today + timedelta(days=7):
        raise ValueError("Sistem yönü tahmini en fazla 7 gün ileri için hazırlanır.")

    forecast_mode = (
        "intraday"
        if not allow_past and target_day == today and current_tr.hour < 18
        else "day_ahead"
    )
    cutoff_day = min(today, target_day - timedelta(days=1))
    cache_key = (
        f"v4:{target_day.isoformat()}:{cutoff_day.isoformat()}:"
        f"{forecast_mode}:{current_tr.strftime('%Y%m%d%H')}"
    )
    now = time.time()
    if not force_refresh:
        with SYSTEM_DIRECTION_FORECAST_CACHE_LOCK:
            cached = SYSTEM_DIRECTION_FORECAST_CACHE.get(cache_key)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    specs = _system_direction_sample_specs(target_day, cutoff_day)
    histories: dict[str, list[dict[str, Any]]] = {}
    calibration_history: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []

    # 12 haftalık aynı-gün örneklerini tek, kontrollü istekte tutar.
    # 84 gün x 24 saat = 2016 satır; EPİAŞ sayfa sınırının altında kalır.
    recent_floor = cutoff_day - timedelta(days=84)
    bulk_dates = sorted(day for day in specs if day >= recent_floor)
    if bulk_dates:
        try:
            bulk_end = max(bulk_dates)
            if forecast_mode == "intraday":
                bulk_end = max(bulk_end, target_day)
            bulk = _system_direction_range(
                min(bulk_dates),
                bulk_end,
                client,
                force_refresh=force_refresh,
            )
            bulk_history = bulk.get("byDate", {})
            calibration_history.update(bulk_history)
            for day in bulk_dates:
                histories[day.isoformat()] = bulk_history.get(
                    day.isoformat(),
                    [],
                )
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            warnings.append("Son günler ve haftalık örnekler alınamadı.")

    for day in sorted(day for day in specs if day < recent_floor):
        try:
            history = _system_direction_range(
                day,
                day,
                client,
                force_refresh=force_refresh,
            )
            histories[day.isoformat()] = history.get("byDate", {}).get(
                day.isoformat(),
                [],
            )
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            warnings.append(f"{day.isoformat()} örneği alınamadı.")

    history_index = _direction_history_index(calibration_history)
    target_actual_by_hour = history_index.get(target_day, {})
    observed_by_hour = {
        hour: row
        for hour, row in target_actual_by_hour.items()
        if forecast_mode == "intraday" and hour < current_tr.hour
    }
    observed_cutoff = max(observed_by_hour, default=-1)

    sample_summaries: list[dict[str, Any]] = []
    hourly_scores: dict[int, dict[str, float]] = {
        hour: {category: 0.0 for category in DIRECTION_CATEGORIES}
        for hour in range(24)
    }
    hourly_support: dict[int, int] = {hour: 0 for hour in range(24)}
    source_bucket_counts = {source: 0 for source in DIRECTION_SOURCE_WEIGHTS}
    used_sample_count = 0
    published_hour_count = 0

    for sample_day, sources in sorted(specs.items(), reverse=True):
        rows = histories.get(sample_day.isoformat(), [])
        rows_by_hour = {int(row["hour"]): row for row in rows if "hour" in row}
        published = sum(
            1
            for row in rows_by_hour.values()
            if row.get("category") in DIRECTION_CATEGORIES
        )
        if not published:
            continue
        completeness = published / 24
        weight = round(
            _system_direction_sample_weight(target_day, sample_day, sources)
            * (0.72 + 0.28 * completeness),
            4,
        )
        counts = {category: 0 for category in DIRECTION_CATEGORIES}
        for source in sources:
            source_bucket_counts[source] += 1
        for hour, row in rows_by_hour.items():
            category = row.get("category")
            if category not in DIRECTION_CATEGORIES:
                continue
            hourly_scores[hour][category] += weight
            hourly_support[hour] += 1
            counts[category] += 1
            published_hour_count += 1
        used_sample_count += 1
        dominant_category = max(counts, key=counts.get)
        sample_summaries.append(
            {
                "date": sample_day.isoformat(),
                "weight": weight,
                "sources": [
                    DIRECTION_SOURCE_LABELS.get(source, source)
                    for source in sorted(sources)
                ],
                "publishedHours": published,
                "dominantCategory": dominant_category,
                "dominantLabel": _direction_label(dominant_category),
                "counts": counts,
            }
        )

    # Komşu saatlerdeki küçük rejim kaymalarını yumuşat; ana saat sinyali
    # her zaman baskın kalır.
    smoothed_scores: dict[int, dict[str, float]] = {
        hour: dict(hourly_scores[hour])
        for hour in range(24)
    }
    for hour in range(24):
        for neighbor, factor in ((hour - 1, 0.10), (hour + 1, 0.10)):
            if neighbor < 0 or neighbor > 23:
                continue
            for category in DIRECTION_CATEGORIES:
                smoothed_scores[hour][category] += (
                    hourly_scores[neighbor][category] * factor
                )

    external_context = context_data
    can_load_live_context = (
        isinstance(client, URETIM.EpiasClient)
        and today <= target_day <= today + timedelta(days=1)
    )
    if external_context is None and can_load_live_context:
        external_context = _system_direction_context(
            target_day,
            client,
            force_refresh=force_refresh,
        )
    if external_context:
        warnings.extend(external_context.get("warnings") or [])
        smoothed_scores, context_by_hour = _apply_system_direction_context(
            smoothed_scores,
            external_context,
        )
    else:
        context_by_hour = {}
        external_context = {
            "inputs": [
                {
                    "key": "history",
                    "label": "Sistem yönü geçmişi",
                    "status": "ready",
                    "detail": "Gün · hafta · ay · yıl",
                },
                {
                    "key": "external",
                    "label": "İleriye dönük parametreler",
                    "status": "fallback",
                    "detail": "Tarihsel model",
                },
            ]
        }

    operational_rows = list(
        (external_context or {}).get("operationalRows") or []
    )
    if (
        forecast_mode == "intraday"
        and not operational_rows
        and isinstance(client, URETIM.EpiasClient)
    ):
        try:
            intraday_market = _market_dashboard(
                target_day.isoformat(),
                client,
                force_refresh=force_refresh,
            )
            operational_rows = intraday_market.get("rows") or []
            warnings.extend(intraday_market.get("warnings") or [])
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            warnings.append(
                "Gün içi YAL/YAT ve PTF-SMF sinyali alınamadı; "
                "diğer modeller kullanılmaya devam edildi."
            )
    consumption_context_rows = list(
        ((external_context or {}).get("consumption") or {}).get("rows")
        or []
    )
    operational_hours: list[int] = []
    for row in operational_rows:
        try:
            row_hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if (
            0 <= row_hour <= current_tr.hour
            and any(
                row.get(field) is not None
                for field in ("yal", "yat", "ptf", "smf")
            )
        ):
            operational_hours.append(row_hour)
    for row in consumption_context_rows:
        try:
            row_hour = int(row.get("hour", -1))
        except (TypeError, ValueError):
            continue
        if (
            0 <= row_hour <= current_tr.hour
            and row.get("actual") is not None
            and row.get("forecast") is not None
        ):
            operational_hours.append(row_hour)
    operational_cutoff = max(operational_hours, default=-1)

    error_learning_scores = {
        hour: dict(smoothed_scores[hour])
        for hour in range(24)
    }
    calibration = _system_direction_backtest_calibration(
        target_day,
        calibration_history,
    )
    smoothed_scores, calibration_by_hour = _apply_system_direction_calibration(
        smoothed_scores,
        calibration,
    )
    error_learning_model = _system_direction_error_learning_model(
        target_day,
        calibration,
    )
    method_inputs = list(external_context.get("inputs") or [])
    calibration_ready = calibration.get("testedDays", 0) >= 3
    method_inputs.append(
        {
            "key": "calibration",
            "label": "Doğrulanmış sapma öğrenimi",
            "status": "ready" if calibration_ready else "fallback",
            "detail": (
                f"{calibration.get('testedDays', 0)} gün · "
                f"{calibration.get('testedHours', 0)} saat geriye dönük test"
                if calibration_ready
                else "Yeterli doğrulanmış gün bekleniyor"
            ),
        }
    )
    method_inputs.append(
        {
            "key": "error-learning",
            "label": "Sapmalardan öğrenen ML",
            "status": (
                "ready"
                if error_learning_model.get("available")
                else "fallback"
            ),
            "detail": (
                f"{error_learning_model.get('mistakeCount', 0)} sapma · "
                f"{error_learning_model.get('sampleCount', 0)} doğrulanmış saat · "
                f"%{error_learning_model.get('validationAccuracy', 0)} doğrulama"
                if error_learning_model.get("available")
                else (
                    f"{error_learning_model.get('mistakeCount', 0)} sapma bulundu; "
                    "güvenli eğitim eşiği bekleniyor"
                )
            ),
        }
    )

    day_model = _cached_direction_ml_model(
        target_day,
        history_index,
        "day_ahead",
    )
    intraday_model: dict[str, Any] = {
        "available": False,
        "sampleCount": 0,
        "trainingCacheHit": False,
    }
    if forecast_mode == "intraday":
        intraday_model = _cached_direction_ml_model(
            target_day,
            history_index,
            "intraday",
        )
    transition_model = _system_direction_transition_model(
        target_day,
        history_index,
    )
    transition_series = _system_direction_transition_series(
        target_day,
        history_index,
        transition_model,
        observed_by_hour=(
            observed_by_hour if forecast_mode == "intraday" else None
        ),
    )
    selected_ml_model = (
        intraday_model
        if forecast_mode == "intraday"
        and observed_cutoff >= 0
        and intraday_model.get("available")
        else day_model
    )
    selected_ml_name = (
        "Gün içi makine öğrenmesi"
        if selected_ml_model is intraday_model
        else "Gün öncesi makine öğrenmesi"
    )
    method_inputs.extend(
        [
            {
                "key": "machine-learning",
                "label": selected_ml_name,
                "status": (
                    "ready"
                    if selected_ml_model.get("available")
                    else "fallback"
                ),
                "detail": (
                    f"{selected_ml_model.get('sampleCount', 0)} örnek · "
                    f"%{selected_ml_model.get('validationAccuracy', 0)} "
                    "kronolojik doğrulama"
                    if selected_ml_model.get("available")
                    else "Tarihsel model devrede"
                ),
            },
            {
                "key": "regime-transition",
                "label": "Rejim süresi ve geçiş modeli",
                "status": (
                    "ready"
                    if transition_model.get("testedHours")
                    else "fallback"
                ),
                "detail": (
                    f"{transition_model.get('testedHours', 0)} saat · "
                    f"%{transition_model.get('accuracy', 0)} doğrulama"
                    if transition_model.get("testedHours")
                    else "Geçiş örneği bekleniyor"
                ),
            },
            {
                "key": "intraday-market",
                "label": "Canlı dengeleme sinyalleri",
                "status": (
                    "ready"
                    if forecast_mode == "intraday"
                    and operational_cutoff >= 0
                    else "fallback"
                ),
                "detail": (
                    f"{operational_cutoff:02}:00 yayınına kadar"
                    if forecast_mode == "intraday"
                    and operational_cutoff >= 0
                    else "Yalnızca gün içi modelde"
                ),
            },
        ]
    )

    forecast_rows: list[dict[str, Any]] = []
    predicted_counts = {category: 0 for category in DIRECTION_CATEGORIES}
    confidence_values: list[float] = []
    error_learning_base_probabilities: dict[int, dict[str, float]] = {}
    error_learning_base_predictions: dict[int, str] = {}
    for hour in range(24):
        base_scores = error_learning_scores[hour]
        base_total = sum(base_scores.values())
        base_probabilities = {
            category: (
                base_scores[category] / base_total
                if base_total > 0
                else 0.0
            )
            for category in DIRECTION_CATEGORIES
        }
        error_learning_base_probabilities[hour] = base_probabilities
        error_learning_base_predictions[hour] = (
            max(base_probabilities, key=base_probabilities.get)
            if base_total > 0
            else "missing"
        )
    ensemble_weight_totals = {
        "history": 0.0,
        "ml": 0.0,
        "transition": 0.0,
        "learning": 0.0,
        "operational": 0.0,
    }
    ensemble_weight_hours = 0
    for hour in range(24):
        scores = smoothed_scores[hour]
        historical_total = sum(scores.values())
        historical_probabilities = {
            label: (
                scores[label] / historical_total
                if historical_total > 0
                else 0.0
            )
            for label in DIRECTION_CATEGORIES
        }
        observed = (
            forecast_mode == "intraday"
            and hour in observed_by_hour
        )
        actual_category = (
            (observed_by_hour.get(hour) or {}).get("category")
            if observed
            else None
        )
        if observed:
            ml_probabilities = {
                label: 0.0 for label in DIRECTION_CATEGORIES
            }
        else:
            if (
                selected_ml_model is intraday_model
                and observed_cutoff >= 0
            ):
                ml_features = _direction_day_features(
                    target_day,
                    hour,
                    history_index,
                    observed_by_hour=observed_by_hour,
                    observed_cutoff=observed_cutoff,
                )
            else:
                ml_features = _direction_day_features(
                    target_day,
                    hour,
                    history_index,
                )
            ml_probabilities = _softmax_direction_probabilities(
                selected_ml_model,
                ml_features,
            )
        regime_probabilities = transition_series.get(
            hour,
            {label: 0.0 for label in DIRECTION_CATEGORIES},
        )
        error_learning_probabilities = (
            _softmax_direction_probabilities(
                error_learning_model,
                _direction_error_learning_features(
                    target_day,
                    hour,
                    error_learning_base_probabilities[hour],
                    error_learning_base_predictions[hour],
                    error_learning_base_predictions.get(hour - 1),
                ),
            )
            if error_learning_model.get("available") and not observed
            else {label: 0.0 for label in DIRECTION_CATEGORIES}
        )
        operational_probabilities = (
            _system_direction_operational_probabilities(
                operational_rows,
                operational_cutoff,
                hour,
                consumption_rows=consumption_context_rows,
            )
            if (
                forecast_mode == "intraday"
                and operational_cutoff >= 0
                and not observed
            )
            else None
        )

        if observed and actual_category in DIRECTION_CATEGORIES:
            category = actual_category
            combined_probabilities = {
                label: 1.0 if label == actual_category else 0.0
                for label in DIRECTION_CATEGORIES
            }
            weights = {
                "history": 0.0,
                "ml": 0.0,
                "transition": 0.0,
                "learning": 0.0,
                "operational": 0.0,
            }
            confidence = 100.0
        else:
            weights = _system_direction_ensemble_weights(
                calibration.get("accuracy"),
                selected_ml_model.get("validationAccuracy"),
                transition_model.get("accuracy"),
                learning_accuracy=error_learning_model.get(
                    "validationAccuracy"
                ),
                history_available=historical_total > 0,
                ml_available=bool(selected_ml_model.get("available")),
                transition_available=bool(
                    transition_model.get("testedHours")
                ),
                learning_available=bool(
                    error_learning_model.get("available")
                ),
                operational_available=(
                    operational_probabilities is not None
                ),
            )
            combined_probabilities = {
                label: (
                    historical_probabilities[label] * weights["history"]
                    + ml_probabilities[label] * weights["ml"]
                    + regime_probabilities[label] * weights["transition"]
                    + error_learning_probabilities[label] * weights["learning"]
                    + (
                        (operational_probabilities or {}).get(label, 0.0)
                        * weights["operational"]
                    )
                )
                for label in DIRECTION_CATEGORIES
            }
            combined_total = sum(combined_probabilities.values())
            if combined_total > 0:
                combined_probabilities = {
                    label: value / combined_total
                    for label, value in combined_probabilities.items()
                }
                category = max(
                    combined_probabilities,
                    key=combined_probabilities.get,
                )
                raw_confidence = combined_probabilities[category] * 100
                reliability = max(
                    min(1.0, hourly_support[hour] / 10),
                    0.72
                    if selected_ml_model.get("available")
                    else 0.0,
                )
                confidence = round(
                    33.3 + (raw_confidence - 33.3) * reliability,
                    1,
                )
            else:
                category = "missing"
                confidence = 0.0

        if category == "missing":
            category = "missing"
            probabilities = {label: 0.0 for label in DIRECTION_CATEGORIES}
            confidence = 0.0
        else:
            probabilities = {
                label: round(combined_probabilities[label] * 100, 1)
                for label in DIRECTION_CATEGORIES
            }
            predicted_counts[category] += 1
            if not observed:
                confidence_values.append(confidence)
        if not observed:
            for model_key, model_weight in weights.items():
                ensemble_weight_totals[model_key] += model_weight
            ensemble_weight_hours += 1
        forecast_rows.append(
            {
                "hour": hour,
                "time": f"{hour:02}:00",
                "category": category,
                "label": _direction_label(category),
                "confidence": round(confidence, 1),
                "probabilities": probabilities,
                "support": hourly_support[hour],
                "mode": forecast_mode,
                "observed": observed,
                "context": context_by_hour.get(hour, {}),
                "calibration": calibration_by_hour.get(hour, {}),
                "modelContributions": {
                    "weights": weights,
                    "history": {
                        label: round(value * 100, 1)
                        for label, value in historical_probabilities.items()
                    },
                    "machineLearning": {
                        label: round(value * 100, 1)
                        for label, value in ml_probabilities.items()
                    },
                    "regimeTransition": {
                        label: round(value * 100, 1)
                        for label, value in regime_probabilities.items()
                    },
                    "errorLearning": {
                        label: round(value * 100, 1)
                        for label, value in error_learning_probabilities.items()
                    },
                    "operational": {
                        label: round(value * 100, 1)
                        for label, value in (
                            operational_probabilities or {}
                        ).items()
                    },
                },
            }
        )

    average_ensemble_weights = {
        key: (
            round(value / ensemble_weight_hours, 4)
            if ensemble_weight_hours
            else 0.0
        )
        for key, value in ensemble_weight_totals.items()
    }
    dominant_category = (
        max(predicted_counts, key=predicted_counts.get)
        if sum(predicted_counts.values())
        else "missing"
    )
    average_confidence = (
        sum(confidence_values) / len(confidence_values)
        if confidence_values
        else 0.0
    )
    low_confidence_hours = [
        row["time"]
        for row in forecast_rows
        if row["category"] != "missing" and row["confidence"] < 48
    ]
    if target_day == today:
        target_label = "Bugün"
    elif target_day == today + timedelta(days=1):
        target_label = "Yarın"
    else:
        target_label = "Seçili gün"
    schedule = (
        automatic_schedule
        if not target_date
        else {
            "phase": "selected",
            "targetDate": target_day.isoformat(),
            "targetLabel": target_label,
            "validationDate": min(today, target_day).isoformat(),
            "switchHour": 18,
            "switchAt": automatic_schedule["switchAt"],
            "currentTime": automatic_schedule["currentTime"],
            "headline": f"{target_label} sistem yönü tahmini",
            "detail": (
                "Tahmin yalnızca hedef günden önce yayımlanmış geçmiş verilerle hazırlanır."
            ),
        }
    )
    if forecast_mode == "intraday":
        schedule = {
            **schedule,
            "headline": "Bugünün gün içi sistem yönü",
            "detail": (
                f"{len(observed_by_hour)} yayımlanmış saat gerçekleşen olarak "
                f"sabitlendi; kalan {24 - len(observed_by_hour)} saat gün içi "
                "modeliyle tahmin edildi."
            ),
        }
    payload = {
        "targetDate": target_day.isoformat(),
        "targetLabel": target_label,
        "forecastMode": forecast_mode,
        "schedule": schedule,
        "generatedAt": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "forecastRows": forecast_rows,
        "summary": {
            "dominantCategory": dominant_category,
            "dominantLabel": _direction_label(dominant_category),
            "predictedCounts": predicted_counts,
            "averageConfidence": round(average_confidence, 1),
            "sampleCount": used_sample_count,
            "publishedHourCount": published_hour_count,
            "lowConfidenceHours": low_confidence_hours,
            "observedHours": len(observed_by_hour),
            "forecastHours": 24 - len(observed_by_hour),
        },
        "modelSummary": {
            "mode": forecast_mode,
            "name": (
                "Gün içi ensemble"
                if forecast_mode == "intraday"
                else "Gün öncesi ensemble"
            ),
            "detail": (
                f"ML %{selected_ml_model.get('validationAccuracy') or 0} · "
                f"Rejim %{transition_model.get('accuracy') or 0} · "
                f"Sapma ML %{error_learning_model.get('validationAccuracy') or 0} "
                "doğrulama"
            ),
            "machineLearning": {
                "name": selected_ml_name,
                "available": bool(selected_ml_model.get("available")),
                "sampleCount": selected_ml_model.get("sampleCount", 0),
                "trainingCacheHit": bool(
                    selected_ml_model.get("trainingCacheHit")
                ),
                "validationAccuracy": selected_ml_model.get(
                    "validationAccuracy"
                ),
            },
            "regimeTransition": {
                "testedHours": transition_model.get("testedHours", 0),
                "validationAccuracy": transition_model.get("accuracy"),
            },
            "errorLearning": {
                "name": "Sapmalardan öğrenen hata düzeltme ML",
                "available": bool(error_learning_model.get("available")),
                "sampleCount": error_learning_model.get("sampleCount", 0),
                "mistakeCount": error_learning_model.get("mistakeCount", 0),
                "correctCount": error_learning_model.get("correctCount", 0),
                "trainedThrough": error_learning_model.get("trainedThrough"),
                "validationAccuracy": error_learning_model.get(
                    "validationAccuracy"
                ),
            },
            "averageWeights": average_ensemble_weights,
        },
        "method": {
            "name": "Çok modelli sistem yönü ensemble",
            "description": (
                "Gün öncesi ve gün içi tahminler ayrı eğitilir. Tarihsel "
                "benzerlik; saat, hafta günü, mevsim, önceki gün/hafta gecikmeleri "
                "ve son 7/28 günlük rejim dağılımıyla eğitilen makine öğrenmesi; "
                "rejimin kaç saattir sürdüğünü okuyan geçiş modeliyle birlikte "
                "performansına göre ağırlıklandırılır. KGÜP, tüketim tahmini, PTF "
                "ve hava koşulları her iki modelde kontrollü etki yapar. Gün içi "
                "model yalnızca o ana kadar yayımlanmış sistem yönü, YAL/YAT ve "
                "PTF-SMF farkıyla tüketim tahmin hatasını kullanır. Ayrı hata "
                "düzeltme modeli, geçmiş yürüyen testlerdeki yanlış saatlere daha "
                "fazla ağırlık vererek tekrar eden saat ve rejim geçişi sapmalarını "
                "öğrenir. Hedef saatin "
                "gerçekleşen yönü ile "
                "sonradan kesinleşen UEVM-UEÇM değerleri tahmin girdisine alınmaz; "
                "böylece sonuç sızıntısı engellenir. Resmî EPİAŞ tahmini değildir."
            ),
            "inputs": method_inputs,
            "sourceBuckets": [
                {
                    "key": source,
                    "label": DIRECTION_SOURCE_LABELS[source],
                    "weight": DIRECTION_SOURCE_WEIGHTS[source],
                    "sampleCount": source_bucket_counts[source],
                }
                for source in DIRECTION_SOURCE_WEIGHTS
            ],
        },
        "samples": sample_summaries[:36],
        "contextSummary": {
            "weatherAvailableHours": (
                (external_context.get("weather") or {}).get("availableHours", 0)
            ),
            "weatherPointCount": (
                (external_context.get("weather") or {}).get("pointCount", 0)
            ),
            "kgupAvailableHours": (
                (external_context.get("kgup") or {}).get("availableHours", 0)
            ),
            "consumptionForecastHours": (
                (
                    (external_context.get("consumption") or {}).get("summary")
                    or {}
                ).get("forecastHours", 0)
            ),
            "ptfAvailableHours": (
                (external_context.get("market") or {}).get("availableHours", 0)
            ),
            "weatherSource": (
                (external_context.get("weather") or {}).get("source")
            ),
            "calibrationTestedDays": calibration.get("testedDays", 0),
            "calibrationTestedHours": calibration.get("testedHours", 0),
            "calibrationAccuracy": calibration.get("accuracy"),
            "forecastMode": forecast_mode,
            "observedHours": len(observed_by_hour),
            "machineLearningSamples": selected_ml_model.get(
                "sampleCount",
                0,
            ),
            "machineLearningAccuracy": selected_ml_model.get(
                "validationAccuracy"
            ),
            "transitionAccuracy": transition_model.get("accuracy"),
            "errorLearningSamples": error_learning_model.get("sampleCount", 0),
            "errorLearningMistakes": error_learning_model.get(
                "mistakeCount",
                0,
            ),
            "errorLearningAccuracy": error_learning_model.get(
                "validationAccuracy"
            ),
            "ensembleWeights": average_ensemble_weights,
        },
        "calibrationSummary": {
            "testedDays": calibration.get("testedDays", 0),
            "testedHours": calibration.get("testedHours", 0),
            "accuracy": calibration.get("accuracy"),
            "adjustedHours": sum(
                1
                for detail in calibration_by_hour.values()
                if detail.get("applied")
            ),
            "changedHours": sum(
                1
                for detail in calibration_by_hour.values()
                if detail.get("changedDirection")
            ),
            "errorLearningAvailable": bool(
                error_learning_model.get("available")
            ),
            "errorLearningSamples": error_learning_model.get("sampleCount", 0),
            "errorLearningMistakes": error_learning_model.get(
                "mistakeCount",
                0,
            ),
            "errorLearningAccuracy": error_learning_model.get(
                "validationAccuracy"
            ),
        },
        "qualityMetrics": calibration.get("qualityMetrics") or {},
        "warnings": warnings,
        "cached": False,
    }
    with SYSTEM_DIRECTION_FORECAST_CACHE_LOCK:
        SYSTEM_DIRECTION_FORECAST_CACHE[cache_key] = {
            "payload": payload,
            "expires": time.time() + 1800,
        }
    return payload


def _system_direction_miss_reason(
    hour: int,
    forecast_by_hour: dict[int, dict[str, Any]],
    actual_by_hour: dict[int, dict[str, Any]],
) -> dict[str, str]:
    forecast_row = forecast_by_hour.get(hour) or {}
    forecast_category = forecast_row.get("category")
    actual_category = (actual_by_hour.get(hour) or {}).get("category")

    # Detect a predicted regime switch that the actual series did not make.
    for transition_hour in range(hour, max(0, hour - 3), -1):
        current_forecast = (
            forecast_by_hour.get(transition_hour) or {}
        ).get("category")
        previous_forecast = (
            forecast_by_hour.get(transition_hour - 1) or {}
        ).get("category")
        if (
            current_forecast not in DIRECTION_CATEGORIES
            or previous_forecast not in DIRECTION_CATEGORIES
            or current_forecast == previous_forecast
        ):
            continue
        actual_window = [
            (actual_by_hour.get(check_hour) or {}).get("category")
            for check_hour in range(transition_hour - 1, hour + 1)
        ]
        if actual_window and all(
            category == previous_forecast
            for category in actual_window
        ):
            persisted_hours = hour - transition_hour + 1
            return {
                "code": "early_transition",
                "text": (
                    f"Rejim geçişi {transition_hour:02}:00 için erken öngörüldü; "
                    f"gerçek {_direction_label(previous_forecast).lower()} yönü "
                    f"{persisted_hours} saat daha sürdü."
                ),
            }

    probabilities = forecast_row.get("probabilities") or {}
    ranked_probabilities = sorted(
        (
            float(probabilities.get(category, 0.0)),
            category,
        )
        for category in DIRECTION_CATEGORIES
    )
    probability_margin = (
        ranked_probabilities[-1][0] - ranked_probabilities[-2][0]
        if len(ranked_probabilities) >= 2
        else 100.0
    )
    confidence = float(forecast_row.get("confidence") or 0.0)
    if confidence < 55 or probability_margin < 8:
        return {
            "code": "low_margin",
            "text": (
                f"Tahmin sınırdaydı: en güçlü iki yön arasındaki fark "
                f"%{round(probability_margin, 1)} ve güven %{round(confidence, 1)} idi."
            ),
        }

    signals = (forecast_row.get("context") or {}).get("signals") or []
    signal_labels = [
        str(signal.get("label"))
        for signal in signals
        if signal.get("label")
    ][:2]
    if signal_labels:
        return {
            "code": "external_signal",
            "text": (
                f"{' ve '.join(signal_labels)} tahmini "
                f"{_direction_label(forecast_category).lower()} yönüne itti; "
                f"gerçek yön {_direction_label(actual_category).lower()} kaldı."
            ),
        }

    calibration = forecast_row.get("calibration") or {}
    if calibration.get("applied"):
        return {
            "code": "historical_pattern",
            "text": (
                f"Bu saat için geçmiş doğrulama başarısı "
                f"%{calibration.get('historicalAccuracy', 0)} olmasına rağmen "
                "günün gerçekleşen rejimi tarihsel desenden ayrıştı."
            ),
        }
    return {
        "code": "historical_pattern",
        "text": (
            f"Tarihsel saat deseni {_direction_label(forecast_category).lower()} "
            f"yönünü öne çıkardı; gerçek yön {_direction_label(actual_category).lower()} oldu."
        ),
    }


def _system_direction_validation(
    selected_date: str | None,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    today = datetime.now(URETIM.TR_TZ).date()
    if not selected_date:
        validation_day = today
    else:
        try:
            validation_day = date.fromisoformat(selected_date)
        except ValueError as exc:
            raise ValueError("Geçerli bir karşılaştırma tarihi seçin.") from exc
    if validation_day > today:
        raise ValueError("Gerçek değerlerle karşılaştırmak için bugünden ileri tarih seçilemez.")
    if validation_day < today - timedelta(days=30):
        raise ValueError("Tahmin doğrulaması son 30 gün için hazırlanır.")

    forecast = _system_direction_forecast(
        validation_day.isoformat(),
        client,
        force_refresh=force_refresh,
        allow_past=True,
    )
    actual = _system_direction_range(
        validation_day,
        validation_day,
        client,
        force_refresh=force_refresh,
    )
    actual_rows = actual.get("byDate", {}).get(validation_day.isoformat(), [])
    actual_by_hour = {
        int(row["hour"]): row
        for row in actual_rows
        if "hour" in row and row.get("category") in DIRECTION_CATEGORIES
    }

    forecast_rows = forecast.get("forecastRows", [])
    forecast_by_hour = {
        int(row.get("hour", 0)): row
        for row in forecast_rows
    }
    rows: list[dict[str, Any]] = []
    compared_hours = 0
    correct_hours = 0
    miss_reason_counts: dict[str, int] = {}
    for forecast_row in forecast_rows:
        hour = int(forecast_row.get("hour", 0))
        actual_row = actual_by_hour.get(hour)
        forecast_category = forecast_row.get("category") or "missing"
        actual_category = (
            actual_row.get("category") if actual_row else "missing"
        )
        comparable = (
            forecast_category in DIRECTION_CATEGORIES
            and actual_category in DIRECTION_CATEGORIES
        )
        match = None
        if comparable:
            compared_hours += 1
            match = forecast_category == actual_category
            if match:
                correct_hours += 1
        reason = {"code": "", "text": ""}
        if match is False:
            reason = _system_direction_miss_reason(
                hour,
                forecast_by_hour,
                actual_by_hour,
            )
            reason_code = reason.get("code") or "other"
            miss_reason_counts[reason_code] = (
                miss_reason_counts.get(reason_code, 0) + 1
            )
        rows.append(
            {
                "hour": hour,
                "time": f"{hour:02}:00",
                "forecastCategory": forecast_category,
                "forecastLabel": _direction_label(forecast_category),
                "forecastConfidence": forecast_row.get("confidence", 0),
                "actualCategory": actual_category,
                "actualLabel": _direction_label(actual_category),
                "actualPublished": actual_category in DIRECTION_CATEGORIES,
                "match": match,
                "reasonCode": reason.get("code"),
                "reason": reason.get("text"),
            }
        )

    accuracy = (
        round(correct_hours / compared_hours * 100, 1)
        if compared_hours
        else None
    )
    status = (
        "ready"
        if compared_hours == 24
        else "partial"
        if compared_hours
        else "waiting"
    )
    status_label = {
        "ready": "Tamamlandı",
        "partial": "Kısmi veri",
        "waiting": "Gerçek veri bekleniyor",
    }[status]
    wrong_hours = max(0, compared_hours - correct_hours)
    early_transition_hours = miss_reason_counts.get("early_transition", 0)
    learning_summary = forecast.get("calibrationSummary") or {}
    learning_detail = (
        " Sapma öğrenimi "
        f"{learning_summary.get('errorLearningMistakes', 0)} geçmiş hata ve "
        f"{learning_summary.get('errorLearningSamples', 0)} doğrulanmış saatle "
        "etkin."
        if learning_summary.get("errorLearningAvailable")
        else " Sapma öğrenimi güvenli eğitim eşiği için yeni hata örneklerini izliyor."
    )
    if wrong_hours:
        analysis_note = (
            f"{wrong_hours} sapma analiz edildi"
            + (
                f"; {early_transition_hours} saat erken rejim geçişi kaynaklı."
                if early_transition_hours
                else "."
            )
            + " Son doğrulanmış sapmalar sonraki tahminlerin saat ve geçiş "
            "kalibrasyonunda sınırlı ağırlıkla kullanılır."
            + learning_detail
        )
    else:
        analysis_note = (
            "Yayımlanan saatlerde sapma yok. Doğrulanan sonuçlar sonraki "
            "tahminlerin geriye dönük kalibrasyonuna eklenir."
            + learning_detail
        )
    return {
        "date": validation_day.isoformat(),
        "generatedAt": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "forecastGeneratedAt": forecast.get("generatedAt"),
        "rows": rows,
        "qualityMetrics": forecast.get("qualityMetrics") or {},
        "summary": {
            "status": status,
            "statusLabel": status_label,
            "publishedHours": len(actual_by_hour),
            "comparedHours": compared_hours,
            "correctHours": correct_hours,
            "wrongHours": wrong_hours,
            "missingHours": max(0, 24 - len(actual_by_hour)),
            "accuracy": accuracy,
        },
        "note": analysis_note,
        "analysis": {
            "reasonCounts": miss_reason_counts,
            "earlyTransitionHours": early_transition_hours,
            "learningEnabled": True,
            "errorLearningAvailable": bool(
                learning_summary.get("errorLearningAvailable")
            ),
            "errorLearningSamples": learning_summary.get(
                "errorLearningSamples",
                0,
            ),
            "errorLearningMistakes": learning_summary.get(
                "errorLearningMistakes",
                0,
            ),
            "errorLearningAccuracy": learning_summary.get(
                "errorLearningAccuracy"
            ),
            "calibrationTestedDays": (
                forecast.get("calibrationSummary") or {}
            ).get("testedDays", 0),
            "calibrationTestedHours": (
                forecast.get("calibrationSummary") or {}
            ).get("testedHours", 0),
        },
        "cached": bool(forecast.get("cached") or actual.get("cached")),
    }


def _next_day_ptf_publication(
    target_day: date,
    now_tr: datetime | None = None,
) -> dict[str, Any]:
    """Describe the official next-day PTF publication phase in Turkey time."""

    current = now_tr or datetime.now(URETIM.TR_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=URETIM.TR_TZ)
    else:
        current = current.astimezone(URETIM.TR_TZ)
    publication_day = target_day - timedelta(days=1)
    day_start = datetime.combine(
        publication_day,
        datetime.min.time(),
        tzinfo=URETIM.TR_TZ,
    )
    preliminary_at = day_start + timedelta(hours=13)
    final_at = day_start + timedelta(hours=14)
    if current >= final_at:
        status = "final"
        label = "Kesinleşmiş PTF"
        next_refresh_at = None
    elif current >= preliminary_at:
        status = "preliminary"
        label = "Kesinleşmemiş PTF"
        next_refresh_at = final_at.isoformat(timespec="seconds")
    else:
        status = "waiting"
        label = "Kesinleşmemiş PTF bekleniyor"
        next_refresh_at = preliminary_at.isoformat(timespec="seconds")
    return {
        "status": status,
        "label": label,
        "preliminaryAt": preliminary_at.isoformat(timespec="seconds"),
        "finalAt": final_at.isoformat(timespec="seconds"),
        "nextRefreshAt": next_refresh_at,
    }


def _next_day_ptf_dashboard(
    selected_date: str,
    client: Any,
    *,
    force_refresh: bool = False,
    now_tr: datetime | None = None,
) -> dict[str, Any]:
    """Return only EPİAŞ's directly published PTF for the following day."""
    try:
        selected_day = date.fromisoformat(selected_date)
    except ValueError as exc:
        raise ValueError("Geçerli bir tarih seçin.") from exc

    current = now_tr or datetime.now(URETIM.TR_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=URETIM.TR_TZ)
    else:
        current = current.astimezone(URETIM.TR_TZ)
    today = current.date()
    if selected_day > today:
        raise ValueError("Temel tarih bugünden ileri olamaz.")
    target_day = selected_day + timedelta(days=1)
    target_date = target_day.isoformat()
    publication = _next_day_ptf_publication(target_day, current)

    now = time.time()
    if not force_refresh:
        with NEXT_DAY_PTF_CACHE_LOCK:
            cached = NEXT_DAY_PTF_CACHE.get(target_date)
            cached_status = cached and cached["payload"].get("publication", {}).get("status")
            if (
                cached
                and cached["expires"] > now
                and cached_status == publication["status"]
            ):
                return {**cached["payload"], "cached": True}

    start_date = f"{target_date}T00:00:00+03:00"

    def fetch_ptf(kind: str) -> dict[str, Any]:
        if kind == "preliminary":
            endpoint = "/v1/markets/dam/data/interim-mcp"
            # EPİAŞ InterimMcpRequestDto yalnızca startDate ve sayfalama alır.
            body = {
                "startDate": start_date,
                "page": {"number": 1, "size": 100},
            }
        else:
            endpoint = "/v1/markets/dam/data/mcp"
            body = {
                "startDate": start_date,
                "endDate": start_date,
                "page": {"number": 1, "size": 100},
            }
        try:
            return _epias_post_json(
                client,
                endpoint,
                body,
                force_refresh=force_refresh,
            )
        except URETIM.EpiasError as exc:
            if exc.status_code in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.NOT_FOUND,
            }:
                return {}
            raise

    response_kind = publication["status"]
    response = (
        fetch_ptf(response_kind)
        if response_kind in {"preliminary", "final"}
        else {}
    )

    if response_kind == "preliminary" and _items(response):
        publication = {
            **publication,
            "status": "preliminary",
            "label": "Kesinleşmemiş PTF",
            "nextRefreshAt": publication["finalAt"],
        }
    elif response_kind == "final" and not _items(response):
        publication = {
            **publication,
            "label": "Kesinleşmiş PTF",
            "nextRefreshAt": (current + timedelta(minutes=2)).isoformat(
                timespec="seconds"
            ),
        }
    publication["source"] = {
        "preliminary": "interim-mcp",
        "final": "mcp",
    }.get(response_kind)

    stats = _section(response, "statistic", "statistics")
    rows_by_hour: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(_items(response)):
        hour = _hour_key(item, index)
        ptf_try = _number(item, "price", "marketTradePrice")
        rows_by_hour[hour] = {
            "hour": hour,
            "time": f"{hour:02}:00",
            "ptf": ptf_try,
            "ptfByCurrency": {
                "TRY": ptf_try,
                "EUR": _number(item, "priceEur"),
                "USD": _number(item, "priceUsd"),
            },
        }
    rows = [rows_by_hour[hour] for hour in sorted(rows_by_hour)]

    def currency_average(currency: str, statistic_field: str) -> float | None:
        official = _number(stats, statistic_field)
        if official is not None:
            return official
        values = [
            row["ptfByCurrency"].get(currency)
            for row in rows
            if row["ptfByCurrency"].get(currency) is not None
        ]
        return sum(values) / len(values) if values else None

    averages = {
        "TRY": currency_average(
            "TRY",
            "interimMcpAvg" if response_kind == "preliminary" else "priceAvg",
        ),
        "EUR": currency_average("EUR", "priceEurAvg"),
        "USD": currency_average("USD", "priceUsdAvg"),
    }
    available = [
        currency
        for currency in ("TRY", "EUR", "USD")
        if averages[currency] is not None
        or any(
            row["ptfByCurrency"].get(currency) is not None for row in rows
        )
    ]
    published_hours = sum(row["ptf"] is not None for row in rows)
    payload = {
        "baseDate": selected_date,
        "date": target_date,
        "rows": rows,
        "summary": {
            "ptfAverageByCurrency": averages,
            "publishedHours": published_hours,
        },
        "currencyInfo": {
            "available": available or ["TRY"],
            "appliesTo": "PTF",
            "mode": "epias-ptf-direct",
            "source": (
                "EPİAŞ K.PTF marketTradePrice"
                if response_kind == "preliminary"
                else (
                    "EPİAŞ PTF price / priceEur / priceUsd"
                    if response_kind == "final"
                    else "EPİAŞ PTF yayını bekleniyor"
                )
            ),
        },
        "published": published_hours > 0,
        "publication": publication,
        "updatedAt": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "cached": False,
    }
    ttl = 120 if target_day >= today else 21_600
    with NEXT_DAY_PTF_CACHE_LOCK:
        NEXT_DAY_PTF_CACHE[target_date] = {
            "payload": payload,
            "expires": time.time() + ttl,
        }
    return payload


def _consumption_dashboard(
    selected_date: str,
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """EPİAŞ gerçek zamanlı tüketimini 24 saatlik panel verisine dönüştür."""

    try:
        selected_day = date.fromisoformat(selected_date)
    except ValueError as exc:
        raise ValueError("Geçerli bir tarih seçin.") from exc
    today = datetime.now(URETIM.TR_TZ).date()
    if selected_day > today:
        raise ValueError("Bugünden ileri bir tarih seçilemez.")

    now = time.time()
    if not force_refresh:
        with CONSUMPTION_CACHE_LOCK:
            cached = CONSUMPTION_CACHE.get(selected_date)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    response = _epias_post_json(
        client,
        "/v1/consumption/data/realtime-consumption",
        {
            "startDate": f"{selected_date}T00:00:00+03:00",
            "endDate": f"{selected_date}T00:00:00+03:00",
            "page": {"number": 1, "size": 100},
        },
        force_refresh=force_refresh,
    )
    by_hour: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(_items(response)):
        hour = _hour_key(item, index)
        if not 0 <= hour <= 23:
            continue
        value = _number(
            item,
            "consumption",
            "consumptionAmount",
            "amount",
            "value",
        )
        if value is None:
            continue
        timestamp = str(item.get("date") or item.get("time") or "")
        by_hour[hour] = {
            "hour": hour,
            "time": f"{hour:02d}:00",
            "consumption": value,
            "timestamp": timestamp,
        }

    rows = [
        by_hour.get(
            hour,
            {
                "hour": hour,
                "time": f"{hour:02d}:00",
                "consumption": None,
                "timestamp": "",
            },
        )
        for hour in range(24)
    ]
    available = [row for row in rows if row["consumption"] is not None]
    values = [float(row["consumption"]) for row in available]
    latest = available[-1] if available else None
    previous = available[-2] if len(available) > 1 else None
    peak = max(available, key=lambda row: row["consumption"], default=None)
    lowest = min(available, key=lambda row: row["consumption"], default=None)
    statistics = _section(response, "statistics", "statistic")

    def statistic(name: str, fallback: float | None) -> float | None:
        value = _number(statistics, name)
        return value if value is not None else fallback

    average = sum(values) / len(values) if values else None
    total = sum(values) if values else None
    latest_change = (
        float(latest["consumption"]) - float(previous["consumption"])
        if latest and previous
        else None
    )
    latest_change_percent = (
        latest_change / float(previous["consumption"]) * 100
        if latest_change is not None and previous and previous["consumption"]
        else None
    )
    payload = {
        "date": selected_date,
        "rows": rows,
        "summary": {
            "latest": latest["consumption"] if latest else None,
            "latestHour": latest["time"] if latest else None,
            "latestChange": latest_change,
            "latestChangePercent": latest_change_percent,
            "average": statistic("consumptionAvg", average),
            "maximum": statistic(
                "consumptionMax",
                float(peak["consumption"]) if peak else None,
            ),
            "maximumHour": peak["time"] if peak else None,
            "minimum": statistic(
                "consumptionMin",
                float(lowest["consumption"]) if lowest else None,
            ),
            "minimumHour": lowest["time"] if lowest else None,
            "total": statistic("consumptionTotal", total),
            "availableHours": len(available),
            "missingHours": 24 - len(available),
        },
        "source": "EPİAŞ Şeffaflık Platformu",
        "publicationDelayHours": 2,
        "updatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "cached": False,
    }
    if available:
        ttl = 120 if selected_day == today else 21_600
        with CONSUMPTION_CACHE_LOCK:
            CONSUMPTION_CACHE[selected_date] = {
                "payload": payload,
                "expires": time.time() + ttl,
            }
    return payload


def _consumption_forecast_lock_meta(
    target_day: date,
    hour: int,
    *,
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """Saatlik tüketim tahmininin son güncelleme ve kilit bilgisini üret."""

    now_dt = now_dt or datetime.now(URETIM.TR_TZ)
    hour_start = (
        datetime.combine(target_day, datetime.min.time(), tzinfo=URETIM.TR_TZ)
        + timedelta(hours=hour)
    )
    final_update_at = hour_start - timedelta(hours=1, minutes=10)
    locked = now_dt >= final_update_at
    return {
        "locked": locked,
        "status": "Sabitlendi" if locked else "Güncellenebilir",
        "hourStartsAt": hour_start.isoformat(),
        "finalUpdateAt": final_update_at.isoformat(),
    }


def _consumption_forecast(
    base_date: str,
    client: Any,
    *,
    force_refresh: bool = False,
    target_date: str | None = None,
) -> dict[str, Any]:
    """Son 14 günün saat profilinden hedef gün için açıklanabilir tahmin üret."""

    try:
        base_day = date.fromisoformat(base_date)
    except ValueError as exc:
        raise ValueError("Geçerli bir tahmin başlangıç tarihi seçin.") from exc
    today = datetime.now(URETIM.TR_TZ).date()
    if base_day > today:
        raise ValueError("Tahmin başlangıcı bugünden ileri olamaz.")
    if target_date:
        try:
            target_day = date.fromisoformat(target_date)
        except ValueError as exc:
            raise ValueError("Geçerli bir tahmin hedef tarihi seçin.") from exc
    else:
        target_day = base_day + timedelta(days=1)
    if target_day > today + timedelta(days=1):
        raise ValueError("Tahmin hedefi yarından ileri olamaz.")

    training_end = target_day - timedelta(days=1)
    training_start = training_end - timedelta(days=13)
    cache_key = target_day.isoformat()
    now = time.time()
    if not force_refresh:
        with CONSUMPTION_FORECAST_CACHE_LOCK:
            cached = CONSUMPTION_FORECAST_CACHE.get(cache_key)
            if cached and cached["expires"] > now:
                return {**cached["payload"], "cached": True}

    response = _epias_post_json(
        client,
        "/v1/consumption/data/realtime-consumption",
        {
            "startDate": f"{training_start.isoformat()}T00:00:00+03:00",
            "endDate": f"{training_end.isoformat()}T00:00:00+03:00",
            "page": {"number": 1, "size": 500},
        },
        force_refresh=force_refresh,
    )
    history: dict[date, dict[int, float]] = {}
    for index, item in enumerate(_items(response)):
        timestamp = str(
            item.get("date")
            or item.get("tarih")
            or item.get("effectiveDate")
            or item.get("time")
            or ""
        )
        try:
            item_day = date.fromisoformat(timestamp[:10])
        except ValueError:
            continue
        if not training_start <= item_day <= training_end:
            continue
        hour = _hour_key(item, index)
        value = _number(
            item,
            "consumption",
            "consumptionAmount",
            "amount",
            "value",
        )
        if 0 <= hour <= 23 and value is not None:
            history.setdefault(item_day, {})[hour] = value

    actual = None
    if target_day <= today:
        actual = _consumption_dashboard(
            target_day.isoformat(),
            client,
            force_refresh=force_refresh,
        )
    actual_by_hour = {
        int(row.get("hour")): row.get("consumption")
        for row in (actual or {}).get("rows") or []
        if row.get("hour") is not None
    }

    rows = []
    forecast_values = []
    absolute_errors = []
    percentage_errors = []
    lock_meta_rows = []
    now_dt = datetime.now(URETIM.TR_TZ)
    for hour in range(24):
        weighted_samples = []
        for sample_day, hourly in history.items():
            value = hourly.get(hour)
            if value is None:
                continue
            age = (target_day - sample_day).days
            recency_weight = max(1.0, 15.0 - age)
            same_weekday = sample_day.weekday() == target_day.weekday()
            same_day_type = (sample_day.weekday() >= 5) == (target_day.weekday() >= 5)
            calendar_weight = 2.2 if same_weekday else 1.15 if same_day_type else 0.55
            weighted_samples.append((float(value), recency_weight * calendar_weight))
        forecast = (
            sum(value * weight for value, weight in weighted_samples)
            / sum(weight for _, weight in weighted_samples)
            if weighted_samples else None
        )
        if forecast is not None:
            forecast = round(forecast, 2)
        lock_meta = _consumption_forecast_lock_meta(
            target_day,
            hour,
            now_dt=now_dt,
        )
        if forecast is not None and lock_meta["locked"]:
            with CONSUMPTION_FORECAST_LOCKED_ROWS_LOCK:
                day_locks = CONSUMPTION_FORECAST_LOCKED_ROWS.setdefault(
                    target_day.isoformat(),
                    {},
                )
                locked_row = day_locks.get(hour)
                if locked_row and locked_row.get("forecast") is not None:
                    forecast = locked_row["forecast"]
                else:
                    day_locks[hour] = {
                        "forecast": forecast,
                        "sampleCount": len(weighted_samples),
                        "frozenAt": lock_meta["finalUpdateAt"],
                    }
        if forecast is not None:
            forecast_values.append(forecast)
        actual_value = actual_by_hour.get(hour)
        error = (
            float(actual_value) - forecast
            if actual_value is not None and forecast is not None
            else None
        )
        if error is not None:
            absolute_errors.append(abs(error))
            if actual_value:
                percentage_errors.append(abs(error) / abs(float(actual_value)) * 100)
        rows.append(
            {
                "hour": hour,
                "time": f"{hour:02d}:00",
                "forecast": forecast,
                "actual": actual_value,
                "difference": error,
                "sampleCount": len(weighted_samples),
                "locked": lock_meta["locked"],
                "status": lock_meta["status"],
                "hourStartsAt": lock_meta["hourStartsAt"],
                "finalUpdateAt": lock_meta["finalUpdateAt"],
            }
        )
        lock_meta_rows.append(lock_meta)

    peak_row = max(
        (row for row in rows if row["forecast"] is not None),
        key=lambda row: row["forecast"],
        default={},
    )
    training_days = len(history)
    confidence = "yüksek" if training_days >= 10 else "orta" if training_days >= 5 else "düşük"
    next_update_at = next(
        (
            meta["finalUpdateAt"]
            for meta in lock_meta_rows
            if not meta["locked"]
        ),
        None,
    )
    payload = {
        "baseDate": base_day.isoformat(),
        "date": target_day.isoformat(),
        "rows": rows,
        "summary": {
            "average": (
                sum(forecast_values) / len(forecast_values)
                if forecast_values else None
            ),
            "maximum": peak_row.get("forecast"),
            "maximumHour": peak_row.get("time"),
            "forecastHours": len(forecast_values),
            "trainingDays": training_days,
            "actualHours": sum(
                1 for value in actual_by_hour.values() if value is not None
            ),
            "meanAbsoluteError": (
                sum(absolute_errors) / len(absolute_errors)
                if absolute_errors else None
            ),
            "meanAbsolutePercentageError": (
                sum(percentage_errors) / len(percentage_errors)
                if percentage_errors else None
            ),
            "comparedHours": len(absolute_errors),
            "confidence": confidence,
            "lockedHours": sum(1 for row in rows if row.get("locked")),
            "nextUpdateAt": next_update_at,
        },
        "source": "EPİAŞ Şeffaflık Platformu",
        "method": "14 günlük, gün tipi ve yakınlık ağırlıklı saat profili",
        "methodNote": (
            "Bu gösterge istatistiksel bir operasyon tahminidir; resmî talep "
            "tahmini veya yatırım tavsiyesi değildir. Her saatlik tahmin, ilgili "
            "saat başlamadan 1 saat 10 dakika önce son kez güncellenir ve sonra "
            "sabit kalır."
        ),
        "updatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "cached": False,
    }
    if forecast_values:
        ttl = 120 if target_day >= today else 21_600
        with CONSUMPTION_FORECAST_CACHE_LOCK:
            CONSUMPTION_FORECAST_CACHE[cache_key] = {
                "payload": payload,
                "expires": time.time() + ttl,
            }
    return payload


def _active_fullness(
    client: Any,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    now = time.time()
    client_key = f"{client.__class__.__module__}.{client.__class__.__qualname__}"
    if not force_refresh:
        with ACTIVE_FULLNESS_CACHE_LOCK:
            cached_entry = ACTIVE_FULLNESS_CACHE.get(client_key)
            if cached_entry and cached_entry["expires"] > now:
                return {**cached_entry["payload"], "cached": True}

    payload = _epias_post_json(
        client,
        "/v1/dams/data/active-fullness",
        {"page": {"number": 1, "size": 500}},
        force_refresh=force_refresh,
    )
    normalized = [
        {
            "dam": item.get("dam") or item.get("damName") or "—",
            "basin": item.get("basin") or item.get("basinName") or "—",
            "activeFullnessAmount": item.get("activeFullnessAmount"),
            "date": item.get("date") or "",
        }
        for item in _items(payload)
    ]
    available_dates = sorted(
        {
            str(row["date"])[:10]
            for row in normalized
            if row.get("date")
        }
    )
    result = {
        "items": normalized,
        "availableDates": available_dates,
        "cached": False,
    }
    with ACTIVE_FULLNESS_CACHE_LOCK:
        ACTIVE_FULLNESS_CACHE[client_key] = {
            "payload": result,
            "expires": time.time() + 300,
        }
    return result


_XLSX_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XLSX_REL_NS = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
)
_XLSX_PACKAGE_REL_NS = (
    "http://schemas.openxmlformats.org/package/2006/relationships"
)


def _xlsx_column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    result = 0
    for character in letters.upper():
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _xlsx_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(node.text or "" for node in item.iter(f"{{{_XLSX_MAIN_NS}}}t"))
        for item in root.iter(f"{{{_XLSX_MAIN_NS}}}si")
    ]


def _xlsx_sheet_path(workbook: zipfile.ZipFile, sheet_name: str) -> str:
    root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
    relationship_id = None
    for sheet in root.iter(f"{{{_XLSX_MAIN_NS}}}sheet"):
        if sheet.get("name") == sheet_name:
            relationship_id = sheet.get(f"{{{_XLSX_REL_NS}}}id")
            break
    if not relationship_id:
        raise ValueError(f"Excel dosyasında '{sheet_name}' sekmesi bulunamadı.")

    relationships = ElementTree.fromstring(
        workbook.read("xl/_rels/workbook.xml.rels")
    )
    for relationship in relationships.iter(
        f"{{{_XLSX_PACKAGE_REL_NS}}}Relationship"
    ):
        if relationship.get("Id") != relationship_id:
            continue
        target = relationship.get("Target") or ""
        if target.startswith("/"):
            return target.lstrip("/")
        return posixpath.normpath(posixpath.join("xl", target))
    raise ValueError(f"'{sheet_name}' sekmesinin dosya ilişkisi bulunamadı.")


def _xlsx_sheet_rows(
    workbook: zipfile.ZipFile,
    sheet_name: str,
    shared_strings: list[str],
) -> dict[int, dict[int, Any]]:
    root = ElementTree.fromstring(
        workbook.read(_xlsx_sheet_path(workbook, sheet_name))
    )
    rows: dict[int, dict[int, Any]] = {}
    for row in root.iter(f"{{{_XLSX_MAIN_NS}}}row"):
        row_number = int(row.get("r") or len(rows) + 1)
        values: dict[int, Any] = {}
        for cell in row.findall(f"{{{_XLSX_MAIN_NS}}}c"):
            column = _xlsx_column_index(cell.get("r") or "")
            cell_type = cell.get("t") or ""
            value_node = cell.find(f"{{{_XLSX_MAIN_NS}}}v")
            raw_value = value_node.text if value_node is not None else None
            if cell_type == "s" and raw_value is not None:
                index = int(raw_value)
                value: Any = (
                    shared_strings[index] if index < len(shared_strings) else ""
                )
            elif cell_type == "inlineStr":
                value = "".join(
                    node.text or ""
                    for node in cell.iter(f"{{{_XLSX_MAIN_NS}}}t")
                )
            elif cell_type in {"str", "e"}:
                value = raw_value or ""
            elif cell_type == "b":
                value = raw_value == "1"
            elif raw_value is None:
                value = None
            else:
                try:
                    value = float(raw_value)
                except ValueError:
                    value = raw_value
            values[column] = value
        rows[row_number] = values
    return rows


def _excel_serial_date(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10]).isoformat()
        except ValueError:
            return None
    try:
        serial = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(serial):
        return None
    return (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()


def _archive_name_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def _load_baraj_archive(path: Path) -> dict[str, Any]:
    """Excel Pivot verisini tarihe göre gruplanmış Baraj kayıtlarına dönüştür."""

    with zipfile.ZipFile(path) as workbook:
        shared_strings = _xlsx_shared_strings(workbook)
        raw_rows = _xlsx_sheet_rows(
            workbook, "Aktif Doluluk", shared_strings
        )
        pivot_rows = _xlsx_sheet_rows(workbook, "Pivot", shared_strings)

    raw_header_row = next(
        (
            row_number
            for row_number, row in raw_rows.items()
            if "Havza" in row.values() and "Baraj" in row.values()
        ),
        None,
    )
    if raw_header_row is None:
        raise ValueError(
            "Aktif Doluluk sekmesinde Havza ve Baraj sütunları bulunamadı."
        )
    raw_headers = {
        str(value).strip(): column
        for column, value in raw_rows[raw_header_row].items()
        if value is not None
    }
    basin_column = raw_headers["Havza"]
    dam_column = raw_headers["Baraj"]
    basin_by_dam: dict[str, str] = {}
    for row_number, row in raw_rows.items():
        if row_number <= raw_header_row:
            continue
        dam = str(row.get(dam_column) or "").strip()
        basin = str(row.get(basin_column) or "").strip()
        if dam and basin:
            basin_by_dam.setdefault(_archive_name_key(dam), basin)

    pivot_header_row = next(
        (
            row_number
            for row_number, row in pivot_rows.items()
            if str(row.get(1) or "").strip() == "Barajlar"
        ),
        None,
    )
    if pivot_header_row is None:
        raise ValueError("Pivot sekmesinde Barajlar başlığı bulunamadı.")
    date_columns = {
        column: selected_date
        for column, value in pivot_rows[pivot_header_row].items()
        if column > 1
        and (selected_date := _excel_serial_date(value)) is not None
    }
    if not date_columns:
        raise ValueError("Pivot sekmesinde tarih sütunu bulunamadı.")

    by_date: dict[str, list[dict[str, Any]]] = {
        selected_date: [] for selected_date in date_columns.values()
    }
    for row_number, row in pivot_rows.items():
        if row_number <= pivot_header_row:
            continue
        dam = str(row.get(1) or "").strip()
        if not dam or dam.casefold() == "genel ortalama":
            continue
        basin = basin_by_dam.get(_archive_name_key(dam), "—")
        for column, selected_date in date_columns.items():
            try:
                fullness = float(row.get(column))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fullness):
                continue
            by_date[selected_date].append(
                {
                    "dam": dam,
                    "basin": basin,
                    "activeFullnessAmount": fullness,
                    "date": f"{selected_date}T00:00:00+03:00",
                    "source": "excel",
                }
            )

    available_dates = sorted(
        selected_date for selected_date, items in by_date.items() if items
    )
    return {
        "byDate": {
            selected_date: by_date[selected_date]
            for selected_date in available_dates
        },
        "availableDates": available_dates,
        "recordCount": sum(len(items) for items in by_date.values()),
        "sourceFile": path.name,
        "sourceSheet": "Pivot",
    }


def _baraj_archive() -> dict[str, Any]:
    if not BARAJ_ARCHIVE_XLSX.is_file():
        return {"byDate": {}, "availableDates": [], "recordCount": 0}
    modified = BARAJ_ARCHIVE_XLSX.stat().st_mtime_ns
    with BARAJ_ARCHIVE_LOCK:
        if BARAJ_ARCHIVE_CACHE["mtime"] == modified:
            return BARAJ_ARCHIVE_CACHE["payload"]
        payload = _load_baraj_archive(BARAJ_ARCHIVE_XLSX)
        BARAJ_ARCHIVE_CACHE.update({"mtime": modified, "payload": payload})
        return payload


def _baraj_data(client: Any, selected_date: str = "") -> dict[str, Any]:
    if selected_date:
        try:
            date.fromisoformat(selected_date)
        except ValueError as exc:
            raise ValueError("Baraj tarihi YYYY-AA-GG biçiminde olmalıdır.") from exc

    archive = _baraj_archive()
    archive_dates = archive.get("availableDates") or []
    if selected_date in archive.get("byDate", {}):
        return {
            "items": archive["byDate"][selected_date],
            "availableDates": archive_dates,
            "archiveDates": archive_dates,
            "selectedDate": selected_date,
            "source": "excel",
            "sourceLabel": "Arşiv",
        }

    live = _active_fullness(client)
    live_dates = live.get("availableDates") or []
    if selected_date and selected_date not in live_dates:
        raise ValueError(f"{selected_date} tarihi için Baraj verisi bulunamadı.")
    live_selected = selected_date or (
        live_dates[-1]
        if live_dates
        else datetime.now(URETIM.TR_TZ).date().isoformat()
    )
    live_items = live.get("items") or []
    if selected_date:
        live_items = [
            item
            for item in live_items
            if str(item.get("date") or "")[:10] == selected_date
        ]
    return {
        "items": live_items,
        "availableDates": sorted(set(archive_dates) | set(live_dates)),
        "archiveDates": archive_dates,
        "selectedDate": live_selected,
        "source": "epias",
        "sourceLabel": "EPİAŞ Şeffaflık Platformu",
    }


_DAM_SORT_LABELS = {
    "fullness-desc": "Doluluk: yüksekten düşüğe",
    "fullness-asc": "Doluluk: düşükten yükseğe",
    "name-asc": "Baraj adı: A-Z",
    "name-desc": "Baraj adı: Z-A",
}
_TURKISH_ALPHABET = {
    character: index
    for index, character in enumerate("abcçdefgğhıijklmnoöprsştuüvyz")
}


def _turkish_sort_key(value: Any) -> tuple[int, ...]:
    text = (
        str(value or "")
        .strip()
        .replace("I", "ı")
        .replace("İ", "i")
        .lower()
    )
    return tuple(
        _TURKISH_ALPHABET.get(character, len(_TURKISH_ALPHABET) + ord(character))
        for character in text
    )


def _fullness_number(item: dict[str, Any]) -> float | None:
    try:
        value = float(item.get("activeFullnessAmount"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _sort_dams(
    items: list[dict[str, Any]], sort_mode: str = "fullness-desc"
) -> list[dict[str, Any]]:
    """Barajları doluluk veya Türkçe ada göre, eksik değerleri sona atarak sırala."""

    mode = sort_mode if sort_mode in _DAM_SORT_LABELS else "fullness-desc"
    copied = list(items)
    if mode in {"name-asc", "name-desc"}:
        return sorted(
            copied,
            key=lambda item: _turkish_sort_key(item.get("dam")),
            reverse=mode == "name-desc",
        )

    valid = [item for item in copied if _fullness_number(item) is not None]
    missing = [item for item in copied if _fullness_number(item) is None]
    valid.sort(
        key=lambda item: (
            _fullness_number(item),
            _turkish_sort_key(item.get("dam")),
        ),
        reverse=mode == "fullness-desc",
    )
    missing.sort(key=lambda item: _turkish_sort_key(item.get("dam")))
    return valid + missing


def _basin_period_comparison(
    points: list[dict[str, Any]],
    days: int,
) -> dict[str, Any]:
    valid: list[tuple[date, float]] = []
    for point in points:
        try:
            point_day = date.fromisoformat(str(point.get("date") or ""))
            average = float(point.get("average"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(average):
            valid.append((point_day, average))
    valid.sort(key=lambda item: item[0])
    empty = {
        "days": days,
        "actualDays": None,
        "baselineDate": None,
        "latestDate": valid[-1][0].isoformat() if valid else None,
        "baselineAverage": None,
        "latestAverage": valid[-1][1] if valid else None,
        "change": None,
        "drop": None,
        "available": False,
    }
    if len(valid) < 2:
        return empty

    latest_day, latest_average = valid[-1]
    target_day = latest_day - timedelta(days=days)
    baseline_day, baseline_average = next(
        (item for item in reversed(valid[:-1]) if item[0] <= target_day),
        valid[0],
    )
    actual_days = (latest_day - baseline_day).days
    if actual_days <= 0:
        return empty
    change = latest_average - baseline_average
    return {
        "days": days,
        "actualDays": actual_days,
        "baselineDate": baseline_day.isoformat(),
        "latestDate": latest_day.isoformat(),
        "baselineAverage": baseline_average,
        "latestAverage": latest_average,
        "change": change,
        "drop": -change,
        "available": True,
    }


def _basin_regime_analysis(
    points: list[dict[str, Any]],
) -> dict[str, Any]:
    """Havza ortalama doluluğuna doğrusal eğilim ve temkinli tahmin uygula."""

    period_comparisons = {
        "weekly": _basin_period_comparison(points, 7),
        "monthly": _basin_period_comparison(points, 30),
    }
    if not points:
        return {
            "regime": "Veri yok",
            "slopePerDay": None,
            "changeFromStart": None,
            "projectedDepletionDate": None,
            "daysRemaining": None,
            "confidence": "hesaplanamadı",
            "rSquared": None,
            "observationCount": 0,
            "coveredDays": 0,
            "projectionStatus": "Havza verisi bulunamadı",
            "periodComparisons": period_comparisons,
        }

    start = date.fromisoformat(points[0]["date"])
    x_values = [
        (date.fromisoformat(point["date"]) - start).days for point in points
    ]
    y_values = [float(point["average"]) for point in points]
    change = y_values[-1] - y_values[0]
    if len(points) < 2 or len(set(x_values)) < 2:
        return {
            "regime": "Yetersiz veri",
            "slopePerDay": None,
            "changeFromStart": change,
            "projectedDepletionDate": None,
            "daysRemaining": None,
            "confidence": "hesaplanamadı",
            "rSquared": None,
            "observationCount": len(points),
            "coveredDays": 0,
            "projectionStatus": "En az 7 yayın ve 14 günlük dönem gerekli",
            "periodComparisons": period_comparisons,
        }

    x_average = sum(x_values) / len(x_values)
    y_average = sum(y_values) / len(y_values)
    denominator = sum((value - x_average) ** 2 for value in x_values)
    slope = (
        sum(
            (x_value - x_average) * (y_value - y_average)
            for x_value, y_value in zip(x_values, y_values)
        )
        / denominator
    )
    intercept = y_average - slope * x_average
    predicted = [intercept + slope * value for value in x_values]
    total_variance = sum((value - y_average) ** 2 for value in y_values)
    residual_variance = sum(
        (actual - estimate) ** 2
        for actual, estimate in zip(y_values, predicted)
    )
    r_squared = (
        max(0.0, min(1.0, 1 - residual_variance / total_variance))
        if total_variance
        else 1.0
    )
    if slope <= -0.03:
        regime = "Azalan rejim"
    elif slope >= 0.03:
        regime = "Yükselen rejim"
    else:
        regime = "Dengeli rejim"
    covered_days = x_values[-1] - x_values[0]
    enough_observations = len(points) >= 7 and covered_days >= 14
    confidence = (
        "yetersiz veri"
        if not enough_observations
        else "yüksek"
        if r_squared >= 0.7
        else "orta"
        if r_squared >= 0.4
        else "düşük"
    )

    depletion_date = None
    days_remaining = None
    projection_status = "Mevcut eğilim azalmıyor"
    if not enough_observations:
        projection_status = "En az 7 yayın ve 14 günlük dönem gerekli"
    elif r_squared < 0.4:
        projection_status = "Doğrusal eğilim güveni yetersiz"
    elif slope < -0.005 and y_values[-1] > 0:
        estimate = math.ceil(y_values[-1] / abs(slope))
        if 0 < estimate <= 3650:
            days_remaining = estimate
            depletion_date = (
                date.fromisoformat(points[-1]["date"]) + timedelta(days=estimate)
            ).isoformat()
            projection_status = "Deneysel doğrusal eğilim tahmini"
        else:
            projection_status = "Doğrusal tahmin 10 yıldan uzun"

    return {
        "regime": regime,
        "slopePerDay": slope,
        "changeFromStart": change,
        "projectedDepletionDate": depletion_date,
        "daysRemaining": days_remaining,
        "confidence": confidence,
        "rSquared": r_squared,
        "observationCount": len(points),
        "coveredDays": covered_days,
        "projectionStatus": projection_status,
        "trendStart": predicted[0],
        "trendEnd": predicted[-1],
        "periodComparisons": period_comparisons,
    }


def _basin_risk_analysis(
    points: list[dict[str, Any]],
    analysis: dict[str, Any],
    *,
    critical_level: float = 30.0,
) -> dict[str, Any]:
    """Doluluk, düşüş hızı ve kritik seviyeye kalan süreyi tek riskte birleştir."""

    period_comparisons = analysis.get("periodComparisons") or {}
    weekly_drop = period_comparisons.get("weekly", {}).get("drop")
    monthly_drop = period_comparisons.get("monthly", {}).get("drop")
    latest = points[-1] if points else {}
    try:
        latest_fullness = float(latest.get("average"))
    except (TypeError, ValueError):
        latest_fullness = None
    try:
        slope = float(analysis.get("slopePerDay"))
    except (TypeError, ValueError):
        slope = None
    if latest_fullness is None or not math.isfinite(latest_fullness):
        return {
            "level": "Hesaplanamadı",
            "score": None,
            "latestFullness": None,
            "dailySlope": slope,
            "weeklyDrop": weekly_drop,
            "monthlyDrop": monthly_drop,
            "criticalLevel": critical_level,
            "daysToCritical": None,
            "criticalDate": None,
            "reason": "Güncel doluluk verisi bulunamadı.",
        }

    days_to_critical = None
    critical_date = None
    latest_date = str(latest.get("date") or "")
    if latest_fullness <= critical_level:
        days_to_critical = 0
        critical_date = latest_date or None
    elif slope is not None and math.isfinite(slope) and slope < -0.005:
        estimate = math.ceil((latest_fullness - critical_level) / abs(slope))
        if 0 < estimate <= 3650 and latest_date:
            days_to_critical = estimate
            critical_date = (
                date.fromisoformat(latest_date) + timedelta(days=estimate)
            ).isoformat()

    fullness_score = max(0.0, min(55.0, (60.0 - latest_fullness) / 60.0 * 55.0))
    decline_score = (
        max(0.0, min(25.0, -slope / 0.15 * 25.0))
        if slope is not None and math.isfinite(slope)
        else 0.0
    )
    if days_to_critical is None:
        horizon_score = 0.0
    elif days_to_critical <= 30:
        horizon_score = 20.0
    elif days_to_critical <= 90:
        horizon_score = 17.0
    elif days_to_critical <= 180:
        horizon_score = 13.0
    elif days_to_critical <= 365:
        horizon_score = 8.0
    else:
        horizon_score = 3.0
    score = round(min(100.0, fullness_score + decline_score + horizon_score), 1)

    if latest_fullness <= critical_level or (
        days_to_critical is not None and days_to_critical <= 90
    ) or score >= 60:
        level = "Yüksek"
    elif latest_fullness <= 50 or (
        days_to_critical is not None and days_to_critical <= 365
    ) or score >= 30:
        level = "Orta"
    else:
        level = "Düşük"

    if latest_fullness <= critical_level:
        reason = f"Doluluk %{critical_level:g} kritik seviyesinin altında."
    elif days_to_critical is not None:
        reason = f"Mevcut eğilimle kritik seviyeye yaklaşık {days_to_critical} gün kaldı."
    elif slope is not None and slope >= -0.005:
        reason = "Mevcut eğilim kritik seviyeye doğru azalmıyor."
    else:
        reason = "Kritik seviyeye varış için yeterli eğilim verisi yok."
    return {
        "level": level,
        "score": score,
        "latestFullness": latest_fullness,
        "dailySlope": slope,
        "weeklyDrop": weekly_drop,
        "monthlyDrop": monthly_drop,
        "criticalLevel": critical_level,
        "daysToCritical": days_to_critical,
        "criticalDate": critical_date,
        "reason": reason,
    }


def _baraj_basin_history(client: Any) -> dict[str, Any]:
    """Excel arşivi ile son EPİAŞ kaydını havza zaman serilerine dönüştür."""

    archive_mtime = (
        BARAJ_ARCHIVE_XLSX.stat().st_mtime_ns
        if BARAJ_ARCHIVE_XLSX.is_file()
        else None
    )
    client_key = f"{client.__class__.__module__}.{client.__class__.__qualname__}"
    now = time.time()
    with BARAJ_BASIN_HISTORY_CACHE_LOCK:
        cached_payload = BARAJ_BASIN_HISTORY_CACHE.get("payload")
        if (
            cached_payload is not None
            and BARAJ_BASIN_HISTORY_CACHE.get("archive_mtime") == archive_mtime
            and BARAJ_BASIN_HISTORY_CACHE.get("client_key") == client_key
            and BARAJ_BASIN_HISTORY_CACHE.get("expires", 0.0) > now
        ):
            return {**cached_payload, "cached": True}

    archive = _baraj_archive()
    rows_by_date: dict[str, dict[str, dict[str, Any]]] = {}
    for selected_date, items in (archive.get("byDate") or {}).items():
        rows_by_date[selected_date] = {
            _archive_name_key(item.get("dam")): dict(item) for item in items
        }

    archive_dates = set(rows_by_date)
    live = _active_fullness(client)
    for item in live.get("items") or []:
        selected_date = str(item.get("date") or "")[:10]
        try:
            date.fromisoformat(selected_date)
        except ValueError:
            continue
        # Excel'de bulunan bir günün tamamı arşiv kabul edilir. Aynı güne ait
        # EPİAŞ satırları, Excel değerlerini veya kaynak etiketini değiştiremez.
        if selected_date in archive_dates:
            continue
        rows_by_date.setdefault(selected_date, {})[
            _archive_name_key(item.get("dam"))
        ] = dict(item)

    basin_values: dict[str, dict[str, list[float]]] = {}
    basin_dams: dict[str, dict[str, set[str]]] = {}
    basin_dam_history: dict[str, dict[str, dict[str, Any]]] = {}
    for selected_date, row_map in rows_by_date.items():
        for item in row_map.values():
            basin = str(item.get("basin") or "").strip()
            dam = str(item.get("dam") or "").strip()
            fullness = _fullness_number(item)
            if not basin or basin == "—" or not dam or fullness is None:
                continue
            basin_values.setdefault(basin, {}).setdefault(selected_date, []).append(
                fullness
            )
            basin_dams.setdefault(basin, {}).setdefault(selected_date, set()).add(
                dam
            )
            dam_entry = basin_dam_history.setdefault(basin, {}).setdefault(
                _archive_name_key(dam),
                {"name": dam, "points": []},
            )
            dam_entry["points"].append(
                {
                    "date": selected_date,
                    "activeFullnessAmount": fullness,
                    "source": (
                        "Arşiv" if item.get("source") == "excel" else "EPİAŞ"
                    ),
                }
            )

    basins: list[dict[str, Any]] = []
    for basin in sorted(basin_values, key=_turkish_sort_key):
        points = []
        for selected_date in sorted(basin_values[basin]):
            values = basin_values[basin][selected_date]
            points.append(
                {
                    "date": selected_date,
                    "average": sum(values) / len(values),
                    "minimum": min(values),
                    "maximum": max(values),
                    "damCount": len(basin_dams[basin][selected_date]),
                }
            )
        analysis = _basin_regime_analysis(points)
        basins.append(
            {
                "name": basin,
                "points": points,
                "analysis": analysis,
                "risk": _basin_risk_analysis(points, analysis),
                "dams": [
                    {
                        **entry,
                        "points": sorted(
                            entry["points"],
                            key=lambda point: point["date"],
                        ),
                    }
                    for entry in sorted(
                        basin_dam_history.get(basin, {}).values(),
                        key=lambda item: _turkish_sort_key(item["name"]),
                    )
                ],
            }
        )

    all_dates = sorted(rows_by_date)
    result = {
        "startDate": all_dates[0] if all_dates else None,
        "endDate": all_dates[-1] if all_dates else None,
        "basins": basins,
        "aggregationMethod": "unweighted-arithmetic-mean",
        "methodNote": (
            "Havza değeri, o tarihte veri bulunan barajların aktif doluluk "
            "yüzdelerinin kapasiteyle ağırlıklandırılmamış basit aritmetik "
            "ortalamasıdır. Deneysel tükenme tahmini yalnızca en az 7 yayın, "
            "14 gün ve yeterli doğrusal eğilim uyumu varsa gösterilir; yağış, su "
            "girişi, üretim programı ve baraj hacim farklarını içermez."
        ),
    }
    with BARAJ_BASIN_HISTORY_CACHE_LOCK:
        BARAJ_BASIN_HISTORY_CACHE.update(
            {
                "archive_mtime": archive_mtime,
                "client_key": client_key,
                "payload": result,
                "expires": time.time() + 300,
            }
        )
    return result


def _baraj_basin_xlsx(payload: dict[str, Any], basin_name: str) -> bytes:
    """Seçili havzanın ortalama ve baraj bazlı geçmişini XLSX'e aktar."""

    selected = next(
        (
            basin
            for basin in payload.get("basins") or []
            if basin.get("name") == basin_name
        ),
        None,
    )
    if selected is None:
        raise ValueError("Geçerli bir havza seçin.")

    dams = selected.get("dams") or []
    points = selected.get("points") or []
    analysis = selected.get("analysis") or {}
    detail_rows: list[list[tuple[Any, int]]] = [
        [
            ("Tarih", 1),
            ("Baraj", 1),
            ("Aktif doluluk (%)", 1),
            ("Veri kaynağı", 1),
        ]
    ]
    detail_items = sorted(
        (
            {
                "date": point.get("date") or "",
                "dam": dam.get("name") or "—",
                "fullness": point.get("activeFullnessAmount"),
                "source": point.get("source") or "—",
            }
            for dam in dams
            for point in (dam.get("points") or [])
        ),
        key=lambda item: (item["date"], _turkish_sort_key(item["dam"])),
    )
    detail_rows.extend(
        [
            (item["date"], 0),
            (item["dam"], 0),
            (item["fullness"], 2),
            (item["source"], 0),
        ]
        for item in detail_items
    )

    average_rows: list[list[tuple[Any, int]]] = [
        [
            ("Tarih", 1),
            ("Havza ortalaması (%)", 1),
            ("En düşük (%)", 1),
            ("En yüksek (%)", 1),
            ("Baraj sayısı", 1),
        ]
    ]
    average_rows.extend(
        [
            (point.get("date") or "", 0),
            (point.get("average"), 2),
            (point.get("minimum"), 2),
            (point.get("maximum"), 2),
            (point.get("damCount"), 0),
        ]
        for point in points
    )

    latest = points[-1] if points else {}
    period_comparisons = analysis.get("periodComparisons") or {}
    weekly = period_comparisons.get("weekly") or {}
    monthly = period_comparisons.get("monthly") or {}
    summary_rows = [
        [("Baha Enerji — Havza Baraj Doluluk Raporu", 4), (None, 0)],
        [("Havza", 1), (basin_name, 0)],
        [("Dönem başlangıcı", 1), (payload.get("startDate") or "—", 0)],
        [("Dönem sonu", 1), (payload.get("endDate") or "—", 0)],
        [("Baraj sayısı", 1), (len(dams), 0)],
        [("Toplam baraj kaydı", 1), (len(detail_items), 0)],
        [
            ("Havza hesaplama yöntemi", 1),
            ("Kapasiteyle ağırlıklandırılmamış basit ortalama", 0),
        ],
        [("Son havza ortalaması (%)", 1), (latest.get("average"), 2)],
        [("Rejim", 1), (analysis.get("regime") or "—", 0)],
        [("Günlük eğilim (% puan)", 1), (analysis.get("slopePerDay"), 2)],
        [
            ("Haftalık düşüş (% puan)", 1),
            (weekly.get("drop") if weekly.get("available") else None, 2),
        ],
        [
            ("Haftalık kıyas tarihi", 1),
            (weekly.get("baselineDate") or "—", 0),
        ],
        [
            ("Aylık düşüş (% puan)", 1),
            (monthly.get("drop") if monthly.get("available") else None, 2),
        ],
        [
            ("Aylık kıyas tarihi", 1),
            (monthly.get("baselineDate") or "—", 0),
        ],
        [
            ("Tahmini tükenme tarihi", 1),
            (analysis.get("projectedDepletionDate") or "Öngörülmüyor", 0),
        ],
    ]
    return _xlsx_workbook(
        (
            ("Özet", URETIM._xlsx_sheet(summary_rows, widths=[33, 34])),
            (
                "Baraj Dolulukları",
                URETIM._xlsx_sheet(
                    detail_rows,
                    widths=[18, 32, 23, 20],
                    freeze_row=1,
                    auto_filter=True,
                ),
            ),
            (
                "Havza Ortalaması",
                URETIM._xlsx_sheet(
                    average_rows,
                    widths=[18, 27, 20, 20, 18],
                    freeze_row=1,
                    auto_filter=True,
                ),
            ),
        )
    )


def _xlsx_workbook(sheets: tuple[tuple[str, str], ...]) -> bytes:
    """Hazır çalışma sayfası XML'lerini bağımlılıksız bir XLSX paketine dönüştür."""

    sheet_count = len(sheets)
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
            for index in range(1, sheet_count + 1)
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
        + "".join(
            (
                f'<sheet name="{xml_escape(name)}" '
                f'sheetId="{index}" r:id="rId{index}"/>'
            )
            for index, (name, _) in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            (
                f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{index}.xml"/>'
            )
            for index in range(1, sheet_count + 1)
        )
        + (
            f'<Relationship Id="rId{sheet_count + 1}" '
            'Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        )
        + "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
        '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FF0B1D39"/><sz val="15"/><name val="Calibri"/></font>'
        '</fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2D70EE"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" '
        'applyFont="1"><alignment horizontal="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" '
        'applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" '
        'xfId="0"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" '
        'applyFont="1"/></cellXfs><cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_relationships)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr(
            "xl/_rels/workbook.xml.rels", workbook_relationships
        )
        workbook_zip.writestr("xl/styles.xml", styles)
        for index, (_, sheet) in enumerate(sheets, start=1):
            workbook_zip.writestr(f"xl/worksheets/sheet{index}.xml", sheet)
    return output.getvalue()


def _baraj_xlsx(
    payload: dict[str, Any], sort_mode: str = "fullness-desc"
) -> bytes:
    """Baraj özeti ve sıralanmış baraj listesini içeren XLSX raporu üret."""

    mode = sort_mode if sort_mode in _DAM_SORT_LABELS else "fullness-desc"
    items = _sort_dams(payload.get("items") or [], mode)
    valid = [
        (_fullness_number(item), item)
        for item in items
        if _fullness_number(item) is not None
    ]
    values = [value for value, _ in valid]
    highest = max(valid, default=(None, None), key=lambda entry: entry[0] or 0)
    lowest = min(valid, default=(None, None), key=lambda entry: entry[0] or 0)
    data_date = (
        payload.get("selectedDate")
        or (payload.get("availableDates") or [""])[-1]
        or datetime.now(URETIM.TR_TZ).date().isoformat()
    )
    summary_rows = [
        [("Baha Enerji — Baraj Aktif Doluluk Raporu", 4), (None, 0)],
        [("Veri tarihi", 1), (data_date, 0)],
        [("Veri kaynağı", 1), (payload.get("sourceLabel") or "EPİAŞ", 0)],
        [("Sıralama", 1), (_DAM_SORT_LABELS[mode], 0)],
        [("Gösterge", 1), ("Değer", 1)],
        [("Toplam baraj", 0), (len(items), 0)],
        [("Ortalama doluluk (%)", 0), (sum(values) / len(values) if values else None, 2)],
        [
            ("En yüksek doluluk", 0),
            (
                (
                    f"{highest[1].get('dam')} · %{highest[0]:.2f}"
                    if highest[1] is not None
                    else ""
                ),
                0,
            ),
        ],
        [
            ("En düşük doluluk", 0),
            (
                (
                    f"{lowest[1].get('dam')} · %{lowest[0]:.2f}"
                    if lowest[1] is not None
                    else ""
                ),
                0,
            ),
        ],
    ]
    list_rows = [
        [
            ("Sıra", 1),
            ("Baraj", 1),
            ("Havza", 1),
            ("Aktif doluluk (%)", 1),
            ("Veri tarihi", 1),
        ]
    ]
    list_rows.extend(
        [
            (index, 0),
            (item.get("dam") or "—", 0),
            (item.get("basin") or "—", 0),
            (_fullness_number(item), 2),
            (str(item.get("date") or "")[:10], 0),
        ]
        for index, item in enumerate(items, start=1)
    )
    return _xlsx_workbook(
        (
            ("Özet", URETIM._xlsx_sheet(summary_rows, widths=[31, 35])),
            (
                "Baraj Listesi",
                URETIM._xlsx_sheet(
                    list_rows,
                    widths=[9, 31, 31, 22, 18],
                    freeze_row=1,
                    auto_filter=True,
                ),
            ),
        )
    )


def _market_xlsx(dashboard: dict[str, Any]) -> bytes:
    """Piyasa paneli için harici paketsiz, geçerli bir XLSX raporu üret."""

    summary = dashboard["summary"]
    ptf_averages = {
        "TRY": summary.get("ptfAverage"),
        **(summary.get("ptfAverageByCurrency") or {}),
    }
    summary_rows = [
        [("Baha Enerji — Günlük Piyasa Raporu", 4), (None, 0)],
        [("Tarih", 1), (dashboard["date"], 0)],
        [("Gösterge", 1), ("Değer", 1)],
        [("PTF ortalama (TL/MWh)", 0), (ptf_averages.get("TRY"), 2)],
        [("PTF ortalama (EUR/MWh)", 0), (ptf_averages.get("EUR"), 2)],
        [("PTF ortalama (USD/MWh)", 0), (ptf_averages.get("USD"), 2)],
        [("SMF ortalama (TL/MWh)", 0), (summary.get("smfAverage"), 2)],
        [("Toplam YAL (MWh)", 0), (summary.get("yalTotal"), 2)],
        [("Toplam YAT (MWh)", 0), (summary.get("yatTotal"), 2)],
    ]
    hourly_rows = [
        [
            ("Tarih", 1),
            ("Saat", 1),
            ("PTF (TL/MWh)", 1),
            ("PTF (EUR/MWh)", 1),
            ("PTF (USD/MWh)", 1),
            ("SMF (TL/MWh)", 1),
            ("YAL (MWh)", 1),
            ("YAT (MWh)", 1),
            ("Sistem Yönü", 1),
        ]
    ]
    hourly_rows.extend(
        [
            (dashboard["date"], 0),
            (row.get("time"), 0),
            (row.get("ptf"), 2),
            ((row.get("ptfByCurrency") or {}).get("EUR"), 2),
            ((row.get("ptfByCurrency") or {}).get("USD"), 2),
            (row.get("smf"), 2),
            (row.get("yal"), 2),
            (
                abs(row["yat"]) if row.get("yat") is not None else None,
                2,
            ),
            (row.get("direction") or "", 0),
        ]
        for row in dashboard["rows"]
    )
    sheets = (
        URETIM._xlsx_sheet(summary_rows, widths=[32, 24]),
        URETIM._xlsx_sheet(
            hourly_rows,
            widths=[14, 10, 18, 18, 18, 18, 16, 16, 24],
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
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
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
        '<sheets><sheet name="Özet" sheetId="1" r:id="rId1"/>'
        '<sheet name="Saatlik Veri" sheetId="2" r:id="rId2"/></sheets></workbook>'
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
        '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><color rgb="FF0B1D39"/><sz val="15"/><name val="Calibri"/></font>'
        '</fonts><fills count="3"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF2D70EE"/>'
        '<bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" '
        'applyFont="1"><alignment horizontal="center"/></xf>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" '
        'applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" '
        'xfId="0"/><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" '
        'applyFont="1"/></cellXfs><cellStyles count="1">'
        '<cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as workbook_zip:
        workbook_zip.writestr("[Content_Types].xml", content_types)
        workbook_zip.writestr("_rels/.rels", root_relationships)
        workbook_zip.writestr("xl/workbook.xml", workbook)
        workbook_zip.writestr(
            "xl/_rels/workbook.xml.rels", workbook_relationships
        )
        workbook_zip.writestr("xl/styles.xml", styles)
        for index, sheet in enumerate(sheets, start=1):
            workbook_zip.writestr(
                f"xl/worksheets/sheet{index}.xml", sheet
            )
    return output.getvalue()


def _consumption_xlsx(dashboard: dict[str, Any]) -> bytes:
    """Gerçek zamanlı tüketim özeti ve saatlik değerleri için XLSX üret."""

    summary = dashboard.get("summary") or {}
    summary_rows = [
        [("Baha Enerji — Gerçek Zamanlı Tüketim Raporu", 4), (None, 0)],
        [("Tarih", 1), (dashboard.get("date") or "—", 0)],
        [("Veri kaynağı", 1), (dashboard.get("source") or "EPİAŞ", 0)],
        [("Gösterge", 1), ("Değer", 1)],
        [("Son tüketim (MWh)", 0), (summary.get("latest"), 2)],
        [("Son veri saati", 0), (summary.get("latestHour") or "—", 0)],
        [("Ortalama tüketim (MWh)", 0), (summary.get("average"), 2)],
        [("En yüksek tüketim (MWh)", 0), (summary.get("maximum"), 2)],
        [("En yüksek saat", 0), (summary.get("maximumHour") or "—", 0)],
        [("En düşük tüketim (MWh)", 0), (summary.get("minimum"), 2)],
        [("En düşük saat", 0), (summary.get("minimumHour") or "—", 0)],
        [("Toplam tüketim (MWh)", 0), (summary.get("total"), 2)],
        [("Yayımlanan saat", 0), (summary.get("availableHours"), 0)],
    ]
    hourly_rows = [[("Tarih", 1), ("Saat", 1), ("Tüketim (MWh)", 1), ("Durum", 1)]]
    hourly_rows.extend(
        [
            (dashboard.get("date") or "", 0),
            (row.get("time") or "", 0),
            (row.get("consumption"), 2),
            ("Yayımlandı" if row.get("consumption") is not None else "Veri bekleniyor", 0),
        ]
        for row in dashboard.get("rows") or []
    )
    return _xlsx_workbook(
        (
            ("Özet", URETIM._xlsx_sheet(summary_rows, widths=[34, 31])),
            (
                "Saatlik Tüketim",
                URETIM._xlsx_sheet(
                    hourly_rows,
                    widths=[15, 12, 23, 22],
                    freeze_row=1,
                    auto_filter=True,
                ),
            ),
        )
    )


ExportSections = list[tuple[str, list[list[Any]]]]


def _export_value(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Evet" if value else "Hayır"
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return "—"
        if isinstance(value, int) or float(value).is_integer():
            return f"{int(value):,}".replace(",", ".")
        return _tr_report_number(value, digits)
    return str(value)


def _export_sections_csv(sections: ExportSections) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    for index, (title, rows) in enumerate(sections):
        if index:
            writer.writerow([])
        if title:
            writer.writerow([title])
        for row in rows:
            writer.writerow([_export_value(cell) for cell in row])
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _pdf_text(text: Any) -> str:
    value = _export_value(text).replace("\n", " ").replace("\r", " ")
    return f"<FEFF{value.encode('utf-16-be', 'replace').hex().upper()}>"


def _pdf_line(row: list[Any]) -> str:
    line = "  |  ".join(_export_value(cell) for cell in row)
    return line if len(line) <= 155 else f"{line[:152]}..."


def _export_sections_pdf(title: str, sections: ExportSections) -> bytes:
    lines: list[tuple[str, int]] = [(title, 16)]
    for section_title, rows in sections:
        lines.append(("", 10))
        lines.append((section_title, 12))
        for row in rows:
            lines.append((_pdf_line(row), 9))

    pages: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    for line in lines:
        current.append(line)
        if len(current) >= 42:
            pages.append(current)
            current = []
    if current or not pages:
        pages.append(current)

    page_streams: list[bytes] = []
    for page in pages:
        content_lines: list[str] = []
        y = 552
        for text, size in page:
            if text:
                content_lines.append(
                    f"BT /F1 {size} Tf 42 {y} Td {_pdf_text(text)} Tj ET"
                )
            y -= 12 if size <= 10 else 16
        page_streams.append("\n".join(content_lines).encode("ascii"))

    page_count = len(page_streams)
    max_object_id = 3 + page_count * 2
    page_ids = [4 + index * 2 for index in range(page_count)]
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{page_id} 0 R' for page_id in page_ids)}] "
            f"/Count {page_count} >>"
        ).encode("ascii"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, stream in enumerate(page_streams):
        page_id = page_ids[index]
        content_id = page_id + 1
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_object_id + 1)
    for object_id in range(1, max_object_id + 1):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {max_object_id + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_object_id + 1):
        output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {max_object_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def _market_export_sections(dashboard: dict[str, Any]) -> ExportSections:
    summary = dashboard.get("summary") or {}
    date_value = dashboard.get("date") or "—"
    ptf_averages = {
        "TRY": summary.get("ptfAverage"),
        **(summary.get("ptfAverageByCurrency") or {}),
    }
    return [
        (
            "Özet",
            [
                ["Tarih", date_value],
                ["PTF ortalama (TL/MWh)", ptf_averages.get("TRY")],
                ["PTF ortalama (EUR/MWh)", ptf_averages.get("EUR")],
                ["PTF ortalama (USD/MWh)", ptf_averages.get("USD")],
                ["SMF ortalama (TL/MWh)", summary.get("smfAverage")],
                ["Toplam YAL (MWh)", summary.get("yalTotal")],
                ["Toplam YAT (MWh)", summary.get("yatTotal")],
            ],
        ),
        (
            "Saatlik veri",
            [
                [
                    "Tarih",
                    "Saat",
                    "PTF (TL/MWh)",
                    "PTF (EUR/MWh)",
                    "PTF (USD/MWh)",
                    "SMF (TL/MWh)",
                    "YAL (MWh)",
                    "YAT (MWh)",
                    "Sistem Yönü",
                ],
                *[
                    [
                        date_value,
                        row.get("time"),
                        row.get("ptf"),
                        (row.get("ptfByCurrency") or {}).get("EUR"),
                        (row.get("ptfByCurrency") or {}).get("USD"),
                        row.get("smf"),
                        row.get("yal"),
                        abs(row["yat"]) if row.get("yat") is not None else None,
                        row.get("direction") or "",
                    ]
                    for row in dashboard.get("rows") or []
                ],
            ],
        ),
    ]


def _consumption_export_sections(dashboard: dict[str, Any]) -> ExportSections:
    summary = dashboard.get("summary") or {}
    date_value = dashboard.get("date") or "—"
    return [
        (
            "Özet",
            [
                ["Tarih", date_value],
                ["Veri kaynağı", dashboard.get("source") or "EPİAŞ"],
                ["Son tüketim (MWh)", summary.get("latest")],
                ["Son veri saati", summary.get("latestHour") or "—"],
                ["Ortalama tüketim (MWh)", summary.get("average")],
                ["En yüksek tüketim (MWh)", summary.get("maximum")],
                ["En yüksek saat", summary.get("maximumHour") or "—"],
                ["En düşük tüketim (MWh)", summary.get("minimum")],
                ["En düşük saat", summary.get("minimumHour") or "—"],
                ["Toplam tüketim (MWh)", summary.get("total")],
                ["Yayımlanan saat", summary.get("availableHours")],
            ],
        ),
        (
            "Saatlik tüketim",
            [
                ["Tarih", "Saat", "Tüketim (MWh)", "Durum"],
                *[
                    [
                        date_value,
                        row.get("time") or "",
                        row.get("consumption"),
                        "Yayımlandı"
                        if row.get("consumption") is not None
                        else "Veri bekleniyor",
                    ]
                    for row in dashboard.get("rows") or []
                ],
            ],
        ),
    ]


def _baraj_export_sections(
    payload: dict[str, Any], sort_mode: str = "fullness-desc"
) -> ExportSections:
    mode = sort_mode if sort_mode in _DAM_SORT_LABELS else "fullness-desc"
    items = _sort_dams(payload.get("items") or [], mode)
    valid = [
        (_fullness_number(item), item)
        for item in items
        if _fullness_number(item) is not None
    ]
    values = [value for value, _ in valid]
    highest = max(valid, default=(None, None), key=lambda entry: entry[0] or 0)
    lowest = min(valid, default=(None, None), key=lambda entry: entry[0] or 0)
    data_date = (
        payload.get("selectedDate")
        or (payload.get("availableDates") or [""])[-1]
        or datetime.now(URETIM.TR_TZ).date().isoformat()
    )
    return [
        (
            "Özet",
            [
                ["Veri tarihi", data_date],
                ["Veri kaynağı", payload.get("sourceLabel") or "EPİAŞ"],
                ["Sıralama", _DAM_SORT_LABELS[mode]],
                ["Toplam baraj", len(items)],
                [
                    "Ortalama doluluk (%)",
                    sum(values) / len(values) if values else None,
                ],
                [
                    "En yüksek doluluk",
                    (
                        f"{highest[1].get('dam')} · %{_tr_report_number(highest[0])}"
                        if highest[1] is not None
                        else "—"
                    ),
                ],
                [
                    "En düşük doluluk",
                    (
                        f"{lowest[1].get('dam')} · %{_tr_report_number(lowest[0])}"
                        if lowest[1] is not None
                        else "—"
                    ),
                ],
            ],
        ),
        (
            "Baraj listesi",
            [
                ["Sıra", "Baraj", "Havza", "Aktif doluluk (%)", "Veri tarihi"],
                *[
                    [
                        index,
                        item.get("dam") or "—",
                        item.get("basin") or "—",
                        _fullness_number(item),
                        str(item.get("date") or "")[:10],
                    ]
                    for index, item in enumerate(items, start=1)
                ],
            ],
        ),
    ]


def _baraj_basin_export_sections(
    payload: dict[str, Any], basin_name: str
) -> ExportSections:
    selected = next(
        (
            basin
            for basin in payload.get("basins") or []
            if basin.get("name") == basin_name
        ),
        None,
    )
    if selected is None:
        raise ValueError("Geçerli bir havza seçin.")

    dams = selected.get("dams") or []
    points = selected.get("points") or []
    analysis = selected.get("analysis") or {}
    detail_items = sorted(
        (
            {
                "date": point.get("date") or "",
                "dam": dam.get("name") or "—",
                "fullness": point.get("activeFullnessAmount"),
                "source": point.get("source") or "—",
            }
            for dam in dams
            for point in (dam.get("points") or [])
        ),
        key=lambda item: (item["date"], _turkish_sort_key(item["dam"])),
    )
    period_comparisons = analysis.get("periodComparisons") or {}
    weekly = period_comparisons.get("weekly") or {}
    monthly = period_comparisons.get("monthly") or {}
    latest = points[-1] if points else {}
    return [
        (
            "Özet",
            [
                ["Havza", basin_name],
                ["Dönem başlangıcı", payload.get("startDate") or "—"],
                ["Dönem sonu", payload.get("endDate") or "—"],
                ["Baraj sayısı", len(dams)],
                ["Toplam baraj kaydı", len(detail_items)],
                ["Son havza ortalaması (%)", latest.get("average")],
                ["Rejim", analysis.get("regime") or "—"],
                ["Günlük eğilim (% puan)", analysis.get("slopePerDay")],
                [
                    "Haftalık düşüş (% puan)",
                    weekly.get("drop") if weekly.get("available") else None,
                ],
                ["Haftalık kıyas tarihi", weekly.get("baselineDate") or "—"],
                [
                    "Aylık düşüş (% puan)",
                    monthly.get("drop") if monthly.get("available") else None,
                ],
                ["Aylık kıyas tarihi", monthly.get("baselineDate") or "—"],
                [
                    "Tahmini tükenme tarihi",
                    analysis.get("projectedDepletionDate") or "Öngörülmüyor",
                ],
            ],
        ),
        (
            "Baraj dolulukları",
            [
                ["Tarih", "Baraj", "Aktif doluluk (%)", "Veri kaynağı"],
                *[
                    [
                        item["date"],
                        item["dam"],
                        item["fullness"],
                        item["source"],
                    ]
                    for item in detail_items
                ],
            ],
        ),
        (
            "Havza ortalaması",
            [
                [
                    "Tarih",
                    "Havza ortalaması (%)",
                    "En düşük (%)",
                    "En yüksek (%)",
                    "Baraj sayısı",
                ],
                *[
                    [
                        point.get("date") or "",
                        point.get("average"),
                        point.get("minimum"),
                        point.get("maximum"),
                        point.get("damCount"),
                    ]
                    for point in points
                ],
            ],
        ),
    ]


def _uretim_export_sections(dashboard: dict[str, Any]) -> ExportSections:
    period = dashboard.get("period") or {}
    summary = dashboard.get("summary") or {}
    group_labels = {
        "renewable": "Yenilenebilir",
        "thermal": "Termik",
        "natural_gas": "Doğal gaz",
        "other": "Diğer / Uluslararası",
    }
    return [
        (
            "Özet",
            [
                ["Başlangıç", period.get("start")],
                ["Bitiş", period.get("end")],
                ["Kapsanan saat", period.get("hours")],
                ["UEVM saati", period.get("uevmHours")],
                ["UEÇM saati", period.get("uecmHours")],
                ["Karşılaştırılabilir saat", period.get("comparableHours")],
                ["Toplam UEVM (MWh)", summary.get("uevmTotal")],
                ["Toplam UEÇM (MWh)", summary.get("uecmTotal")],
                ["UEVM − UEÇM farkı (MWh)", summary.get("difference")],
                ["Yüzdesel sapma", summary.get("deviationPct")],
                ["Saatlik ortalama UEVM (MWh)", summary.get("hourlyAverage")],
            ],
        ),
        (
            "Kaynak kırılımı",
            [
                ["Grup", "Kaynak", "UEVM (MWh)", "Pay (%)"],
                *[
                    [
                        group_labels.get(source.get("group"), source.get("group")),
                        source.get("label"),
                        source.get("value"),
                        source.get("share"),
                    ]
                    for source in dashboard.get("sources") or []
                ],
            ],
        ),
        (
            "Saatlik veri",
            [
                [
                    "Tarih / saat",
                    "UEVM (MWh)",
                    "UEÇM (MWh)",
                    "Yenilenebilir (MWh)",
                    "Güneş (MWh)",
                    "Rüzgâr (MWh)",
                    "Hidroelektrik (MWh)",
                    "Termik (MWh)",
                    "Doğal gaz (MWh)",
                ],
                *[
                    [
                        row.get("timestamp"),
                        row.get("uevm"),
                        row.get("uecm"),
                        row.get("renewable"),
                        row.get("sun"),
                        row.get("wind"),
                        row.get("hydro"),
                        row.get("thermal"),
                        row.get("naturalGas"),
                    ]
                    for row in dashboard.get("series") or []
                ],
            ],
        ),
    ]


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_weekday_previous_iso_year(selected_day: date) -> date:
    """Return the same ISO week + weekday in the previous ISO year."""

    iso_year, iso_week, iso_weekday = selected_day.isocalendar()
    target_year = iso_year - 1
    week = iso_week
    while week >= 1:
        try:
            return date.fromisocalendar(target_year, week, iso_weekday)
        except ValueError:
            week -= 1
    return selected_day - timedelta(days=364)


def _executive_comparison_specs(selected_day: date) -> tuple[dict[str, Any], ...]:
    return (
        {
            "key": "selected",
            "label": "Seçili gün",
            "date": selected_day,
            "note": "Rapor tarihi",
        },
        {
            "key": "yesterday",
            "label": "Dün",
            "date": selected_day - timedelta(days=1),
            "note": "Bir önceki takvim günü",
        },
        {
            "key": "lastWeekSameDay",
            "label": "Geçen hafta aynı gün",
            "date": selected_day - timedelta(days=7),
            "note": "7 gün önceki aynı hafta günü",
        },
        {
            "key": "lastYearSameWeekday",
            "label": "Geçen yıl aynı hafta-gün",
            "date": _same_weekday_previous_iso_year(selected_day),
            "note": "Aynı ISO hafta ve aynı hafta günü",
        },
    )


def _dam_average_from_items(items: list[dict[str, Any]]) -> float | None:
    values = [
        value
        for item in items
        if (value := _fullness_number(item)) is not None
    ]
    return sum(values) / len(values) if values else None


def _executive_metric_snapshot(
    spec: dict[str, Any],
    client: Any,
    *,
    modules: dict[str, Any] | None = None,
    dam_summary: dict[str, Any] | None = None,
    module_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a compact metric snapshot for calendar comparison rows."""

    snapshot_day: date = spec["date"]
    selected_date = snapshot_day.isoformat()
    loaded_modules: dict[str, Any] = modules if modules is not None else {}
    errors: dict[str, str] = dict(module_errors or {})

    if modules is None:
        def collect(name: str, loader: Any) -> None:
            try:
                loaded_modules[name] = loader()
            except URETIM.EpiasError as exc:
                if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                    raise
                errors[name] = str(exc)
                loaded_modules[name] = None
            except ValueError as exc:
                errors[name] = str(exc)
                loaded_modules[name] = None
            except Exception:
                errors[name] = "Bu veri grubu hazırlanamadı."
                loaded_modules[name] = None

        collect("market", lambda: _market_dashboard(selected_date, client))
        collect("dams", lambda: _baraj_data(client, selected_date))
        collect(
            "production",
            lambda: URETIM_SERVICE.dashboard(
                URETIM.DateRange(start=snapshot_day, end=snapshot_day),
                client=client,
            ),
        )
        collect("consumption", lambda: _consumption_dashboard(selected_date, client))

    market_summary = (loaded_modules.get("market") or {}).get("summary") or {}
    production_summary = (loaded_modules.get("production") or {}).get("summary") or {}
    consumption_summary = (loaded_modules.get("consumption") or {}).get("summary") or {}
    dams = loaded_modules.get("dams") or {}
    dam_average = (
        dam_summary.get("average")
        if dam_summary is not None
        else _dam_average_from_items(dams.get("items") or [])
    )
    snapshot = {
        "key": spec["key"],
        "label": spec["label"],
        "date": selected_date,
        "note": spec["note"],
        "ptfAverage": _finite_float(
            (market_summary.get("ptfAverageByCurrency") or {}).get("TRY")
        ),
        "smfAverage": _finite_float(market_summary.get("smfAverage")),
        "damAverage": _finite_float(dam_average),
        "uevmTotal": _finite_float(production_summary.get("uevmTotal")),
        "consumptionAverage": _finite_float(consumption_summary.get("average")),
        "errors": errors,
    }
    snapshot["available"] = any(
        snapshot.get(metric) is not None
        for metric in (
            "ptfAverage",
            "smfAverage",
            "damAverage",
            "uevmTotal",
            "consumptionAverage",
        )
    )
    return snapshot


def _executive_comparisons(
    selected_day: date,
    client: Any,
    modules: dict[str, Any],
    dam_summary: dict[str, Any],
    errors: dict[str, str],
) -> list[dict[str, Any]]:
    specs = _executive_comparison_specs(selected_day)
    snapshots = [
        _executive_metric_snapshot(
            specs[0],
            client,
            modules=modules,
            dam_summary=dam_summary,
            module_errors={
                key: value
                for key, value in errors.items()
                if key in {"market", "dams", "production", "consumption"}
            },
        )
    ]
    snapshots.extend(
        _executive_metric_snapshot(spec, client)
        for spec in specs[1:]
    )
    selected = snapshots[0]
    metrics = (
        "ptfAverage",
        "smfAverage",
        "damAverage",
        "uevmTotal",
        "consumptionAverage",
    )
    for snapshot in snapshots:
        deltas: dict[str, float | None] = {}
        for metric in metrics:
            current = _finite_float(selected.get(metric))
            baseline = _finite_float(snapshot.get(metric))
            deltas[metric] = (
                current - baseline
                if current is not None and baseline is not None
                else None
            )
        snapshot["delta"] = deltas
    return snapshots


def _executive_dashboard(selected_date: str, client: Any) -> dict[str, Any]:
    """Dört panelin aynı güne ait yönetici özetini güvenli biçimde birleştir."""

    try:
        selected_day = date.fromisoformat(selected_date)
    except ValueError as exc:
        raise ValueError("Rapor tarihi YYYY-AA-GG biçiminde olmalıdır.") from exc
    today = datetime.now(URETIM.TR_TZ).date()
    if selected_day > today:
        raise ValueError("Gelecek tarihli rapor oluşturulamaz.")

    cache_key = selected_day.isoformat()
    now_timestamp = time.time()
    with EXECUTIVE_REPORT_CACHE_LOCK:
        cached = EXECUTIVE_REPORT_CACHE.get(cache_key)
        if cached and cached.get("expires", 0.0) > now_timestamp:
            return {**cached["payload"], "cached": True}

    modules: dict[str, Any] = {}
    errors: dict[str, str] = {}

    def collect(name: str, loader: Any) -> None:
        try:
            modules[name] = loader()
        except URETIM.EpiasError as exc:
            if exc.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise
            errors[name] = str(exc)
            modules[name] = None
        except ValueError as exc:
            errors[name] = str(exc)
            modules[name] = None
        except Exception:
            errors[name] = "Bu veri grubu hazırlanamadı."
            modules[name] = None

    collect("market", lambda: _market_dashboard(selected_date, client))
    collect("nextDayPtf", lambda: _next_day_ptf_dashboard(selected_date, client))
    collect("dams", lambda: _baraj_data(client, selected_date))
    collect(
        "production",
        lambda: URETIM_SERVICE.dashboard(
            URETIM.DateRange(start=selected_day, end=selected_day),
            client=client,
        ),
    )
    collect("consumption", lambda: _consumption_dashboard(selected_date, client))

    dams = modules.get("dams") or {}
    dam_items = dams.get("items") or []
    valid_dams = [
        (value, item)
        for item in dam_items
        if (value := _fullness_number(item)) is not None
    ]
    dam_values = [value for value, _ in valid_dams]
    dam_average = sum(dam_values) / len(dam_values) if dam_values else None
    highest = max(valid_dams, default=(None, None), key=lambda entry: entry[0] or 0)
    lowest = min(valid_dams, default=(None, None), key=lambda entry: entry[0] or 0)
    previous_dam_date = None
    previous_dam_average = None
    previous_candidates = []
    for available_date in dams.get("availableDates") or []:
        try:
            available_day = date.fromisoformat(str(available_date)[:10])
        except ValueError:
            continue
        if available_day < selected_day:
            previous_candidates.append(available_day)
    if previous_candidates:
        previous_dam_date = max(previous_candidates).isoformat()
        try:
            previous_dams = _baraj_data(client, previous_dam_date)
            previous_values = [
                value
                for item in previous_dams.get("items") or []
                if (value := _fullness_number(item)) is not None
            ]
            if previous_values:
                previous_dam_average = sum(previous_values) / len(previous_values)
        except Exception:
            previous_dam_average = None
    dam_summary = {
        "count": len(dam_items),
        "average": dam_average,
        "highest": (
            {"name": highest[1].get("dam"), "value": highest[0]}
            if highest[1] is not None
            else None
        ),
        "lowest": (
            {"name": lowest[1].get("dam"), "value": lowest[0]}
            if lowest[1] is not None
            else None
        ),
        "source": dams.get("sourceLabel"),
        "date": dams.get("selectedDate"),
        "previousDate": previous_dam_date,
        "previousAverage": previous_dam_average,
        "dailyChange": (
            dam_average - previous_dam_average
            if dam_average is not None and previous_dam_average is not None
            else None
        ),
    }
    comparisons = _executive_comparisons(
        selected_day,
        client,
        modules,
        dam_summary,
        errors,
    )

    report = {
        "date": selected_date,
        "generatedAt": datetime.now(URETIM.TR_TZ).isoformat(timespec="seconds"),
        "modules": modules,
        "damSummary": dam_summary,
        "comparisons": comparisons,
        "errors": errors,
        "availableModules": [
            name for name, payload in modules.items() if payload is not None
        ],
        "cached": False,
    }
    if not errors:
        ttl = 300 if selected_day >= today else 21_600
        with EXECUTIVE_REPORT_CACHE_LOCK:
            EXECUTIVE_REPORT_CACHE[cache_key] = {
                "payload": report,
                "expires": time.time() + ttl,
            }
    return report


def _executive_xlsx(report: dict[str, Any]) -> bytes:
    """Günlük yönetici özetini modül bazlı sayfalarla XLSX'e dönüştür."""

    selected_date = report.get("date") or "—"
    modules = report.get("modules") or {}
    market = modules.get("market") or {}
    market_summary = market.get("summary") or {}
    next_day = modules.get("nextDayPtf") or {}
    next_summary = next_day.get("summary") or {}
    dam_summary = report.get("damSummary") or {}
    production = modules.get("production") or {}
    production_summary = production.get("summary") or {}
    consumption = modules.get("consumption") or {}
    consumption_summary = consumption.get("summary") or {}
    errors = report.get("errors") or {}
    comparisons = report.get("comparisons") or []
    action_items = _executive_action_items(report)

    overview_rows = [
        [("Baha Enerji — Günlük Yönetici Raporu", 4), (None, 0)],
        [("Rapor tarihi", 1), (selected_date, 0)],
        [("Oluşturulma", 1), (report.get("generatedAt") or "—", 0)],
        [("Gösterge", 1), ("Değer", 1)],
        [("PTF ortalama (TL/MWh)", 0), ((market_summary.get("ptfAverageByCurrency") or {}).get("TRY"), 2)],
        [("SMF ortalama (TL/MWh)", 0), (market_summary.get("smfAverage"), 2)],
        [("Ertesi gün PTF durumu", 0), ((next_day.get("publication") or {}).get("label") or "—", 0)],
        [("Ertesi gün PTF ortalama (TL/MWh)", 0), ((next_summary.get("ptfAverageByCurrency") or {}).get("TRY"), 2)],
        [("Ortalama baraj doluluğu (%)", 0), (dam_summary.get("average"), 2)],
        [("Günlük ortalama doluluk değişimi (puan)", 0), (dam_summary.get("dailyChange"), 2)],
        [("Toplam UEVM (MWh)", 0), (production_summary.get("uevmTotal"), 2)],
        [("UEVM–UEÇM sapması (%)", 0), (production_summary.get("deviationPct"), 2)],
        [("Tüketim ortalaması (MWh)", 0), (consumption_summary.get("average"), 2)],
        [("Tüketim zirvesi (MWh)", 0), (consumption_summary.get("maximum"), 2)],
        [("Veri durumu", 1), ("Eksiksiz" if not errors else f"{len(errors)} modülde uyarı", 0)],
    ]
    overview_rows.append(
        [("İlk aksiyon", 1), (action_items[0]["title"] if action_items else "—", 0)]
    )
    action_rows = [[
        ("Alan", 1),
        ("Aksiyon", 1),
        ("Değer", 1),
        ("Durum", 1),
        ("Not", 1),
    ]]
    action_rows.extend([
        (item.get("label") or "—", 0),
        (item.get("title") or "—", 0),
        (item.get("value") or "—", 0),
        (item.get("level") or "normal", 0),
        (item.get("detail") or "—", 0),
    ] for item in action_items)

    comparison_rows = [[
        ("Kıyas", 1),
        ("Tarih", 1),
        ("Not", 1),
        ("PTF ort. (TL/MWh)", 1),
        ("PTF fark", 1),
        ("SMF ort. (TL/MWh)", 1),
        ("SMF fark", 1),
        ("Baraj ort. (%)", 1),
        ("Baraj fark", 1),
        ("UEVM toplam (MWh)", 1),
        ("UEVM fark", 1),
        ("Tüketim ort. (MWh)", 1),
        ("Tüketim fark", 1),
    ]]
    comparison_rows.extend([
        (snapshot.get("label") or "—", 0),
        (snapshot.get("date") or "—", 0),
        (snapshot.get("note") or "—", 0),
        (snapshot.get("ptfAverage"), 2),
        ((snapshot.get("delta") or {}).get("ptfAverage"), 2),
        (snapshot.get("smfAverage"), 2),
        ((snapshot.get("delta") or {}).get("smfAverage"), 2),
        (snapshot.get("damAverage"), 2),
        ((snapshot.get("delta") or {}).get("damAverage"), 2),
        (snapshot.get("uevmTotal"), 2),
        ((snapshot.get("delta") or {}).get("uevmTotal"), 2),
        (snapshot.get("consumptionAverage"), 2),
        ((snapshot.get("delta") or {}).get("consumptionAverage"), 2),
    ] for snapshot in comparisons)

    market_rows = [[
        ("Saat", 1), ("PTF (TL/MWh)", 1), ("SMF (TL/MWh)", 1),
        ("YAL (MWh)", 1), ("YAT (MWh)", 1), ("Sistem yönü", 1),
    ]]
    market_rows.extend([
        (row.get("time") or "—", 0), (row.get("ptf"), 2),
        (row.get("smf"), 2), (row.get("yal"), 2),
        (abs(row["yat"]) if row.get("yat") is not None else None, 2),
        (row.get("direction") or "—", 0),
    ] for row in market.get("rows") or [])

    dam_rows = [[
        ("Baraj", 1), ("Havza", 1), ("Aktif doluluk (%)", 1), ("Veri tarihi", 1),
    ]]
    dam_rows.extend([
        (item.get("dam") or "—", 0), (item.get("basin") or "—", 0),
        (_fullness_number(item), 2), (str(item.get("date") or "")[:10], 0),
    ] for item in (modules.get("dams") or {}).get("items") or [])

    production_rows = [[
        ("Tarih / saat", 1), ("UEVM (MWh)", 1), ("UEÇM (MWh)", 1),
        ("Yenilenebilir (MWh)", 1), ("Termik (MWh)", 1), ("Doğal gaz (MWh)", 1),
    ]]
    production_rows.extend([
        (row.get("timestamp") or "—", 0), (row.get("uevm"), 2),
        (row.get("uecm"), 2), (row.get("renewable"), 2),
        (row.get("thermal"), 2), (row.get("naturalGas"), 2),
    ] for row in production.get("series") or [])

    consumption_rows = [[("Saat", 1), ("Tüketim (MWh)", 1), ("Durum", 1)]]
    consumption_rows.extend([
        (row.get("time") or "—", 0), (row.get("consumption"), 2),
        ("Yayımlandı" if row.get("consumption") is not None else "Veri bekleniyor", 0),
    ] for row in consumption.get("rows") or [])

    return _xlsx_workbook((
        ("Aksiyonlar", URETIM._xlsx_sheet(action_rows, widths=[16, 34, 24, 16, 62], freeze_row=1, auto_filter=True)),
        ("Yönetici Özeti", URETIM._xlsx_sheet(overview_rows, widths=[38, 36])),
        ("Takvim Kıyası", URETIM._xlsx_sheet(comparison_rows, widths=[25, 16, 30, 20, 15, 20, 15, 18, 15, 22, 15, 24, 15], freeze_row=1, auto_filter=True)),
        ("Piyasa", URETIM._xlsx_sheet(market_rows, widths=[12, 19, 19, 16, 16, 23], freeze_row=1, auto_filter=True)),
        ("Barajlar", URETIM._xlsx_sheet(dam_rows, widths=[30, 28, 22, 17], freeze_row=1, auto_filter=True)),
        ("Üretim", URETIM._xlsx_sheet(production_rows, widths=[27, 18, 18, 24, 18, 20], freeze_row=1, auto_filter=True)),
        ("Tüketim", URETIM._xlsx_sheet(consumption_rows, widths=[13, 23, 22], freeze_row=1, auto_filter=True)),
    ))


def _tr_report_number(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    formatted = f"{number:,.{digits}f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def _executive_action_items(report: dict[str, Any]) -> list[dict[str, str]]:
    modules = report.get("modules") or {}
    market = modules.get("market") or {}
    market_summary = market.get("summary") or {}
    market_rows = market.get("rows") or []
    dam_summary = report.get("damSummary") or {}
    dam_items = (modules.get("dams") or {}).get("items") or []
    production_summary = (modules.get("production") or {}).get("summary") or {}
    consumption_summary = (modules.get("consumption") or {}).get("summary") or {}
    errors = report.get("errors") or {}

    def item(
        label: str,
        title: str,
        value: str,
        detail: str,
        level: str = "normal",
    ) -> dict[str, str]:
        return {
            "label": label,
            "title": title,
            "value": value,
            "detail": detail,
            "level": level,
        }

    ptf_average = _finite_float(
        (market_summary.get("ptfAverageByCurrency") or {}).get("TRY")
        or market_summary.get("ptfAverage")
    )
    ptf_points = [
        row for row in market_rows
        if _finite_float(row.get("ptf")) is not None
    ]
    peak_ptf = max(
        ptf_points,
        key=lambda row: _finite_float(row.get("ptf")) or 0,
        default={},
    )
    peak_value = _finite_float(peak_ptf.get("ptf"))
    if ptf_average is not None and peak_value is not None and ptf_average:
        peak_gap = (peak_value - ptf_average) / ptf_average * 100
        price_level = "critical" if peak_gap >= 25 else "warning" if peak_gap >= 12 else "good"
        price_detail = (
            f"{peak_ptf.get('time') or '—'} saatindeki zirve, günlük PTF "
            f"ortalamasının %{_tr_report_number(abs(peak_gap), 1)} "
            f"{'üzerinde' if peak_gap >= 0 else 'altında'}."
        )
        price_value = f"{_tr_report_number(peak_value)} TL/MWh"
    else:
        price_level = "warning"
        price_value = "Veri yok"
        price_detail = "PTF zirvesi için yeterli saatlik veri bulunamadı."

    critical_dams = [
        item for item in dam_items
        if (value := _fullness_number(item)) is not None and value <= 30
    ]
    low_dams = [
        item for item in dam_items
        if (value := _fullness_number(item)) is not None and value <= 50
    ]
    daily_change = _finite_float(dam_summary.get("dailyChange"))
    if critical_dams:
        dam_level = "critical"
        dam_value = f"{len(critical_dams)} kritik baraj"
        dam_detail = "Doluluk %30 ve altında olan barajlar öncelikli takip edilmeli."
    elif low_dams:
        dam_level = "warning"
        dam_value = f"{len(low_dams)} düşük baraj"
        dam_detail = "Doluluk %50 ve altında olan barajlar yakın izleme listesinde."
    elif daily_change is not None and daily_change < -1:
        dam_level = "warning"
        dam_value = f"↓ {_tr_report_number(abs(daily_change))} puan"
        dam_detail = "Ortalama doluluk önceki yayınlanan güne göre belirgin azaldı."
    else:
        dam_level = "good"
        dam_value = "Stabil"
        dam_detail = "Kritik doluluk eşiğinde belirgin baraj yoğunluğu görünmüyor."

    deviation = _finite_float(production_summary.get("deviationPct"))
    if deviation is None:
        production_level = "warning"
        production_value = "Veri yok"
        production_detail = "UEVM ve UEÇM sapması hesaplanamadı."
    else:
        abs_deviation = abs(deviation)
        production_level = "critical" if abs_deviation >= 8 else "warning" if abs_deviation >= 4 else "good"
        sign = "+" if deviation > 0 else "−" if deviation < 0 else "±"
        production_value = f"{sign}%{_tr_report_number(abs_deviation)}"
        production_detail = (
            "UEVM ile UEÇM arasındaki sapma operasyonel izleme gerektiriyor."
            if abs_deviation >= 4
            else "Üretim programı ile gerçekleşen üretim yakın seyrediyor."
        )

    available_hours = int(consumption_summary.get("availableHours") or 0)
    maximum = _finite_float(consumption_summary.get("maximum"))
    if maximum is None:
        consumption_level = "warning"
        consumption_value = "Veri bekleniyor"
        consumption_detail = "Tüketim eğrisi için yeterli saatlik veri yayımlanmadı."
    else:
        consumption_level = "warning" if available_hours < 18 else "good"
        consumption_value = f"{_tr_report_number(maximum)} MWh"
        consumption_detail = (
            f"Zirve {consumption_summary.get('maximumHour') or '—'} saatinde; "
            f"{available_hours}/24 saat yayımlandı."
        )

    if errors:
        data_level = "warning"
        data_value = f"{len(errors)} uyarı"
        data_detail = "Eksik modül varsa rapordaki karar notları temkinli okunmalı."
    else:
        data_level = "good"
        data_value = "Tam"
        data_detail = "Rapor tüm ana veri gruplarından üretildi."

    return [
        item("FİYAT", "PTF zirvesini kontrol et", price_value, price_detail, price_level),
        item("BARAJ", "Doluluk eşiğini izle", dam_value, dam_detail, dam_level),
        item("ÜRETİM", "UEVM–UEÇM sapmasını takip et", production_value, production_detail, production_level),
        item("TÜKETİM", "Talep zirvesini izle", consumption_value, consumption_detail, consumption_level),
        item("VERİ", "Rapor güvenini kontrol et", data_value, data_detail, data_level),
    ]


def _executive_report_html(report: dict[str, Any], *, auto_print: bool = False) -> str:
    """Yazdırma iletişim kutusundan PDF'e kaydedilebilen markalı rapor sayfası."""

    modules = report.get("modules") or {}
    market = modules.get("market") or {}
    market_summary = market.get("summary") or {}
    next_day = modules.get("nextDayPtf") or {}
    dam_summary = report.get("damSummary") or {}
    production = modules.get("production") or {}
    production_summary = production.get("summary") or {}
    consumption = modules.get("consumption") or {}
    consumption_summary = consumption.get("summary") or {}
    comparisons = report.get("comparisons") or []
    selected_date = str(report.get("date") or "")
    display_date = ".".join(reversed(selected_date.split("-"))) if selected_date else "—"
    next_publication = (next_day.get("publication") or {}).get("label") or "Yayımlanmadı"
    next_average = ((next_day.get("summary") or {}).get("ptfAverageByCurrency") or {}).get("TRY")
    ptf_average = (market_summary.get("ptfAverageByCurrency") or {}).get("TRY")
    highest_dam = dam_summary.get("highest") or {}
    lowest_dam = dam_summary.get("lowest") or {}
    daily_dam_change = dam_summary.get("dailyChange")
    try:
        daily_dam_change_number = float(daily_dam_change)
    except (TypeError, ValueError):
        daily_dam_change_number = None
    if daily_dam_change_number is None or not math.isfinite(daily_dam_change_number):
        daily_dam_change_value = "Veri bulunamadı"
        daily_dam_change_detail = "Önceki yayımlanan günle karşılaştırma yapılamadı."
    else:
        change_arrow = "↑" if daily_dam_change_number > 0 else "↓" if daily_dam_change_number < 0 else "→"
        change_sign = "+" if daily_dam_change_number > 0 else "−" if daily_dam_change_number < 0 else ""
        daily_dam_change_value = (
            f"{change_arrow} {change_sign}{_tr_report_number(abs(daily_dam_change_number))} puan"
        )
        previous_date = str(dam_summary.get("previousDate") or "")
        previous_display = (
            ".".join(reversed(previous_date.split("-")))
            if previous_date else "önceki yayımlanan gün"
        )
        daily_dam_change_detail = f"{previous_display} tarihine göre ortalama doluluk."
    market_rows = market.get("rows") or []
    peak_ptf = max(
        (row for row in market_rows if row.get("ptf") is not None),
        key=lambda row: row.get("ptf") or 0,
        default={},
    )
    deviation_pct = production_summary.get("deviationPct")
    try:
        deviation_number = float(deviation_pct)
    except (TypeError, ValueError):
        deviation_number = None
    if deviation_number is None or not math.isfinite(deviation_number):
        production_balance_value = "Veri bulunamadı"
        production_insight = "Üretim dengesi için yeterli veri bulunamadı."
    elif deviation_number > 0:
        production_balance_value = f"+%{_tr_report_number(abs(deviation_number))}"
        production_insight = f"UEVM, UEÇM'nin %{_tr_report_number(abs(deviation_number))} üzerinde."
    elif deviation_number < 0:
        production_balance_value = f"−%{_tr_report_number(abs(deviation_number))}"
        production_insight = f"UEVM, UEÇM'nin %{_tr_report_number(abs(deviation_number))} altında."
    else:
        production_balance_value = "%0,00"
        production_insight = "UEVM ve UEÇM aynı seviyede."

    direction_counts = {"Enerji Fazlası": 0, "Enerji Açığı": 0, "Dengede": 0}
    for row in market_rows:
        direction = str(row.get("direction") or "").casefold()
        if "fazla" in direction:
            direction_counts["Enerji Fazlası"] += 1
        elif "aç" in direction:
            direction_counts["Enerji Açığı"] += 1
        elif "denge" in direction:
            direction_counts["Dengede"] += 1
    published_directions = sum(direction_counts.values())
    if published_directions:
        highest_direction_count = max(direction_counts.values())
        dominant_directions = [
            label for label, count in direction_counts.items()
            if count == highest_direction_count
        ]
        system_direction_value = (
            f"{dominant_directions[0]} baskın"
            if len(dominant_directions) == 1 else "Karma sistem yönü"
        )
        system_direction_detail = (
            f'{direction_counts["Enerji Fazlası"]} fazla · '
            f'{direction_counts["Enerji Açığı"]} açık · '
            f'{direction_counts["Dengede"]} dengede'
        )
    else:
        system_direction_value = "Veri bulunamadı"
        system_direction_detail = "Seçilen tarih için sistem yönü yayımlanmadı."

    production_groups = production.get("groups") or []
    leading_group = max(
        (
            group for group in production_groups
            if isinstance(group.get("share"), (int, float))
        ),
        key=lambda group: group.get("share") or 0,
        default={},
    )
    leading_group_value = (
        f'{xml_escape(str(leading_group.get("label") or "—"))} · '
        f'%{_tr_report_number(leading_group.get("share"), 1)}'
        if leading_group else "Veri bulunamadı"
    )
    leading_group_detail = (
        "Günlük üretimde en yüksek paya sahip kaynak grubu."
        if leading_group else "Seçilen tarih için üretim dağılımı hesaplanamadı."
    )
    consumption_peak_value = (
        f'{_tr_report_number(consumption_summary.get("maximum"))} MWh'
        if consumption_summary.get("maximum") is not None else "Veri bulunamadı"
    )
    consumption_peak_detail = (
        f'{xml_escape(str(consumption_summary.get("maximumHour") or "—"))} saatinde tüketim zirvesi.'
        if consumption_summary.get("maximum") is not None
        else "Seçilen tarih için tüketim zirvesi hesaplanamadı."
    )
    top_dams = sorted(
        (modules.get("dams") or {}).get("items") or [],
        key=lambda item: _fullness_number(item) if _fullness_number(item) is not None else -1,
        reverse=True,
    )[:6]
    dam_table = "".join(
        f'<tr><td>{xml_escape(str(item.get("dam") or "—"))}</td>'
        f'<td>{xml_escape(str(item.get("basin") or "—"))}</td>'
        f'<td>%{_tr_report_number(_fullness_number(item))}</td></tr>'
        for item in top_dams
    ) or '<tr><td colspan="3">Veri bulunamadı.</td></tr>'
    group_rows = "".join(
        f'<tr><td>{xml_escape(str(group.get("label") or "—"))}</td>'
        f'<td>{_tr_report_number(group.get("value"))} MWh</td>'
        f'<td>%{_tr_report_number(group.get("share"), 1)}</td></tr>'
        for group in production.get("groups") or []
    ) or '<tr><td colspan="3">Veri bulunamadı.</td></tr>'
    comparison_metrics = (
        ("ptfAverage", "PTF ort.", "TL/MWh", 2),
        ("smfAverage", "SMF ort.", "TL/MWh", 2),
        ("damAverage", "Baraj ort.", "%", 2),
        ("uevmTotal", "UEVM", "MWh", 0),
        ("consumptionAverage", "Tük. ort.", "MWh", 0),
    )

    def comparison_metric_cell(snapshot: dict[str, Any], key: str, unit: str, digits: int) -> str:
        value = snapshot.get(key)
        if value is None:
            return '<td><b>—</b></td>'
        if unit == "%":
            display_value = f"%{_tr_report_number(value, digits)}"
        else:
            display_value = f"{_tr_report_number(value, digits)} {unit}"
        delta = (snapshot.get("delta") or {}).get(key)
        if delta is None or snapshot.get("key") == "selected":
            return f"<td><b>{xml_escape(display_value)}</b><small>baz gün</small></td>"
        delta_number = _finite_float(delta)
        if delta_number is None:
            return f"<td><b>{xml_escape(display_value)}</b></td>"
        delta_class = "positive" if delta_number > 0 else "negative" if delta_number < 0 else "neutral"
        delta_prefix = "+" if delta_number > 0 else "−" if delta_number < 0 else "±"
        delta_text = f"{delta_prefix}{_tr_report_number(abs(delta_number), digits)}"
        return (
            f'<td><b>{xml_escape(display_value)}</b>'
            f'<small class="{delta_class}">{xml_escape(delta_text)} fark</small></td>'
        )

    comparison_header = "".join(
        f"<th>{xml_escape(label)}</th>" for _, label, _, _ in comparison_metrics
    )
    comparison_rows = "".join(
        "<tr>"
        f'<td><b>{xml_escape(str(snapshot.get("label") or "—"))}</b>'
        f'<small>{xml_escape(".".join(reversed(str(snapshot.get("date") or "").split("-"))))}</small>'
        f'<em>{xml_escape(str(snapshot.get("note") or ""))}</em></td>'
        + "".join(
            comparison_metric_cell(snapshot, key, unit, digits)
            for key, _, unit, digits in comparison_metrics
        )
        + "</tr>"
        for snapshot in comparisons
    )
    comparison_section = (
        f'''<section class="report-card report-calendar-compare"><header><span>00 / TAKVİM KIYASI</span><h2>Seçili günü benzer günlerle kıyaslayın</h2><p>Dün, geçen hafta aynı gün ve geçen yıl aynı ISO hafta-gün eşleşmesi kullanılır; geçen yıl kıyası aynı takvim tarihine sabitlenmez.</p></header><table><thead><tr><th>Dönem</th>{comparison_header}</tr></thead><tbody>{comparison_rows}</tbody></table></section>'''
        if comparisons else ""
    )
    action_items = _executive_action_items(report)
    action_cards = "".join(
        f'<article class="report-action {xml_escape(str(item.get("level") or "normal"))}">'
        f'<span>{xml_escape(str(item.get("label") or "—"))}</span>'
        f'<h3>{xml_escape(str(item.get("title") or "—"))}</h3>'
        f'<b>{xml_escape(str(item.get("value") or "—"))}</b>'
        f'<p>{xml_escape(str(item.get("detail") or "—"))}</p></article>'
        for item in action_items
    )
    action_section = (
        f'''<section class="report-card report-actions"><header><span>05 / AKSİYON LİSTESİ</span><h2>Bugün neye bakmalı?</h2><p>Raporun ana sinyalleri fiyat, baraj, üretim, tüketim ve veri güveni başlıklarında önceliklendirildi.</p></header><div class="report-actions-grid">{action_cards}</div></section>'''
        if action_cards else ""
    )
    auto_attribute = "true" if auto_print else "false"
    return f'''<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Baha Enerji Günlük Yönetici Raporu · {xml_escape(display_date)}</title>
<link rel="stylesheet" href="{asset_url("/executive-report.css")}"><link rel="icon" href="{asset_url("/favicon.ico")}"></head>
<body data-auto-print="{auto_attribute}"><header class="report-toolbar"><a href="/piyasa/">← Panele dön</a>
<form action="/rapor" method="get"><input name="date" type="date" value="{xml_escape(selected_date)}" max="{datetime.now(URETIM.TR_TZ).date().isoformat()}"><button type="submit">Raporu getir</button></form>
<a class="xlsx" href="/api/executive-report.xlsx?date={urllib.parse.quote(selected_date)}">↓ XLSX</a><button id="reportPrint" type="button">PDF / Yazdır</button></header>
<main class="report-page"><header class="report-cover"><div class="report-brand"><img src="/suite-assets/baha-logo.png" alt=""><div><b>BAHA ENERJİ</b><span>GÜNLÜK YÖNETİCİ RAPORU</span></div></div><div class="report-date"><span>RAPOR TARİHİ</span><strong>{xml_escape(display_date)}</strong><small>{xml_escape(str(report.get("generatedAt") or ""))}</small></div></header>
<section class="report-intro"><div><span>GÜNÜN ÖZETİ</span><h1>Enerjinin bütün resmi,<br>tek raporda.</h1></div><p>Piyasa, baraj, üretim ve tüketim göstergeleri EPİAŞ verileriyle aynı tarih için bir araya getirildi.</p></section>
<section class="report-kpis"><article><span>PTF ORTALAMA</span><strong>{_tr_report_number(ptf_average)}</strong><small>TL/MWh</small></article><article><span>ORT. BARAJ DOLULUĞU</span><strong>%{_tr_report_number(dam_summary.get("average"))}</strong><small>{dam_summary.get("count") or 0} baraj</small></article><article><span>UEVM · UEÇM SAPMASI</span><strong>%{_tr_report_number(production_summary.get("deviationPct"))}</strong><small>{_tr_report_number(production_summary.get("difference"))} MWh</small></article><article><span>TÜKETİM ZİRVESİ</span><strong>{_tr_report_number(consumption_summary.get("maximum"))}</strong><small>{xml_escape(str(consumption_summary.get("maximumHour") or "—"))}</small></article></section>
{comparison_section}
{action_section}
<section class="report-grid"><article class="report-card market"><header><span>01 / PİYASA</span><h2>Günlük fiyat görünümü</h2></header><div class="report-stat-list"><div><span>SMF ortalama</span><b>{_tr_report_number(market_summary.get("smfAverage"))} TL/MWh</b></div><div><span>PTF zirvesi</span><b>{_tr_report_number(peak_ptf.get("ptf"))} · {xml_escape(str(peak_ptf.get("time") or "—"))}</b></div><div><span>Toplam YAL / YAT</span><b>{_tr_report_number(market_summary.get("yalTotal"))} / {_tr_report_number(market_summary.get("yatTotal"))} MWh</b></div><div><span>Ertesi gün PTF</span><b>{xml_escape(str(next_publication))} · {_tr_report_number(next_average)} TL/MWh</b></div></div></article>
<article class="report-card dams"><header><span>02 / BARAJLAR</span><h2>Doluluk görünümü</h2></header><div class="report-stat-list"><div><span>En yüksek</span><b>{xml_escape(str(highest_dam.get("name") or "—"))} · %{_tr_report_number(highest_dam.get("value"))}</b></div><div><span>En düşük</span><b>{xml_escape(str(lowest_dam.get("name") or "—"))} · %{_tr_report_number(lowest_dam.get("value"))}</b></div><div><span>Kaynak</span><b>{xml_escape(str(dam_summary.get("source") or "—"))}</b></div><div><span>Günlük değişim</span><b>{xml_escape(daily_dam_change_value)}</b><small>{xml_escape(daily_dam_change_detail)}</small></div></div></article></section>
<section class="report-grid tables"><article class="report-card"><header><span>03 / BARAJ LİSTESİ</span><h2>En yüksek doluluklar</h2></header><table><thead><tr><th>Baraj</th><th>Havza</th><th>Doluluk</th></tr></thead><tbody>{dam_table}</tbody></table></article><article class="report-card"><header><span>04 / ÜRETİM</span><h2>Kaynak grupları</h2></header><table><thead><tr><th>Grup</th><th>Üretim</th><th>Pay</th></tr></thead><tbody>{group_rows}</tbody></table></article></section>
<section class="report-card report-highlights"><header><span>06 / GÜNÜN ÖZETİ</span><h2>Öne çıkan gelişmeler</h2></header><div class="report-highlights-grid">
<article class="report-highlight system"><i>01</i><div><span>SİSTEM DENGESİ</span><b>{xml_escape(system_direction_value)}</b><p>{xml_escape(system_direction_detail)}</p></div></article>
<article class="report-highlight mix"><i>02</i><div><span>ÜRETİM KARMASI</span><b>{leading_group_value}</b><p>{leading_group_detail}</p></div></article>
<article class="report-highlight production"><i>03</i><div><span>ÜRETİM DENGESİ</span><b>{production_balance_value}</b><p>{xml_escape(production_insight)}</p></div></article>
<article class="report-highlight consumption"><i>04</i><div><span>TÜKETİM ZİRVESİ</span><b>{consumption_peak_value}</b><p>{consumption_peak_detail}</p></div></article>
</div></section>
<footer><div class="footer-mark">BAHA ENERJİ↗</div><p>Veri kaynağı: EPİAŞ Şeffaflık Platformu<br>Bu rapor operasyonel değerlendirme amacıyla otomatik oluşturulmuştur.</p></footer></main><script src="{asset_url("/executive-report.js")}" defer></script></body></html>'''


SUITE_LOADING_STYLE = """
<style id="baha-suite-loading-style">
  .baha-suite-loading-screen {
    position: fixed;
    inset: 0;
    z-index: 2147483000;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 28px;
    box-sizing: border-box;
    background: #0b1930;
    color: #fff;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    opacity: 1;
    visibility: visible;
    transition: opacity .28s ease, visibility .28s ease;
  }
  .baha-suite-loading-screen.is-hidden {
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
  }
  .baha-suite-loading-card {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
  }
  .baha-suite-loading-card img {
    width: 92px;
    height: 92px;
    object-fit: contain;
    padding: 10px;
    box-sizing: border-box;
    border-radius: 24px;
    background: #fff;
    box-shadow: 0 24px 60px rgba(2, 10, 24, .32);
  }
  .baha-suite-loading-card strong {
    margin-top: 22px;
    color: #fff;
    font-size: clamp(28px, 5vw, 36px);
    line-height: 1.05;
    font-weight: 800;
    letter-spacing: -.04em;
  }
  .baha-suite-loading-card span {
    margin-top: 8px;
    color: #abbede;
    font-size: 15px;
    line-height: 1.45;
  }
  .baha-suite-loading-bar {
    width: min(220px, 62vw);
    height: 3px;
    margin-top: 22px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(171, 190, 220, .18);
  }
  .baha-suite-loading-bar i {
    display: block;
    width: 42%;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg, #2f70ee, #68e1fd);
    animation: baha-suite-loading-slide 1.05s ease-in-out infinite;
  }
  @keyframes baha-suite-loading-slide {
    0% { transform: translateX(-110%); }
    100% { transform: translateX(260%); }
  }
</style>
"""


SUITE_LOADING_MARKUP = """
<div id="bahaSuiteLoading" class="baha-suite-loading-screen" role="status" aria-live="polite">
  <div class="baha-suite-loading-card">
    <img src="/suite-assets/baha-logo.png" alt="Baha Enerji">
    <strong>Baha Enerji</strong>
    <span>Panel hazırlanıyor...</span>
    <div class="baha-suite-loading-bar" aria-hidden="true"><i></i></div>
  </div>
</div>
"""


def _inject_suite_loading(text: str) -> str:
    if "bahaSuiteLoading" in text:
        return text
    text = text.replace(
        "</head>",
        f'{SUITE_LOADING_STYLE}<script src="{asset_url("/suite-loading.js")}" defer></script></head>',
        1,
    )
    body_match = re.search(r"<body\b[^>]*>", text, flags=re.IGNORECASE)
    if not body_match:
        return text
    return f"{text[:body_match.end()]}{SUITE_LOADING_MARKUP}{text[body_match.end():]}"


# Shared HTML shell helpers are imported from baha_suite.shell/html_tools.


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "BahaEnerjiSuite/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.getenv("HTTP_LOG", "true").lower() not in {"0", "false", "no"}:
            super().log_message(fmt, *args)

    def _accepts_gzip(self) -> bool:
        accepted = self.headers.get("Accept-Encoding", "")
        return any(
            item.split(";", 1)[0].strip().lower() == "gzip"
            for item in accepted.split(",")
        )

    @staticmethod
    def _is_compressible_content(content_type: str) -> bool:
        media_type = content_type.split(";", 1)[0].strip().lower()
        return media_type.startswith("text/") or media_type in COMPRESSIBLE_CONTENT_TYPES

    def _encoded_content(
        self, content: bytes, content_type: str
    ) -> tuple[bytes, dict[str, str]]:
        if (
            len(content) < GZIP_MIN_BYTES
            or not self._accepts_gzip()
            or not self._is_compressible_content(content_type)
        ):
            return content, {}
        compressed = gzip.compress(content, compresslevel=6, mtime=0)
        if len(compressed) >= len(content):
            return content, {}
        return compressed, {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}

    def _send_content(
        self,
        content: bytes,
        content_type: str,
        *,
        status: int = HTTPStatus.OK,
        cache_control: str = "no-store",
        headers: dict[str, str] | None = None,
    ) -> None:
        body, encoding_headers = self._encoded_content(content, content_type)
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in encoding_headers.items():
                self.send_header(name, value)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Tarayıcı sayfa değiştirince/yenileyince açık isteği iptal edebilir.
            # Bu bir uygulama hatası değildir; konsolu gereksiz traceback ile kirletme.
            return

    def _send_attachment(
        self, content: bytes, content_type: str, filename: str
    ) -> None:
        self._send_content(
            content,
            content_type,
            cache_control="no-store",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def _json(
        self,
        payload: dict[str, Any],
        status: int = HTTPStatus.OK,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        content = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self._send_content(
            content,
            "application/json; charset=utf-8",
            status=status,
            cache_control="no-store",
            headers=headers,
        )

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _session_token(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except Exception:
            return None
        morsel = cookie.get(AUTH.cookie_name)
        return morsel.value if morsel else None

    def _session(self):
        return AUTH.get_session(self._session_token())

    def _secure_request(self) -> bool:
        forwarded = self.headers.get("X-Forwarded-Proto", "")
        return forwarded.split(",", 1)[0].strip().lower() == "https"

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        value = forwarded.split(",", 1)[0].strip() if forwarded else ""
        if not value:
            value = str(self.client_address[0] if self.client_address else "unknown")
        return value[:64]

    def _login_rate_limited(self, retry_after: int) -> None:
        self._json(
            {
                "error": (
                    "Çok fazla hatalı giriş denemesi yapıldı. "
                    f"{retry_after} saniye sonra yeniden deneyin."
                )
            },
            HTTPStatus.TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )

    def _read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Geçersiz istek uzunluğu.") from exc
        if size <= 0 or size > 16_384:
            raise ValueError("Geçersiz istek gövdesi.")
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Geçersiz JSON gövdesi.") from exc
        if not isinstance(payload, dict):
            raise ValueError("İstek gövdesi nesne olmalıdır.")
        return payload

    def _client(self):
        token = self._session_token()
        session = AUTH.get_session(token)
        if not token or not session:
            self._json(
                {"error": "Oturum açmanız gerekiyor."},
                HTTPStatus.UNAUTHORIZED,
            )
            return None
        return token, URETIM.EpiasClient(tgt=session.tgt)

    def _epias_error(self, exc: Exception, token: str) -> None:
        status_code = getattr(exc, "status_code", None)
        if status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            AUTH.revoke(token)
            self._json(
                {"error": "EPİAŞ oturumunun süresi doldu. Yeniden giriş yapın."},
                HTTPStatus.UNAUTHORIZED,
                headers={
                    "Set-Cookie": AUTH.clear_cookie_header(
                        secure_request=self._secure_request()
                    )
                },
            )
        elif status_code == HTTPStatus.TOO_MANY_REQUESTS:
            self._json(
                {"error": str(exc)},
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": "60"},
            )
        else:
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def _csp(self, kind: str) -> str:
        if kind == "piyasa":
            return (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
                "worker-src 'self'; base-uri 'self'; frame-ancestors 'none'"
            )
        if kind == "baraj":
            return (
                "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; worker-src 'self'; "
                "base-uri 'self'; frame-ancestors 'none'"
            )
        return (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
            "worker-src 'self'; base-uri 'self'; frame-ancestors 'none'"
        )

    def _static_cache_control(self, suffix: str, text_suffixes: set[str], name: str) -> str:
        if name == "sw.js" or suffix == ".html":
            return "no-cache"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "v" in query:
            return "public, max-age=31536000, immutable"
        if suffix in text_suffixes:
            return "no-cache"
        return "public, max-age=86400"

    def _serve_file(
        self,
        candidate: Path,
        *,
        root: Path,
        prefix: str = "",
        kind: str = "portal",
        inject_navigation: bool = False,
    ) -> None:
        try:
            candidate.resolve().relative_to(root.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        file_stat = candidate.stat()
        cache_key = (str(candidate.resolve()), "", "", False)
        with STATIC_FILE_CACHE_LOCK:
            cached_static = STATIC_FILE_CACHE.get(cache_key)
            if (
                cached_static
                and cached_static.get("mtime") == file_stat.st_mtime_ns
                and cached_static.get("size") == file_stat.st_size
            ):
                content = cached_static["content"]
            else:
                content = candidate.read_bytes()
                STATIC_FILE_CACHE[cache_key] = {
                    "mtime": file_stat.st_mtime_ns,
                    "size": file_stat.st_size,
                    "content": content,
                }
        suffix = candidate.suffix.lower()
        text_suffixes = {
            ".html",
            ".js",
            ".css",
            ".geojson",
            ".json",
            ".webmanifest",
            ".svg",
        }
        if prefix and suffix in text_suffixes:
            text = _rewrite_paths(content.decode("utf-8"), prefix)
            # Oturum sonu ekranı modüllere ait bir dosya değil, portalın
            # herkese açık ortak sayfasıdır; modül öneki almamalıdır.
            text = text.replace(
                f"{prefix}/oturum-kapatildi", "/oturum-kapatildi"
            )
            if kind == "sistem":
                text = text.replace(
                    f"{prefix}/system-direction-forecast.js",
                    "/system-direction-forecast.js",
                )
                text = text.replace(
                    f"{prefix}/system-direction-forecast.css",
                    "/system-direction-forecast.css",
                )
            # Alt paneller kendi eski giriş ekranlarını göstermek yerine her
            # zaman sitenin tek ortak giriş ekranını kullanır.
            if kind == "baraj" and candidate.name == "index.html":
                text = text.replace(
                    "showLogin();", "window.location.replace('/login');"
                )
                text = text.replace(
                    'id="loginScreen" class="page page-center login-screen"',
                    (
                        'id="loginScreen" class="page page-center login-screen" '
                        'style="display:none!important" aria-hidden="true"'
                    ),
                )
                text = text.replace(
                    'id="dashboard" class="page d-none"',
                    'id="dashboard" class="page"',
                )
                text = text.replace(
                    '<div class="row row-deck row-cards mb-3">',
                    (
                        '<div id="baraj-summary" '
                        'class="row row-deck row-cards mb-3">'
                    ),
                    1,
                )
                text = text.replace(
                    '<div class="card"><div class="card-header">'
                    '<h3 class="card-title">',
                    (
                        '<div id="baraj-list" class="card">'
                        '<div class="card-header"><h3 class="card-title">'
                    ),
                    1,
                )
                text = re.sub(
                    r'<h2 class="page-title">.*?</h2>',
                    '<h2 class="page-title">Baraj Aktif Doluluk Özeti</h2>',
                    text,
                    count=1,
                    flags=re.DOTALL,
                )
            if kind == "uretim" and candidate.name == "index.html":
                text = text.replace(
                    "/uretim/app.js", "/uretim/app.js?v=12"
                )
                text = re.sub(
                    r'<div class="eyebrow">.*?</div>',
                    '<div class="eyebrow">PANEL / GENEL BAKIŞ</div>',
                    text,
                    count=1,
                    flags=re.DOTALL,
                )
                text = re.sub(
                    r'(<section class="hero">.*?<h1>).*?(</h1>)',
                    r"\1UEVM &amp; UEÇM Üretim Özeti\2",
                    text,
                    count=1,
                    flags=re.DOTALL,
                )
            content = text.encode("utf-8")
        if suffix == ".html":
            # Modüllerin eski veya eksik ikon tanımlarını tek bir favicon
            # kaynağında birleştir. Sürüm parametresi Chrome'un kalıcı
            # favicon önbelleğini de yeniler.
            text = content.decode("utf-8")
            text = re.sub(
                r"\s*<link\b[^>]*\brel=(['\"])(?:icon|shortcut icon|apple-touch-icon)\1[^>]*>",
                "",
                text,
                flags=re.IGNORECASE,
            )
            text = text.replace(
                "</head>", f"{SUITE_FAVICON_LINKS}</head>", 1
            )
            content = text.encode("utf-8")
        if inject_navigation and suffix == ".html":
            text = content.decode("utf-8")
            shell = (
                f'<link rel="stylesheet" href="{asset_url("/portal-shell.css")}">'
                f'<link rel="stylesheet" href="{asset_url("/chart-fullscreen.css")}">'
                f'<script src="{asset_url("/theme-sync.js")}"></script>'
                f'<script src="{asset_url("/command-center.js")}" defer></script>'
                f'<script src="{asset_url("/chart-fullscreen.js")}" defer></script>'
            )
            if kind == "piyasa":
                shell += f'<link rel="stylesheet" href="{asset_url("/piyasa-suite.css")}">'
            elif kind in {"baraj", "uretim", "tuketim"}:
                shell += f'<link rel="stylesheet" href="{asset_url("/module-suite.css")}">'
            elif kind == "sistem":
                shell += f'<link rel="stylesheet" href="{asset_url("/module-suite.css")}">'
                shell += f'<link rel="stylesheet" href="{asset_url("/system-direction-forecast.css")}">'
            body_shell = _suite_navigation(kind)
            if kind in {"baraj", "uretim", "tuketim", "sistem"}:
                body_shell += _module_sidebar(kind)
            text = text.replace("</head>", f"{shell}</head>", 1)
            text = text.replace(
                "<body>",
                (
                    f'<body class="baha-suite-page baha-suite-{kind}">'
                    f"{body_shell}"
                ),
                1,
            )
            if kind in {"piyasa", "baraj", "tuketim", "sistem"}:
                text = text.replace(
                    "</body>",
                    f"{_suite_footer(kind)}</body>",
                    1,
                )
            if kind in {"baraj", "uretim", "tuketim", "sistem"}:
                text = text.replace(
                    "</body>",
                    f'<script src="{asset_url("/module-suite.js")}" defer></script></body>',
                    1,
                )
            content = text.encode("utf-8")
        if (
            suffix == ".html"
            and candidate.name == "login.html"
            and not inject_navigation
        ):
            content = _inject_suite_loading(content.decode("utf-8")).encode("utf-8")
        if (
            suffix == ".html"
            and "BahaEnerjiAndroid/" in self.headers.get("User-Agent", "")
        ):
            text = content.decode("utf-8")
            text = text.replace(
                "</head>",
                (
                    f'<link rel="stylesheet" '
                    f'href="{asset_url("/android-app.css")}">'
                    "</head>"
                ),
                1,
            )
            content = text.encode("utf-8")

        content_type = (
            "application/manifest+json"
            if suffix == ".webmanifest"
            else "application/geo+json"
            if suffix == ".geojson"
            else mimetypes.guess_type(candidate.name)[0]
            or "application/octet-stream"
        )
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/geo+json",
            "application/json",
            "application/manifest+json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"

        headers = {
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": self._csp(kind),
        }
        if candidate.name == "sw.js":
            headers["Service-Worker-Allowed"] = f"{prefix or ''}/"
        self._send_content(
            content,
            content_type,
            cache_control=self._static_cache_control(
                suffix, text_suffixes, candidate.name
            ),
            headers=headers,
        )

    def _serve_module_static(
        self, path: str, *, prefix: str, root: Path, kind: str
    ) -> None:
        relative = path[len(prefix) :].lstrip("/")
        if not relative:
            relative = "index.html"
        self._serve_file(
            root / relative,
            root=root,
            prefix=prefix,
            kind=kind,
            inject_navigation=relative.endswith(".html"),
        )

    def _require_page_session(self) -> bool:
        if self._session():
            return True
        self._redirect("/login")
        return False

    def _handle_session(self) -> None:
        session = self._session()
        username = session.username if session else None
        status = HTTPStatus.OK if session else HTTPStatus.UNAUTHORIZED
        self._json(
            {
                "authenticated": bool(session),
                "username": username,
                "email": username,
                "name": username,
            },
            status,
        )

    def _handle_uretim_dashboard(self, parsed: Any) -> None:
        epias = self._client()
        if not epias:
            return
        token, client = epias
        try:
            date_range = URETIM.parse_date_range(
                urllib.parse.parse_qs(parsed.query)
            )
            self._json(URETIM_SERVICE.dashboard(date_range, client=client))
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except URETIM.EpiasError as exc:
            self._epias_error(exc, token)
        except Exception:
            self._json(
                {"error": "Beklenmeyen bir sunucu hatası oluştu."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _handle_uretim_export(self, parsed: Any, export_format: str = "xlsx") -> None:
        epias = self._client()
        if not epias:
            return
        token, client = epias
        try:
            date_range = URETIM.parse_date_range(
                urllib.parse.parse_qs(parsed.query)
            )
            dashboard = URETIM_SERVICE.dashboard(date_range, client=client)
            period = dashboard["period"]
            basename = f"baha-uretim-epias-{period['start']}-{period['end']}"
            if export_format == "xlsx":
                content = URETIM.build_xlsx(dashboard)
                self._send_attachment(
                    content,
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                    f"{basename}.xlsx",
                )
            elif export_format == "csv":
                content = _export_sections_csv(_uretim_export_sections(dashboard))
                self._send_attachment(
                    content, "text/csv; charset=utf-8", f"{basename}.csv"
                )
            elif export_format == "pdf":
                content = _export_sections_pdf(
                    "Baha Enerji — Üretim Raporu",
                    _uretim_export_sections(dashboard),
                )
                self._send_attachment(content, "application/pdf", f"{basename}.pdf")
            else:
                self._json(
                    {"error": "Desteklenmeyen dosya formatı."},
                    HTTPStatus.BAD_REQUEST,
                )
        except ValueError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except URETIM.EpiasError as exc:
            self._epias_error(exc, token)
        except Exception:
            self._json(
                {"error": "Dosya hazırlanamadı."},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._json(
                {
                    "status": "ok",
                    "modules": [
                        "piyasa",
                        "baraj",
                        "uretim",
                        "tuketim",
                        "sistem-yonu-tahmini",
                    ],
                    "time": datetime.now(URETIM.TR_TZ).isoformat(
                        timespec="seconds"
                    ),
                }
            )
            return
        if path in {
            "/api/session",
            "/piyasa/api/session",
            "/baraj/api/session",
            "/uretim/api/session",
            "/tuketim/api/session",
        }:
            self._handle_session()
            return
        if path == "/api/epias-limits":
            if not self._session():
                self._json(
                    {"error": "Oturum aÃ§manÄ±z gerekiyor."},
                    HTTPStatus.UNAUTHORIZED,
                )
                return
            payload = URETIM.epias_protection_snapshot()
            payload["configured"]["usernameFailedAttempts"] = {
                "maxAttempts": LOGIN_USERNAME_LIMITER.max_attempts,
                "windowSeconds": LOGIN_USERNAME_LIMITER.window_seconds,
                "blockSeconds": LOGIN_USERNAME_LIMITER.block_seconds,
            }
            payload["background"] = BACKGROUND_REFRESH.snapshot()
            self._json(payload)
            return
        if path == "/api/command-center":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [datetime.now(URETIM.TR_TZ).date().isoformat()]
            )[0]
            try:
                self._json(_executive_dashboard(selected_date, client))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            except Exception:
                self._json(
                    {"error": "Komuta merkezi verileri hazırlanamadı."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/api/executive-report.xlsx":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [datetime.now(URETIM.TR_TZ).date().isoformat()]
            )[0]
            try:
                report = _executive_dashboard(selected_date, client)
                content = _executive_xlsx(report)
                filename = f"baha-enerji-yonetici-raporu-{selected_date}.xlsx"
                self._send_attachment(
                    content,
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                    filename,
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            except Exception:
                self._json(
                    {"error": "Yönetici raporu hazırlanamadı."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/rapor":
            if not self._require_page_session():
                return
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            selected_date = query.get(
                "date", [datetime.now(URETIM.TR_TZ).date().isoformat()]
            )[0]
            auto_print = query.get("print", ["0"])[0] == "1"
            try:
                report = _executive_dashboard(selected_date, client)
                content = _executive_report_html(
                    report, auto_print=auto_print
                ).encode("utf-8")
                self._send_content(
                    content,
                    "text/html; charset=utf-8",
                    cache_control="no-store",
                    headers={
                        "Content-Security-Policy": (
                            "default-src 'self'; img-src 'self' data:; "
                            "style-src 'self'; script-src 'self'; base-uri 'self'; "
                            "frame-ancestors 'none'"
                        )
                    },
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            except Exception:
                self._json(
                    {"error": "Yönetici raporu hazırlanamadı."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/piyasa/api/data":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            try:
                self._json(
                    _market_dashboard(
                        selected_date,
                        client,
                        force_refresh=force_refresh,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/piyasa/api/next-day-ptf":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            try:
                self._json(
                    _next_day_ptf_dashboard(
                        selected_date,
                        client,
                        force_refresh=force_refresh,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path in {
            "/piyasa/api/export.xlsx",
            "/piyasa/api/export.csv",
            "/piyasa/api/export.pdf",
        }:
            epias = self._client()
            if not epias:
                return
            token, client = epias
            export_format = path.rsplit(".", 1)[-1]
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [""]
            )[0]
            try:
                dashboard = _market_dashboard(selected_date, client)
                basename = f"baha-enerji-piyasa-{selected_date}"
                if export_format == "xlsx":
                    content = _market_xlsx(dashboard)
                    self._send_attachment(
                        content,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                        f"{basename}.xlsx",
                    )
                elif export_format == "csv":
                    content = _export_sections_csv(_market_export_sections(dashboard))
                    self._send_attachment(
                        content, "text/csv; charset=utf-8", f"{basename}.csv"
                    )
                else:
                    content = _export_sections_pdf(
                        "Baha Enerji — Günlük Piyasa Raporu",
                        _market_export_sections(dashboard),
                    )
                    self._send_attachment(
                        content, "application/pdf", f"{basename}.pdf"
                    )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/tuketim/api/data":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            selected_date = query.get("date", [""])[0]
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            try:
                self._json(
                    _consumption_dashboard(
                        selected_date,
                        client,
                        force_refresh=force_refresh,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/sistem-yonu-tahmini/api/forecast":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            target_date = query.get("date", [None])[0]
            force_refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            try:
                self._json(
                    _system_direction_forecast(
                        target_date,
                        client,
                        force_refresh=force_refresh,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            except Exception:
                self._json(
                    {"error": "Sistem yönü tahmini hazırlanamadı."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/sistem-yonu-tahmini/api/validation":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            selected_date = query.get("date", [None])[0]
            force_refresh = query.get("refresh", ["0"])[0] in {"1", "true", "yes"}
            try:
                self._json(
                    _system_direction_validation(
                        selected_date,
                        client,
                        force_refresh=force_refresh,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            except Exception:
                self._json(
                    {"error": "Tahmin doğrulaması hazırlanamadı."},
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if path == "/tuketim/api/forecast":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            query = urllib.parse.parse_qs(parsed.query)
            base_date = query.get("baseDate", [""])[0]
            target_date = query.get("targetDate", [""])[0] or None
            force_refresh = query.get("refresh", ["0"])[0] == "1"
            try:
                self._json(
                    _consumption_forecast(
                        base_date,
                        client,
                        force_refresh=force_refresh,
                        target_date=target_date,
                    )
                )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path in {
            "/tuketim/api/export.xlsx",
            "/tuketim/api/export.csv",
            "/tuketim/api/export.pdf",
        }:
            epias = self._client()
            if not epias:
                return
            token, client = epias
            export_format = path.rsplit(".", 1)[-1]
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [""]
            )[0]
            try:
                dashboard = _consumption_dashboard(selected_date, client)
                basename = f"baha-enerji-tuketim-{selected_date}"
                if export_format == "xlsx":
                    content = _consumption_xlsx(dashboard)
                    self._send_attachment(
                        content,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                        f"{basename}.xlsx",
                    )
                elif export_format == "csv":
                    content = _export_sections_csv(
                        _consumption_export_sections(dashboard)
                    )
                    self._send_attachment(
                        content, "text/csv; charset=utf-8", f"{basename}.csv"
                    )
                else:
                    content = _export_sections_pdf(
                        "Baha Enerji — Tüketim Raporu",
                        _consumption_export_sections(dashboard),
                    )
                    self._send_attachment(
                        content, "application/pdf", f"{basename}.pdf"
                    )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/baraj/api/basin-history":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            try:
                self._json(_baraj_basin_history(client))
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path in {
            "/baraj/api/basin-export.xlsx",
            "/baraj/api/basin-export.csv",
            "/baraj/api/basin-export.pdf",
        }:
            epias = self._client()
            if not epias:
                return
            token, client = epias
            export_format = path.rsplit(".", 1)[-1]
            basin_name = urllib.parse.parse_qs(parsed.query).get(
                "basin", [""]
            )[0].strip()
            try:
                history = _baraj_basin_history(client)
                basename = "baha-enerji-havza-baraj-doluluk"
                if export_format == "xlsx":
                    content = _baraj_basin_xlsx(history, basin_name)
                    self._send_attachment(
                        content,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                        f"{basename}.xlsx",
                    )
                elif export_format == "csv":
                    content = _export_sections_csv(
                        _baraj_basin_export_sections(history, basin_name)
                    )
                    self._send_attachment(
                        content, "text/csv; charset=utf-8", f"{basename}.csv"
                    )
                else:
                    content = _export_sections_pdf(
                        f"Baha Enerji — {basin_name} Havza Raporu",
                        _baraj_basin_export_sections(history, basin_name),
                    )
                    self._send_attachment(
                        content, "application/pdf", f"{basename}.pdf"
                    )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/baraj/api/active-fullness":
            epias = self._client()
            if not epias:
                return
            token, client = epias
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [""]
            )[0]
            try:
                self._json(_baraj_data(client, selected_date))
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path in {
            "/baraj/api/export.xlsx",
            "/baraj/api/export.csv",
            "/baraj/api/export.pdf",
        }:
            epias = self._client()
            if not epias:
                return
            token, client = epias
            export_format = path.rsplit(".", 1)[-1]
            sort_mode = urllib.parse.parse_qs(parsed.query).get(
                "sort", ["fullness-desc"]
            )[0]
            selected_date = urllib.parse.parse_qs(parsed.query).get(
                "date", [""]
            )[0]
            try:
                payload = _baraj_data(client, selected_date)
                data_date = (
                    payload.get("selectedDate")
                    or (payload.get("availableDates") or [""])[-1]
                    or datetime.now(URETIM.TR_TZ).date().isoformat()
                )
                basename = f"baha-enerji-baraj-aktif-{data_date}"
                if export_format == "xlsx":
                    content = _baraj_xlsx(payload, sort_mode)
                    self._send_attachment(
                        content,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                        f"{basename}.xlsx",
                    )
                elif export_format == "csv":
                    content = _export_sections_csv(
                        _baraj_export_sections(payload, sort_mode)
                    )
                    self._send_attachment(
                        content, "text/csv; charset=utf-8", f"{basename}.csv"
                    )
                else:
                    content = _export_sections_pdf(
                        "Baha Enerji — Baraj Aktif Doluluk Raporu",
                        _baraj_export_sections(payload, sort_mode),
                    )
                    self._send_attachment(
                        content, "application/pdf", f"{basename}.pdf"
                    )
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except URETIM.EpiasError as exc:
                self._epias_error(exc, token)
            return
        if path == "/uretim/api/dashboard":
            self._handle_uretim_dashboard(parsed)
            return
        if path in {
            "/uretim/api/export.xlsx",
            "/uretim/api/export.csv",
            "/uretim/api/export.pdf",
        }:
            self._handle_uretim_export(parsed, path.rsplit(".", 1)[-1])
            return

        if path in {
            "/piyasa",
            "/baraj",
            "/uretim",
            "/tuketim",
            "/sistem-yonu-tahmini",
            "/tv",
        }:
            self._redirect(path + "/")
            return
        if path == "/epias-koruma/":
            self._redirect("/epias-koruma")
            return
        if path == "/login/":
            self._redirect("/login")
            return
        if path == "/oturum-kapatildi/":
            self._redirect("/oturum-kapatildi")
            return
        if path in {"/dashboard", "/dashboard/", "/panel", "/panel/", "/index.html"}:
            self._redirect("/piyasa/" if self._session() else "/login")
            return
        if path in {
            "/uretim/login",
            "/piyasa/login",
            "/baraj/login",
            "/tuketim/login",
        }:
            self._redirect("/login")
            return
        if path.startswith("/piyasa/"):
            if not self._require_page_session():
                return
            if path[len("/piyasa") :] == "/piyasa-charts.js":
                self._serve_file(
                    PORTAL_DIR / "piyasa-charts.js",
                    root=PORTAL_DIR,
                    kind="piyasa",
                )
                return
            self._serve_module_static(
                path, prefix="/piyasa", root=PIYASA_DIR, kind="piyasa"
            )
            return
        if path.startswith("/baraj/"):
            if not self._require_page_session():
                return
            relative = path[len("/baraj") :]
            if relative == "/turkiye-havzalari.geojson":
                self._serve_file(
                    PORTAL_DIR / "turkiye-havzalari.geojson",
                    root=PORTAL_DIR,
                    kind="baraj",
                )
                return
            if relative in {
                "/icons/icon-192.png",
                "/icons/icon-512.png",
                "/apple-touch-icon.png",
                "/favicon.ico",
            }:
                icon_name = {
                    "/icons/icon-512.png": "icon-512.png",
                    "/apple-touch-icon.png": "apple-touch-icon.png",
                }.get(relative, "icon-192.png")
                self._serve_file(
                    PIYASA_DIR / "assets" / icon_name,
                    root=PIYASA_DIR,
                    kind="baraj",
                )
                return
            if relative == "/manifest.webmanifest":
                manifest = {
                    "name": "Baha Enerji | Baraj Aktif",
                    "short_name": "Baraj Aktif",
                    "lang": "tr",
                    "start_url": "/baraj/",
                    "scope": "/baraj/",
                    "display": "standalone",
                    "background_color": "#ffffff",
                    "theme_color": "#07539a",
                    "icons": [
                        {
                            "src": "/baraj/icons/icon-192.png",
                            "sizes": "192x192",
                            "type": "image/png",
                            "purpose": "any",
                        },
                        {
                            "src": "/baraj/icons/icon-512.png",
                            "sizes": "512x512",
                            "type": "image/png",
                            "purpose": "any",
                        },
                    ],
                }
                self._json(manifest)
                return
            if relative in {"", "/"}:
                self._serve_file(
                    BARAJ_DIR / "templates" / "index.html",
                    root=BARAJ_DIR,
                    prefix="/baraj",
                    kind="baraj",
                    inject_navigation=True,
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/uretim/"):
            if not self._require_page_session():
                return
            self._serve_module_static(
                path,
                prefix="/uretim",
                root=URETIM_DIR / "static",
                kind="uretim",
            )
            return
        if path.startswith("/tuketim/"):
            if not self._require_page_session():
                return
            relative = path[len("/tuketim") :]
            if relative in {"", "/"}:
                self._serve_file(
                    PORTAL_DIR / "consumption.html",
                    root=PORTAL_DIR,
                    prefix="/tuketim",
                    kind="tuketim",
                    inject_navigation=True,
                )
                return
            asset_name = relative.lstrip("/")
            if asset_name in {"consumption.css", "consumption.js"}:
                self._serve_file(
                    PORTAL_DIR / asset_name,
                    root=PORTAL_DIR,
                    prefix="/tuketim",
                    kind="tuketim",
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/sistem-yonu-tahmini/"):
            if not self._require_page_session():
                return
            relative = path[len("/sistem-yonu-tahmini") :]
            if relative in {"", "/"}:
                self._serve_file(
                    PORTAL_DIR / "system-direction-forecast.html",
                    root=PORTAL_DIR,
                    prefix="/sistem-yonu-tahmini",
                    kind="sistem",
                    inject_navigation=True,
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if path.startswith("/tv/"):
            if not self._require_page_session():
                return
            relative = path[len("/tv") :]
            asset_name = relative.lstrip("/") or "tv.html"
            if asset_name in {"tv.html", "tv.css", "tv.js"}:
                self._serve_file(
                    PORTAL_DIR / asset_name,
                    root=PORTAL_DIR,
                    kind="portal",
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if path == "/":
            if not self._session():
                self._redirect("/login")
                return
            self._redirect("/piyasa/")
            return
        if path == "/login":
            if self._session():
                self._redirect("/piyasa/")
                return
            self._serve_file(
                URETIM_DIR / "static" / "login.html",
                root=URETIM_DIR / "static",
                kind="portal",
                inject_navigation=False,
            )
            return
        if path == "/oturum-kapatildi":
            self._serve_file(
                PORTAL_DIR / "oturum-kapatildi.html",
                root=PORTAL_DIR,
                kind="portal",
                inject_navigation=False,
            )
            return
        if path == "/panel-hazirlaniyor":
            self._serve_file(
                PORTAL_DIR / "panel-loading.html",
                root=PORTAL_DIR,
                kind="portal",
                inject_navigation=False,
            )
            return
        if path == "/epias-koruma":
            if not self._require_page_session():
                return
            self._serve_file(
                PORTAL_DIR / "epias-guard.html",
                root=PORTAL_DIR,
                kind="portal",
                inject_navigation=True,
            )
            return
        if path == "/oturum-kapatildi.css":
            self._serve_file(
                PORTAL_DIR / "oturum-kapatildi.css",
                root=PORTAL_DIR,
                kind="portal",
            )
            return
        if path in {
            "/login.css",
            "/login.js",
            "/manifest.webmanifest",
            "/sw.js",
        }:
            self._serve_file(
                URETIM_DIR / "static" / path.lstrip("/"),
                root=URETIM_DIR / "static",
                kind="portal",
            )
            return
        suite_icons = {
            "/suite-assets/baha-logo.png": "baha-logo.png",
            "/suite-assets/icon-192.png": "icon-192.png",
            "/suite-assets/icon-512.png": "icon-512.png",
            "/suite-assets/apple-touch-icon.png": "apple-touch-icon.png",
            "/favicon.ico": "icon-192.png",
            "/apple-touch-icon.png": "apple-touch-icon.png",
        }
        if path in suite_icons:
            self._serve_file(
                PIYASA_DIR / "assets" / suite_icons[path],
                root=PIYASA_DIR,
                kind="portal",
            )
            return
        if path in {
            "/android-app.css",
            "/portal-shell.css",
            "/piyasa-suite.css",
            "/piyasa-charts.js",
            "/module-suite.css",
            "/module-suite.js",
            "/suite-loading.js",
            "/panel-loading.js",
            "/system-direction-forecast.css",
            "/system-direction-forecast.js",
            "/theme-sync.js",
            "/command-center.js",
            "/chart-fullscreen.css",
            "/chart-fullscreen.js",
            "/epias-guard.css",
            "/epias-guard.js",
            "/executive-report.css",
            "/executive-report.js",
        }:
            self._serve_file(
                PORTAL_DIR / path.lstrip("/"),
                root=PORTAL_DIR,
                kind="portal",
            )
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        login_paths = {
            "/api/login",
            "/piyasa/api/login",
            "/baraj/api/login",
            "/uretim/api/login",
            "/tuketim/api/login",
        }
        logout_paths = {
            "/api/logout",
            "/piyasa/api/logout",
            "/baraj/api/logout",
            "/uretim/api/logout",
            "/tuketim/api/logout",
        }
        if path in login_paths:
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            username = str(
                payload.get("username") or payload.get("email") or ""
            ).strip()
            password = str(payload.get("password") or "")
            if not username or not password:
                self._json(
                    {"error": "EPİAŞ e-posta adresinizi ve şifrenizi girin."},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            client_ip = self._client_ip()
            retry_after = LOGIN_LIMITER.retry_after(client_ip)
            if retry_after:
                self._login_rate_limited(retry_after)
                return
            username_key = f"username:{username.casefold()}"
            retry_after = LOGIN_USERNAME_LIMITER.retry_after(username_key)
            if retry_after:
                self._login_rate_limited(retry_after)
                return
            client = URETIM.EpiasClient(username=username, password=password)
            try:
                tgt = client.get_tgt()
            except URETIM.EpiasError as exc:
                if exc.status_code in {
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                }:
                    retry_after = max(
                        LOGIN_LIMITER.record_failure(client_ip),
                        LOGIN_USERNAME_LIMITER.record_failure(username_key),
                    )
                    if retry_after:
                        self._login_rate_limited(retry_after)
                        return
                    status = HTTPStatus.UNAUTHORIZED
                    error = "EPİAŞ e-posta adresi veya şifresi hatalı."
                else:
                    status = HTTPStatus.BAD_GATEWAY
                    error = "EPİAŞ giriş servisine şu anda ulaşılamıyor."
                self._json({"error": error}, status)
                return
            finally:
                client.password = ""
            LOGIN_LIMITER.reset(client_ip)
            LOGIN_USERNAME_LIMITER.reset(username_key)
            token = AUTH.create_session(username, tgt)
            self._json(
                {
                    "ok": True,
                    "authenticated": True,
                    "username": username,
                    "email": username,
                    "name": username,
                },
                headers={
                    "Set-Cookie": AUTH.cookie_header(
                        token, secure_request=self._secure_request()
                    )
                },
            )
            return
        if path in logout_paths:
            AUTH.revoke(self._session_token())
            self._json(
                {"ok": True, "authenticated": False},
                headers={
                    "Set-Cookie": AUTH.clear_cookie_header(
                        secure_request=self._secure_request()
                    )
                },
            )
            return
        self._json({"error": "Uç nokta bulunamadı."}, HTTPStatus.NOT_FOUND)


def run_server(host: str, port: int) -> None:
    required = (
        URETIM_DIR / "main.py",
        URETIM_DIR / "static",
        PIYASA_DIR / "index.html",
        BARAJ_DIR / "templates" / "index.html",
        PORTAL_DIR / "portal-shell.css",
        PORTAL_DIR / "android-app.css",
        PORTAL_DIR / "consumption.html",
        PORTAL_DIR / "consumption.css",
        PORTAL_DIR / "consumption.js",
        PORTAL_DIR / "system-direction-forecast.html",
        PORTAL_DIR / "system-direction-forecast.css",
        PORTAL_DIR / "system-direction-forecast.js",
        PORTAL_DIR / "chart-fullscreen.css",
        PORTAL_DIR / "chart-fullscreen.js",
        PORTAL_DIR / "suite-loading.js",
        PORTAL_DIR / "panel-loading.html",
        PORTAL_DIR / "panel-loading.js",
        PORTAL_DIR / "epias-guard.html",
        PORTAL_DIR / "epias-guard.css",
        PORTAL_DIR / "epias-guard.js",
        PORTAL_DIR / "oturum-kapatildi.html",
        PORTAL_DIR / "oturum-kapatildi.css",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Eksik proje dosyaları:\n- " + "\n- ".join(missing))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    BACKGROUND_REFRESH.start()
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"Baha Enerji Web Sitesi: http://{browser_host}:{port}")
    print("Modüller: Piyasa · Baraj Aktif · UEVM/UEÇM · Tüketim")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu durduruldu.")
    finally:
        BACKGROUND_REFRESH.stop()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Baha Enerji birleşik web sitesi")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    arguments = parser.parse_args()
    run_server(arguments.host, arguments.port)
