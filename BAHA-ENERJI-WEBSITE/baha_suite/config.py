"""Central asset versions for the unified Baha Enerji site."""

from __future__ import annotations


ASSET_VERSIONS: dict[str, int] = {
    "/android-app.css": 12,
    "/piyasa/app.js": 60,
    "/piyasa/styles.css": 37,
    "/piyasa-charts.js": 19,
    "/portal-shell.css": 7,
    "/chart-fullscreen.css": 5,
    "/chart-fullscreen.js": 5,
    "/suite-loading.js": 3,
    "/theme-sync.js": 3,
    "/command-center.js": 2,
    "/piyasa-suite.css": 34,
    "/system-direction-forecast.css": 21,
    "/system-direction-forecast.js": 21,
    "/module-suite.css": 54,
    "/module-suite.js": 14,
    "/executive-report.css": 3,
    "/executive-report.js": 1,
    "/suite-assets/icon-192.png": 2,
    "/suite-assets/icon-512.png": 2,
    "/suite-assets/apple-touch-icon.png": 2,
    "/favicon.ico": 2,
}

def asset_url(path: str) -> str:
    """Return a cache-busting URL for a known static asset."""

    version = ASSET_VERSIONS.get(path)
    return f"{path}?v={version}" if version is not None else path
