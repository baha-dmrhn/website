"""HTML/path helpers for serving legacy modules inside the unified shell."""

from __future__ import annotations

import re

from .config import asset_url


ROOT_PATH_LITERAL = re.compile(r"""(["'`])/(?!/|>)([^"'`\s)>]*)""")

ROOT_SHARED_PATH_PREFIXES = (
    "/login",
    "/oturum-kapatildi",
    "/suite-assets/",
    "/favicon.ico",
    "/piyasa-charts.js",
    "/portal-shell.css",
    "/chart-fullscreen.css",
    "/chart-fullscreen.js",
    "/theme-sync.js",
    "/command-center.js",
    "/piyasa-suite.css",
    "/module-suite.css",
    "/module-suite.js",
    "/executive-report.css",
    "/executive-report.js",
)

SUITE_FAVICON_LINKS = (
    '<link rel="icon" type="image/png" sizes="192x192" '
    f'href="{asset_url("/suite-assets/icon-192.png")}">'
    '<link rel="shortcut icon" type="image/png" '
    f'href="{asset_url("/favicon.ico")}">'
    '<link rel="apple-touch-icon" sizes="180x180" '
    f'href="{asset_url("/suite-assets/apple-touch-icon.png")}">'
)


def rewrite_paths(content: str, prefix: str) -> str:
    """Move root-relative module paths under a module prefix.

    Shared suite assets stay rooted at the main site because all modules use the
    same shell, chart engine and icons.
    """

    def replace(match: re.Match[str]) -> str:
        quote = match.group(1)
        path = "/" + match.group(2)
        if any(path.startswith(shared) for shared in ROOT_SHARED_PATH_PREFIXES):
            return f"{quote}{path}"
        return f"{quote}{prefix}{path}"

    return ROOT_PATH_LITERAL.sub(replace, content)
