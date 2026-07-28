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
import importlib.util
import io
import json
import math
import mimetypes
import os
import posixpath
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape as xml_escape


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
COMPRESSIBLE_CONTENT_TYPES = {
    "application/geo+json",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
}
GZIP_MIN_BYTES = 1024


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
            payloads[label] = client._post_json(endpoint, body)
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
            payload = client._post_json(endpoint, body)
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
        direction_payload = client._post_json(
            "/v1/markets/bpm/data/system-direction", body
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
            return client._post_json(endpoint, body)
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

    response = client._post_json(
        "/v1/consumption/data/realtime-consumption",
        {
            "startDate": f"{selected_date}T00:00:00+03:00",
            "endDate": f"{selected_date}T00:00:00+03:00",
            "page": {"number": 1, "size": 100},
        },
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

    response = client._post_json(
        "/v1/consumption/data/realtime-consumption",
        {
            "startDate": f"{training_start.isoformat()}T00:00:00+03:00",
            "endDate": f"{training_end.isoformat()}T00:00:00+03:00",
            "page": {"number": 1, "size": 500},
        },
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

    payload = client._post_json(
        "/v1/dams/data/active-fullness",
        {"page": {"number": 1, "size": 500}},
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
            body_shell = _suite_navigation(kind)
            if kind in {"baraj", "uretim", "tuketim"}:
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
            if kind in {"piyasa", "baraj", "tuketim"}:
                text = text.replace(
                    "</body>",
                    f"{_suite_footer(kind)}</body>",
                    1,
                )
            if kind in {"baraj", "uretim", "tuketim"}:
                text = text.replace(
                    "</body>",
                    f'<script src="{asset_url("/module-suite.js")}" defer></script></body>',
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
                    "modules": ["piyasa", "baraj", "uretim", "tuketim"],
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
                self.send_response(HTTPStatus.OK)
                self.send_header(
                    "Content-Type",
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet",
                )
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
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

        if path in {"/piyasa", "/baraj", "/uretim", "/tuketim", "/tv"}:
            self._redirect(path + "/")
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
            "/portal-shell.css",
            "/piyasa-suite.css",
            "/piyasa-charts.js",
            "/module-suite.css",
            "/module-suite.js",
            "/theme-sync.js",
            "/command-center.js",
            "/chart-fullscreen.css",
            "/chart-fullscreen.js",
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
            client = URETIM.EpiasClient(username=username, password=password)
            try:
                tgt = client.get_tgt()
            except URETIM.EpiasError as exc:
                if exc.status_code in {
                    HTTPStatus.BAD_REQUEST,
                    HTTPStatus.UNAUTHORIZED,
                    HTTPStatus.FORBIDDEN,
                }:
                    retry_after = LOGIN_LIMITER.record_failure(client_ip)
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
        PORTAL_DIR / "consumption.html",
        PORTAL_DIR / "consumption.css",
        PORTAL_DIR / "consumption.js",
        PORTAL_DIR / "chart-fullscreen.css",
        PORTAL_DIR / "chart-fullscreen.js",
        PORTAL_DIR / "oturum-kapatildi.html",
        PORTAL_DIR / "oturum-kapatildi.css",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("Eksik proje dosyaları:\n- " + "\n- ".join(missing))
    server = ThreadingHTTPServer((host, port), RequestHandler)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    print(f"Baha Enerji Web Sitesi: http://{browser_host}:{port}")
    print("Modüller: Piyasa · Baraj Aktif · UEVM/UEÇM · Tüketim")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSunucu durduruldu.")
    finally:
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Baha Enerji birleşik web sitesi")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    arguments = parser.parse_args()
    run_server(arguments.host, arguments.port)
