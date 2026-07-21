"""Core EPİAŞ UEVM / UEÇM services used by the unified Baha Enerji site.

The module intentionally uses only Python's standard library and is imported
by ``BAHA-ENERJI-WEBSITE/app.py``. EPİAŞ passwords are exchanged for temporary
TGT values and are never kept by the application.
"""

from __future__ import annotations

import io
import json
import math
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parent
TR_TZ = timezone(timedelta(hours=3))
MAX_RANGE_DAYS = 62


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
                else float(os.getenv("BAHA_URETIM_SESSION_MINUTES", "100"))
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
        self._tgt_expires_at = time.monotonic() + (105 * 60) if tgt else 0.0
        self._token_lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._tgt or (self.username and self.password))

    def _request(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                exc.read()
                raise EpiasError(
                    "EPİAŞ istek sınırına ulaşıldı. Kayıtlı dönemler önbellekten "
                    "gösterilmeye devam edecek; yeni veri için yaklaşık bir dakika "
                    "sonra tekrar deneyin.",
                    status_code=exc.code,
                ) from exc
            detail = exc.read().decode("utf-8", errors="replace")[:400]
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
            # The official documentation states two hours; refresh early.
            self._tgt_expires_at = now + (105 * 60)
            return token

    def _post_json(
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
                return self._post_json(endpoint, payload, retry_auth=False)
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
            ("Kaynak", 1),
            ("Ana grup", 1),
            ("UEVM (MWh)", 1),
            ("Pay", 1),
        ]
    ]
    source_rows.extend(
        [
            (source["label"], 0),
            (group_labels[source["group"]], 0),
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
        uevm = client.fetch_uevm(effective_range)
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
                probe_items = client.fetch_uevm(probe)
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
            uevm = client.fetch_uevm(effective_range)
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

        uecm = client.fetch_uecm(effective_range)
        rows = normalize_epias_data(uevm, uecm)
        payload = build_dashboard(
            rows,
            effective_range,
            warning=warning,
        )
        self._store_cached(date_range, payload)
        return payload
