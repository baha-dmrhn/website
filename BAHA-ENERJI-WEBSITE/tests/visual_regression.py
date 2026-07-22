"""Gerçek tarayıcıyla telefon, iPad ve masaüstü ekran görüntüsü testleri.

İlk kullanımda referansları üretmek için:
    python tests/visual_regression.py --update

Sonraki kontrollerde:
    python tests/visual_regression.py
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageChops, ImageStat


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("baha_visual_app", ROOT / "app.py")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)
TODAY = "2026-07-20"


def market_payload() -> dict:
    rows = []
    for hour in range(24):
        ptf = 2800 + math.sin(hour / 3) * 640 + hour * 38
        smf = ptf + math.cos(hour / 2) * 220
        rows.append(
            {
                "hour": hour,
                "time": f"{hour:02d}:00",
                "ptf": round(ptf, 2),
                "smf": round(smf, 2),
                "ptfByCurrency": {
                    "TRY": round(ptf, 2),
                    "EUR": round(ptf / 48, 2),
                    "USD": round(ptf / 41, 2),
                },
                "smfByCurrency": {"TRY": round(smf, 2)},
                "yal": round(max(0, math.sin(hour / 2) * 460), 2),
                "yat": round(-max(0, math.cos(hour / 3) * 520), 2),
                "direction": "Enerji Açığı" if hour < 8 else "Enerji Fazlası",
            }
        )
    average = sum(row["ptf"] for row in rows) / len(rows)
    return {
        "date": TODAY,
        "rows": rows,
        "summary": {
            "ptfAverage": average,
            "ptfAverageByCurrency": {"TRY": average, "EUR": average / 48, "USD": average / 41},
            "smfAverage": sum(row["smf"] for row in rows) / len(rows),
            "yalTotal": sum(row["yal"] for row in rows),
            "yatTotal": sum(abs(row["yat"]) for row in rows),
            "ptfSmfCommonHours": 24,
        },
        "currencyInfo": {
            "mode": "epias-ptf-direct",
            "available": ["TRY", "EUR", "USD"],
        },
        "validation": {},
        "warnings": [],
        "updatedAt": "2026-07-20T14:00:00Z",
        "cached": False,
    }


def next_day_payload() -> dict:
    rows = []
    for hour in range(24):
        value = 3200 + math.sin(hour / 4) * 500
        rows.append(
            {
                "hour": hour,
                "time": f"{hour:02d}:00",
                "ptf": value,
                "ptfByCurrency": {"TRY": value, "EUR": value / 48, "USD": value / 41},
            }
        )
    return {
        "date": "2026-07-21",
        "published": True,
        "publication": {
            "status": "final",
            "label": "Kesinleşmiş PTF",
            "nextRefreshAt": None,
        },
        "rows": rows,
        "summary": {
            "publishedHours": 24,
            "ptfAverageByCurrency": {"TRY": 3200, "EUR": 66.67, "USD": 78.05},
        },
    }


def production_payload() -> dict:
    source_cards = [
        ("sun", "Güneş", 1_335_710, 16.5),
        ("wind", "Rüzgâr", 1_381_757, 17.1),
        ("hydro", "Hidroelektrik", 2_640_021, 32.6),
        ("thermal", "Termik", 1_967_382, 24.3),
        ("natural_gas", "Doğal gaz", 391_754, 4.8),
    ]
    groups = [
        {"id": "renewable", "label": "Yenilenebilir", "value": 5_357_488, "share": 69.8, "sources": ["Güneş", "Rüzgâr", "Hidroelektrik"]},
        {"id": "thermal", "label": "Termik", "value": 1_850_000, "share": 24.1, "sources": ["Kömür", "Linyit"]},
        {"id": "natural_gas", "label": "Doğal gaz", "value": 370_000, "share": 4.8, "sources": ["Doğal gaz"]},
        {"id": "other", "label": "Diğer / Uluslararası", "value": 99_800, "share": 1.3, "sources": ["Diğer"]},
    ]
    series = []
    for hour in range(24):
        uevm = 36_000 + math.sin(hour / 4) * 5_500
        series.append(
            {
                "timestamp": f"2026-07-20T{hour:02d}:00:00+03:00",
                "uevm": uevm,
                "uecm": uevm * 0.995,
                "sun": max(0, math.sin((hour - 6) / 12 * math.pi)) * 9000,
                "wind": 5000 + math.cos(hour / 5) * 900,
                "hydro": 9000 + math.sin(hour / 3) * 1200,
                "thermal": 11000,
                "naturalGas": 3500,
            }
        )
    return {
        "meta": {
            "warning": None,
            "generatedAt": "2026-07-20T14:00:00+03:00",
            "latestAvailableDate": TODAY,
            "methodology": "Görsel test verisi",
        },
        "period": {"start": TODAY, "end": TODAY, "hours": 24, "comparableHours": 24, "uevmHours": 24},
        "summary": {"uevmTotal": 8_156_056, "uecmTotal": 8_116_772, "difference": 39_284, "deviationPct": 0.49, "hourlyAverage": 42_185},
        "series": series,
        "sourceCards": [{"id": item[0], "label": item[1], "value": item[2], "share": item[3]} for item in source_cards],
        "groups": groups,
        "sources": [
            {"group": "renewable", "label": "Güneş", "value": 1_335_710, "share": 16.5},
            {"group": "renewable", "label": "Rüzgâr", "value": 1_381_757, "share": 17.1},
            {"group": "renewable", "label": "Barajlı", "value": 2_010_000, "share": 24.7},
            {"group": "thermal", "label": "İthal kömür", "value": 1_250_000, "share": 15.4},
            {"group": "natural_gas", "label": "Doğal gaz", "value": 391_754, "share": 4.8},
        ],
    }


def reservoir_payload() -> dict:
    items = [
        {"dam": "Gölova", "basin": "Yeşilırmak", "activeFullnessAmount": 91.2, "date": TODAY},
        {"dam": "Kesikköprü", "basin": "Kızılırmak", "activeFullnessAmount": 78.4, "date": TODAY},
        {"dam": "Atatürk", "basin": "Fırat - Dicle", "activeFullnessAmount": 67.8, "date": TODAY},
        {"dam": "Seyhan", "basin": "Seyhan", "activeFullnessAmount": 54.6, "date": TODAY},
        {"dam": "Oymapınar", "basin": "Antalya", "activeFullnessAmount": 43.1, "date": TODAY},
    ]
    return {"items": items, "selectedDate": TODAY, "availableDates": [TODAY, "2026-07-19"], "archiveDates": ["2026-07-19"]}


def reservoir_history_payload() -> dict:
    basins = []
    for name, dam in (("Yeşilırmak", "Gölova"), ("Kızılırmak", "Kesikköprü"), ("Seyhan", "Seyhan")):
        points = [{"date": f"2026-07-{day:02d}", "average": 48 + day * 1.7, "damCount": 1} for day in range(15, 21)]
        dam_points = [{"date": point["date"], "activeFullnessAmount": point["average"], "source": "EPİAŞ"} for point in points]
        basins.append({"name": name, "points": points, "analysis": {"regime": "Dengeli", "trendStart": 72, "trendEnd": 76}, "dams": [{"name": dam, "points": dam_points}]})
    return {"startDate": "2026-07-15", "endDate": TODAY, "basins": basins}


def consumption_payload() -> dict:
    rows = []
    for hour in range(24):
        value = 42_000 + math.sin(hour / 4) * 7000
        rows.append({"hour": hour, "time": f"{hour:02d}:00", "consumption": value, "timestamp": f"2026-07-20T{hour:02d}:00:00+03:00"})
    values = [row["consumption"] for row in rows]
    return {
        "date": TODAY,
        "rows": rows,
        "summary": {"latest": values[-1], "latestHour": "23:00", "latestChange": values[-1] - values[-2], "latestChangePercent": 1.2, "average": sum(values) / 24, "maximum": max(values), "maximumHour": "06:00", "minimum": min(values), "minimumHour": "18:00", "total": sum(values), "availableHours": 24, "missingHours": 0},
        "updatedAt": "2026-07-20T14:00:00Z",
        "cached": False,
    }


class VisualHandler(APP.RequestHandler):
    visual_token = ""

    def log_message(self, _fmt: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/__visual_boot":
            query = urllib.parse.parse_qs(parsed.query)
            theme = (query.get("theme") or ["light"])[0]
            target = (query.get("next") or ["/piyasa/"])[0]
            script = (
                "<!doctype html><meta charset='utf-8'><script>"
                f"localStorage.setItem('baha-theme',{json.dumps(theme)});"
                f"location.replace({json.dumps(target)});</script>"
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(script)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Set-Cookie", APP.AUTH.cookie_header(self.visual_token, secure_request=False))
            self.end_headers()
            self.wfile.write(script)
            return
        if path in {"/api/session", "/piyasa/api/session"}:
            session = self._session()
            username = session.username if session else None
            self._json(
                {
                    "authenticated": bool(session),
                    "username": username,
                    "email": username,
                    "name": username,
                },
                HTTPStatus.OK if session else HTTPStatus.UNAUTHORIZED,
            )
            return
        fixtures = {
            "/piyasa/api/data": market_payload,
            "/piyasa/api/next-day-ptf": next_day_payload,
            "/uretim/api/dashboard": production_payload,
            "/baraj/api/active-fullness": reservoir_payload,
            "/baraj/api/basin-history": reservoir_history_payload,
            "/tuketim/api/data": consumption_payload,
        }
        factory = fixtures.get(path)
        if factory:
            self._json(factory())
            return
        super().do_GET()


SCENARIOS = (
    ("piyasa-phone-dark", "/piyasa/", "dark", 390, 844, True),
    ("baraj-ipad", "/baraj/", "light", 820, 1180, True),
    ("uretim-ipad-dark", "/uretim/", "dark", 1024, 768, True),
    ("uretim-phone", "/uretim/", "light", 390, 844, True),
    ("uretim-source-cards-dark", "/uretim/", "dark", 1365, 1800, True),
    ("tuketim-desktop-dark", "/tuketim/", "dark", 1440, 1000, True),
    ("login-phone", "/login", "light", 390, 844, False),
)


def browser_binary() -> Path:
    configured = os.getenv("CHROME_BINARY")
    candidates = [
        configured,
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("Chrome/Edge bulunamadı. CHROME_BINARY ortam değişkenini ayarlayın.")


def image_difference(actual: Path, baseline: Path) -> float:
    with Image.open(actual).convert("RGB") as current, Image.open(baseline).convert("RGB") as expected:
        if current.size != expected.size:
            return 1.0
        difference = ImageChops.difference(current, expected)
        return sum(ImageStat.Stat(difference).mean) / (3 * 255)


def image_variance(path: Path) -> float:
    """Reject screenshots that only contain a flat page background."""

    with Image.open(path).convert("RGB") as image:
        return sum(ImageStat.Stat(image).var) / 3


def run_browser(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run an isolated browser and also stop its child processes on timeout."""

    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.kill()
        stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(command, 124, stdout, stderr + "\nTarayici zaman asimina ugradi.")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--update", action="store_true", help="Referans ekran görüntülerini güncelle")
    parser.add_argument("--threshold", type=float, default=0.035, help="İzin verilen ortalama piksel farkı")
    args = parser.parse_args()
    baseline_dir = Path(__file__).with_name("visual-baselines")
    output_dir = Path(__file__).with_name("visual-output")
    baseline_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    for stale_profile in output_dir.glob(".profile-*"):
        shutil.rmtree(stale_profile, ignore_errors=True)
    token = APP.AUTH.create_session("visual@baha.local", "TGT-visual")
    VisualHandler.visual_token = token
    server = ThreadingHTTPServer(("127.0.0.1", 0), VisualHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    browser = browser_binary()
    failures = []
    try:
        for name, target, theme, width, height, authenticated in SCENARIOS:
            actual = output_dir / f"{name}.png"
            actual.unlink(missing_ok=True)
            url = target
            if authenticated:
                url = "/__visual_boot?" + urllib.parse.urlencode({"theme": theme, "next": target})
            profile = output_dir / f".profile-{os.getpid()}-{name}"
            profile.mkdir(parents=True, exist_ok=True)
            command = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--disable-background-mode",
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-extensions",
                "--disable-sync",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-service-autorun",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "--virtual-time-budget=5500",
                f"--screenshot={actual}",
                base_url + url,
            ]
            result = run_browser(command)
            shutil.rmtree(profile, ignore_errors=True)
            if not actual.is_file():
                failures.append(f"{name}: tarayıcı ekran görüntüsü üretmedi ({result.stderr.strip()})")
                continue
            with Image.open(actual) as screenshot:
                if screenshot.size != (width, height):
                    failures.append(f"{name}: beklenen {(width, height)}, oluşan {screenshot.size}")
            if image_variance(actual) < 10:
                failures.append(f"{name}: ekran goruntusu yalnizca duz arka plan iceriyor")
                continue
            baseline = baseline_dir / actual.name
            if args.update or not baseline.exists():
                shutil.copy2(actual, baseline)
                print(f"REFERANS: {name}")
                continue
            difference = image_difference(actual, baseline)
            print(f"{name}: fark %{difference * 100:.2f}")
            if difference > args.threshold:
                failures.append(f"{name}: görsel fark %{difference * 100:.2f} > %{args.threshold * 100:.2f}")
    finally:
        APP.AUTH.revoke(token)
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    if failures:
        print("\n".join(f"HATA: {failure}" for failure in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
