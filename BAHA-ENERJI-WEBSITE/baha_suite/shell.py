"""Shared navigation, sidebars and footer for the Baha Enerji modules."""

from __future__ import annotations


def suite_navigation(active: str = "") -> str:
    links = (
        ("piyasa", "/piyasa/", "Piyasa"),
        ("baraj", "/baraj/", "Baraj Aktif"),
        ("uretim", "/uretim/", "UEVM · UEÇM"),
        ("tuketim", "/tuketim/", "Tüketim"),
        ("sistem", "/sistem-yonu-tahmini/", "Sistem Yönü Tahmini"),
    )
    anchor_parts = []
    for key, href, label in links:
        css_class = ' class="suite-nav-forecast"' if key == "sistem" else ""
        current = ' aria-current="page"' if key == active else ""
        title = ' title="Sistem Yönü Tahmini"' if key == "sistem" else ""
        aria_label = ' aria-label="Sistem Yönü Tahmini"' if key == "sistem" else ""
        anchor_parts.append(
            f'<a href="{href}"{css_class}{current}{title}{aria_label}>{label}</a>'
        )
    anchors = "".join(anchor_parts)
    return (
        '<nav class="baha-suite-nav" aria-label="Baha Enerji modülleri">'
        f'{anchors}<span class="suite-nav-divider" aria-hidden="true"></span>'
        '<a class="suite-nav-tool suite-nav-tv" href="/tv/" title="Tam ekran TV modu">'
        '<span aria-hidden="true">▣</span> TV</a>'
        '<a class="suite-nav-tool suite-nav-report" data-suite-report-link href="/rapor" '
        'title="Günlük yönetici raporu"><span aria-hidden="true">↓</span> Rapor</a>'
        '<a class="suite-nav-tool" href="/epias-koruma" '
        'title="EPİAŞ limit ve koruma durumu"><span aria-hidden="true">●</span> Koruma</a>'
        '<button class="suite-command-toggle" type="button" '
        'aria-expanded="false" aria-controls="suiteCommandMenu">'
        '<span aria-hidden="true">▦</span><b>Komuta</b></button>'
        '<div class="suite-command-menu" id="suiteCommandMenu">'
        '<span>Komuta merkezi</span>'
        '<a href="/tv/"><i aria-hidden="true">▣</i><b>TV modu</b></a>'
        '<a data-suite-report-link href="/rapor"><i aria-hidden="true">↓</i>'
        '<b>Günlük rapor</b></a>'
        '<a href="/epias-koruma"><i aria-hidden="true">●</i>'
        '<b>EPİAŞ koruma</b></a></div>'
        "</nav>"
    )


def suite_footer(kind: str) -> str:
    updated_ids = {
        "piyasa": "piyasaFooterUpdated",
        "baraj": "barajFooterUpdated",
        "uretim": "updatedAt",
        "tuketim": "consumptionFooterUpdated",
    }
    updated_id = updated_ids.get(kind, "suiteFooterUpdated")
    return (
        f'<footer class="suite-footer" data-suite-footer="{kind}">'
        '<div class="suite-footer-brand">BAHA<br>ENERJ&#304;<span>↗</span></div>'
        "<div>"
        "<strong>Veri kayna&#287;&#305;</strong>"
        '<a href="https://seffaflik.epias.com.tr/" target="_blank" '
        'rel="noreferrer noopener">EP&#304;A&#350; '
        '&#350;effafl&#305;k Platformu</a>'
        "</div>"
        "<div>"
        "<strong>Son g&#252;ncelleme</strong>"
        f'<span id="{updated_id}">—</span>'
        "</div>"
        "<p>Veriyi sadele&#351;tirir.<br>"
        "Anlam&#305;n&#305; de&#287;i&#351;tirmez.</p>"
        "</footer>"
    )


