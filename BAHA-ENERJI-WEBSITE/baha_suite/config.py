"""Central asset versions for the unified Baha Enerji site."""

from __future__ import annotations


ASSET_VERSIONS: dict[str, int] = {
    "/piyasa/app.js": 58,
    "/piyasa/styles.css": 37,
    "/piyasa-charts.js": 19,
    "/portal-shell.css": 6,
    "/chart-fullscreen.css": 5,
    "/chart-fullscreen.js": 5,
    "/suite-loading.js": 1,
    "/theme-sync.js": 2,
    "/command-center.js": 2,
    "/piyasa-suite.css": 33,
    "/module-suite.css": 45,
    "/module-suite.js": 12,
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