def module_sidebar(kind: str) -> str:
    if kind == "baraj":
        panel_name = "Baraj Aktif"
        links = (
            ("#dashboard", "⌁", "Genel Bakış"),
            ("#baraj-summary", "◷", "Doluluk Özeti"),
            ("#baraj-compare", "⇄", "Karşılaştır"),
            ("#basin-risk", "!", "Havza Riskleri"),
            ("#baraj-map", "◎", "Havza Haritası"),
            ("#baraj-regime-analysis", "∿", "Havza Rejimi"),
            ("#baraj-list", "≡", "Baraj Listesi"),
        )
    elif kind == "uretim":
        panel_name = "Üretim Paneli"
        links = (
            ("#main", "⌁", "Genel Bakış"),
            ("#overviewTitle", "◷", "Sistem Özeti"),
            ("#trendTitle", "⌁", "Saatlik Grafik"),
            ("#detailsTitle", "≡", "Detaylı Veri"),
        )
    elif kind == "sistem":
        panel_name = "Sistem Yönü Tahmini"
        links = (
            ("#systemForecastPage", "⌁", "Genel Bakış"),
            ("#system-summary", "◷", "Tahmin Özeti"),
            ("#system-timeline", "⌁", "Zaman Şeridi"),
            ("#system-probability", "⇄", "Olasılıklar"),
            ("#system-method", "?", "Metot"),
            ("#system-samples", "≡", "Referans Günler"),
            ("#system-validation", "✓", "Doğrulama"),
        )
    else:
        panel_name = "Tüketim Paneli"
        links = (
            ("#consumption-top", "⌁", "Genel Bakış"),
            ("#consumption-summary", "◷", "Günlük Özet"),
            ("#consumption-chart", "⌁", "Saatlik Grafik"),
            ("#consumption-table", "≡", "Detaylı Veri"),
        )
    page_href = {
        "baraj": "/baraj/",
        "uretim": "/uretim/",
        "tuketim": "/tuketim/",
        "sistem": "/sistem-yonu-tahmini/",
    }.get(kind, "/")
    anchors = "".join(
        (
            f'<a href="{page_href if href.startswith("#") else href}"'
            f'{" data-suite-scroll=\"" + href + "\"" if href.startswith("#") else ""}'
            f'{" class=\"active\"" if index == 0 else ""}>'
            f'<span class="suite-side-icon" aria-hidden="true">{icon}</span>'
            f"<span>{label}</span></a>"
        )
        for index, (href, icon, label) in enumerate(links)
    )
    return (
        '<button class="suite-menu-button" type="button" '
        'aria-label="Menüyü aç" aria-expanded="false">☰</button>'
        '<aside class="suite-sidebar">'
        '<button class="suite-menu-close" type="button" '
        'aria-label="Menüyü kapat">×</button>'
        '<div class="suite-side-brand">'
        '<span class="suite-side-logo"><img src="/suite-assets/baha-logo.png" '
        'alt="Baha Enerji"></span>'
        "<div>Baha Enerji</div></div>"
        f'<nav aria-label="{panel_name} bölümleri">{anchors}</nav>'
        '<div class="suite-side-bottom">'
        '<div class="suite-live-dot"><i></i>'
        '<span>EPİAŞ · EPİAŞ canlı</span></div>'
        '<button class="suite-logout-button" type="button">Oturumu kapat</button>'
        "</div></aside>"
        '<button class="suite-sidebar-overlay" type="button" '
        'aria-label="Menüyü kapat"></button>'
        '<button id="sidebar-lock" class="suite-sidebar-lock-button sidebar-lock-button" type="button" '
        'aria-label="Yan menüyü sabitle" aria-pressed="false">'
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M7 11V8a5 5 0 0 1 10 0v3"></path>'
        '<rect x="5" y="11" width="14" height="10" rx="2"></rect>'
        '</svg></button>'
        '<div class="suite-header-actions">'
        '<button class="suite-theme-toggle" data-suite-theme-toggle '
        'type="button" aria-label="Koyu temaya geç">☾</button>'
        '<div class="suite-account-pill" aria-label="Oturum kullanıcısı">'
        '<span data-suite-user-initial>B</span>'
        '<b data-suite-user-email>Baha Enerji Kullanıcısı</b></div></div>'
    )
