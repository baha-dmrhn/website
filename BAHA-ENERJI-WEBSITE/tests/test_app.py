import importlib.util
import http.client
import json
import io
import sys
import threading
import unittest
import urllib.request
import zipfile
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("baha_suite_test_app", ROOT / "app.py")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class SuiteHelpersTests(unittest.TestCase):
    def test_rewrites_only_root_relative_string_paths(self):
        source = (
            'fetch("/api/session"); const external = "https://example.com/x"; '
            'const relative = "assets/logo.png";'
        )
        result = APP._rewrite_paths(source, "/piyasa")
        self.assertIn('fetch("/piyasa/api/session")', result)
        self.assertIn('"https://example.com/x"', result)
        self.assertIn('"assets/logo.png"', result)

    def test_rewrite_paths_keeps_shared_suite_assets_at_the_site_root(self):
        source = '"/suite-assets/icon-192.png"'
        self.assertEqual(APP._rewrite_paths(source, "/uretim"), source)

    def test_extracts_nested_epias_items(self):
        payload = {"body": {"items": [{"hour": 1}, {"hour": 2}]}}
        self.assertEqual(len(APP._items(payload)), 2)

    def test_hour_key_accepts_iso_and_market_hour(self):
        self.assertEqual(APP._hour_key({"hour": "2026-07-20T04:00:00+03:00"}, 0), 4)
        self.assertEqual(APP._hour_key({"hour": 24}, 0), 23)
        self.assertEqual(APP._hour_key({"date": "2026-07-21T03:00:00+03:00"}, 0), 3)

    def test_suite_navigation_marks_active_module(self):
        navigation = APP._suite_navigation("baraj")
        self.assertIn('href="/baraj/" aria-current="page"', navigation)
        self.assertIn('href="/uretim/"', navigation)
        self.assertNotIn("Ana Sayfa", navigation)

    def test_module_sidebar_uses_matching_sections(self):
        baraj = APP._module_sidebar("baraj")
        uretim = APP._module_sidebar("uretim")
        self.assertIn('href="#baraj-summary"', baraj)
        self.assertIn('href="#baraj-regime"', baraj)
        self.assertIn('href="#baraj-list"', baraj)
        self.assertIn('href="#trendTitle"', uretim)
        self.assertIn('href="#detailsTitle"', uretim)
        self.assertIn('src="/suite-assets/baha-logo.png"', uretim)
        self.assertIn("EPİAŞ · EPİAŞ canlı", baraj)
        self.assertIn("EPİAŞ · EPİAŞ canlı", uretim)
        self.assertIn('class="suite-menu-close"', baraj)
        self.assertIn('class="suite-menu-close"', uretim)
        self.assertNotIn("<small>", baraj)
        self.assertNotIn("<small>", uretim)

    def test_market_dashboard_combines_price_quantity_and_direction(self):
        class StubClient:
            def _post_json(self, endpoint, body):
                if endpoint.endswith("/mcp"):
                    return {
                        "items": [
                            {
                                "hour": 1,
                                "price": 100,
                                "priceEur": 4,
                                "priceUsd": 5,
                            }
                        ],
                        "statistic": {"priceAvg": 100},
                    }
                if endpoint.endswith("/system-marginal-price"):
                    return {
                        "items": [{"hour": 1, "systemMarginalPrice": 110}],
                        "statistics": {"smpArithmeticalAverage": 110},
                    }
                if endpoint.endswith("/order-summary-up"):
                    return {
                        "items": [
                            {
                                "hour": 1,
                                "upRegulationZeroCoded": 1,
                                "upRegulationOneCoded": 2,
                                "upRegulationTwoCoded": 3,
                            }
                        ],
                        "statistics": {
                            "upRegulationZeroCodedTotal": 1,
                            "upRegulationOneCodedTotal": 2,
                            "upRegulationTwoCodedTotal": 3,
                        },
                    }
                if endpoint.endswith("/order-summary-down"):
                    return {
                        "items": [
                            {
                                "hour": 1,
                                "downRegulationZeroCoded": -2,
                                "downRegulationOneCoded": -3,
                                "downRegulationTwoCoded": -4,
                            }
                        ]
                    }
                if endpoint.endswith("/system-direction"):
                    return {
                        "items": [{"hour": 1, "systemDirection": "Enerji Açığı"}]
                    }
                raise AssertionError(endpoint)

        APP.MARKET_CACHE.pop("2026-07-19", None)
        dashboard = APP._market_dashboard("2026-07-19", StubClient())
        self.assertEqual(dashboard["summary"]["ptfAverage"], 100)
        self.assertEqual(dashboard["summary"]["smfAverage"], 110)
        self.assertEqual(dashboard["summary"]["yalTotal"], 6)
        self.assertEqual(dashboard["summary"]["yatTotal"], 9)
        self.assertEqual(dashboard["rows"][0]["time"], "01:00")
        self.assertEqual(dashboard["rows"][0]["direction"], "Enerji Açığı")
        self.assertEqual(
            dashboard["rows"][0]["ptfByCurrency"],
            {"TRY": 100, "EUR": 4, "USD": 5},
        )
        self.assertEqual(
            dashboard["rows"][0]["smfByCurrency"],
            {"TRY": 110},
        )
        self.assertEqual(
            dashboard["currencyInfo"]["available"], ["TRY", "EUR", "USD"]
        )
        self.assertEqual(
            dashboard["currencyInfo"]["mode"],
            "epias-ptf-direct",
        )
        self.assertEqual(dashboard["currencyInfo"]["appliesTo"], "PTF")
        self.assertNotIn("tryPerUnit", dashboard["currencyInfo"])
        self.assertEqual(
            dashboard["summary"]["smfAverageByCurrency"],
            {"TRY": 110},
        )

    def test_market_dashboard_does_not_derive_missing_hourly_currencies(self):
        class StubClient:
            def _post_json(self, endpoint, body):
                if endpoint.endswith("/mcp"):
                    return {
                        "items": [{"hour": 1, "price": 1_000}],
                        "statistic": {
                            "priceAvg": 1_000,
                            "priceEurAvg": 20,
                            "priceUsdAvg": 25,
                        },
                    }
                if endpoint.endswith("/system-marginal-price"):
                    return {
                        "items": [{"hour": 1, "systemMarginalPrice": 1_100}],
                        "statistics": {"smpArithmeticalAverage": 1_100},
                    }
                return {"items": []}

        APP.MARKET_CACHE.pop("2026-07-18", None)
        dashboard = APP._market_dashboard("2026-07-18", StubClient())
        self.assertEqual(
            dashboard["currencyInfo"]["available"],
            ["TRY", "EUR", "USD"],
        )
        self.assertIsNone(dashboard["rows"][0]["ptfByCurrency"]["EUR"])
        self.assertIsNone(dashboard["rows"][0]["ptfByCurrency"]["USD"])
        self.assertEqual(
            dashboard["summary"]["ptfAverageByCurrency"]["EUR"],
            20,
        )
        self.assertEqual(
            dashboard["summary"]["ptfAverageByCurrency"]["USD"],
            25,
        )
        self.assertEqual(
            dashboard["rows"][0]["smfByCurrency"],
            {"TRY": 1_100},
        )

    def test_market_force_refresh_bypasses_server_cache_and_keeps_smf_hour(self):
        selected_date = "2026-07-17"
        APP.MARKET_CACHE[selected_date] = {
            "payload": {"rows": [{"hour": 2, "smf": 99}]},
            "expires": APP.time.time() + 600,
        }

        class StubClient:
            calls = 0

            def _post_json(self, endpoint, body):
                self.calls += 1
                if endpoint.endswith("/system-marginal-price"):
                    return {
                        "items": [
                            {
                                "date": "2026-07-17T03:00:00+03:00",
                                "systemMarginalPrice": 222,
                            }
                        ]
                    }
                return {"items": []}

        client = StubClient()
        cached = APP._market_dashboard(selected_date, client)
        self.assertTrue(cached["cached"])
        self.assertEqual(client.calls, 0)

        fresh = APP._market_dashboard(
            selected_date,
            client,
            force_refresh=True,
        )
        self.assertGreater(client.calls, 0)
        self.assertFalse(fresh["cached"])
        self.assertEqual(fresh["rows"][0]["hour"], 3)
        self.assertEqual(fresh["rows"][0]["smf"], 222)

    def test_active_fullness_normalizes_epias_fields(self):
        class StubClient:
            def _post_json(self, endpoint, body):
                self.endpoint = endpoint
                self.body = body
                return {
                    "body": {
                        "items": [
                            {
                                "damName": "Örnek Barajı",
                                "basinName": "Örnek Havzası",
                                "activeFullnessAmount": 72.5,
                                "date": "2026-07-20T00:00:00+03:00",
                            }
                        ]
                    }
                }

        client = StubClient()
        payload = APP._active_fullness(client)
        self.assertEqual(client.endpoint, "/v1/dams/data/active-fullness")
        self.assertEqual(payload["items"][0]["dam"], "Örnek Barajı")
        self.assertEqual(payload["items"][0]["basin"], "Örnek Havzası")
        self.assertEqual(payload["availableDates"], ["2026-07-20"])

    def test_baraj_archive_reads_pivot_dates_and_raw_basin_mapping(self):
        archive = APP._load_baraj_archive(APP.BARAJ_ARCHIVE_XLSX)
        self.assertEqual(archive["availableDates"][0], "2026-06-24")
        self.assertGreaterEqual(archive["availableDates"][-1], "2026-07-17")
        self.assertEqual(
            len(archive["availableDates"]),
            len(archive["byDate"]),
        )
        self.assertEqual(
            archive["recordCount"],
            sum(len(items) for items in archive["byDate"].values()),
        )
        adatepe = next(
            item
            for item in archive["byDate"]["2026-06-24"]
            if item["dam"] == "ADATEPE"
        )
        self.assertEqual(adatepe["basin"], "Ceyhan")
        self.assertAlmostEqual(adatepe["activeFullnessAmount"], 71.828125)
        self.assertEqual(adatepe["source"], "excel")

    def test_baraj_archive_date_does_not_call_epias(self):
        class FailClient:
            def _post_json(self, endpoint, body):
                raise AssertionError("Arşiv tarihi EPİAŞ'a gitmemeli")

        payload = APP._baraj_data(FailClient(), "2026-07-17")
        self.assertEqual(payload["source"], "excel")
        self.assertEqual(payload["selectedDate"], "2026-07-17")
        self.assertEqual(payload["sourceLabel"], "Arşiv")
        self.assertGreater(len(payload["items"]), 0)

    def test_baraj_basin_history_merges_archive_and_live_regime(self):
        archive = APP._load_baraj_archive(APP.BARAJ_ARCHIVE_XLSX)
        latest_archive_date = date.fromisoformat(archive["availableDates"][-1])
        live_date = (latest_archive_date + timedelta(days=1)).isoformat()
        latest_archive_date_text = latest_archive_date.isoformat()
        archive_adatepe = next(
            item
            for item in archive["byDate"][latest_archive_date_text]
            if item["dam"] == "ADATEPE"
        )

        class StubClient:
            def _post_json(self, endpoint, body):
                self.endpoint = endpoint
                return {
                    "items": [
                        {
                            "dam": "ADATEPE",
                            "basin": "Ceyhan",
                            "activeFullnessAmount": 1,
                            "date": f"{latest_archive_date_text}T00:00:00+03:00",
                        },
                        {
                            "dam": "ADATEPE",
                            "basin": "Ceyhan",
                            "activeFullnessAmount": 65,
                            "date": f"{live_date}T00:00:00+03:00",
                        }
                    ]
                }

        payload = APP._baraj_basin_history(StubClient())
        ceyhan = next(
            basin for basin in payload["basins"] if basin["name"] == "Ceyhan"
        )
        self.assertEqual(payload["startDate"], "2026-06-24")
        self.assertEqual(payload["endDate"], live_date)
        self.assertEqual(ceyhan["points"][0]["date"], "2026-06-24")
        self.assertEqual(ceyhan["points"][-1]["date"], live_date)
        self.assertEqual(ceyhan["points"][-1]["average"], 65)
        adatepe = next(dam for dam in ceyhan["dams"] if dam["name"] == "ADATEPE")
        overlap_point = next(
            point
            for point in adatepe["points"]
            if point["date"] == latest_archive_date_text
        )
        self.assertEqual(
            overlap_point["activeFullnessAmount"],
            archive_adatepe["activeFullnessAmount"],
        )
        self.assertEqual(overlap_point["source"], "Arşiv")
        self.assertEqual(adatepe["points"][-1]["date"], live_date)
        self.assertEqual(adatepe["points"][-1]["activeFullnessAmount"], 65)
        self.assertEqual(adatepe["points"][-1]["source"], "EPİAŞ")
        self.assertIn(
            ceyhan["analysis"]["regime"],
            {"Azalan rejim", "Yükselen rejim", "Dengeli rejim"},
        )
        self.assertIn("doğrusal eğilim", payload["methodNote"])

    def test_baraj_basin_xlsx_contains_individual_dam_history(self):
        payload = {
            "startDate": "2026-06-24",
            "endDate": "2026-07-21",
            "basins": [
                {
                    "name": "Ceyhan",
                    "points": [
                        {
                            "date": "2026-07-21",
                            "average": 65,
                            "minimum": 60,
                            "maximum": 70,
                            "damCount": 2,
                        }
                    ],
                    "analysis": {
                        "regime": "Azalan rejim",
                        "slopePerDay": -0.2,
                    },
                    "dams": [
                        {
                            "name": "ADATEPE",
                            "points": [
                                {
                                    "date": "2026-07-21",
                                    "activeFullnessAmount": 65,
                                    "source": "EPİAŞ",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        workbook = APP._baraj_basin_xlsx(payload, "Ceyhan")
        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            detail_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn('name="Baraj Dolulukları"', workbook_xml)
            self.assertIn('name="Havza Ortalaması"', workbook_xml)
            self.assertIn("ADATEPE", detail_xml)
            self.assertIn("2026-07-21", detail_xml)
            self.assertIn("EPİAŞ", detail_xml)

    def test_baraj_sorting_supports_fullness_and_turkish_name_order(self):
        items = [
            {"dam": "Zamantı", "activeFullnessAmount": 20},
            {"dam": "Çamlıdere", "activeFullnessAmount": 80},
            {"dam": "Altınkaya", "activeFullnessAmount": None},
        ]
        self.assertEqual(
            [item["dam"] for item in APP._sort_dams(items, "fullness-desc")],
            ["Çamlıdere", "Zamantı", "Altınkaya"],
        )
        self.assertEqual(
            [item["dam"] for item in APP._sort_dams(items, "fullness-asc")],
            ["Zamantı", "Çamlıdere", "Altınkaya"],
        )
        self.assertEqual(
            [item["dam"] for item in APP._sort_dams(items, "name-asc")],
            ["Altınkaya", "Çamlıdere", "Zamantı"],
        )
        self.assertEqual(
            [item["dam"] for item in APP._sort_dams(items, "name-desc")],
            ["Zamantı", "Çamlıdere", "Altınkaya"],
        )
        turkish_i_names = [
            {"dam": "İkizdere", "activeFullnessAmount": 10},
            {"dam": "Ilısu", "activeFullnessAmount": 20},
        ]
        self.assertEqual(
            [
                item["dam"]
                for item in APP._sort_dams(turkish_i_names, "name-asc")
            ],
            ["Ilısu", "İkizdere"],
        )

    def test_baraj_xlsx_contains_summary_and_sorted_list_sheets(self):
        workbook = APP._baraj_xlsx(
            {
                "availableDates": ["2026-07-20"],
                "items": [
                    {
                        "dam": "Zamantı",
                        "basin": "Seyhan",
                        "activeFullnessAmount": 20,
                        "date": "2026-07-20T00:00:00+03:00",
                    },
                    {
                        "dam": "Altınkaya",
                        "basin": "Kızılırmak",
                        "activeFullnessAmount": 70,
                        "date": "2026-07-20T00:00:00+03:00",
                    },
                ],
            },
            "name-asc",
        )
        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            list_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn('name="Özet"', workbook_xml)
            self.assertIn('name="Baraj Listesi"', workbook_xml)
            self.assertLess(list_xml.index("Altınkaya"), list_xml.index("Zamantı"))
            self.assertIn("Aktif doluluk (%)", list_xml)

    def test_market_xlsx_contains_summary_and_hourly_sheets(self):
        dashboard = {
            "date": "2026-07-20",
            "summary": {
                "ptfAverage": 100,
                "smfAverage": 110,
                "ptfAverageByCurrency": {
                    "TRY": 100,
                    "EUR": 4,
                    "USD": 5,
                },
                "smfAverageByCurrency": {"TRY": 110},
                "yalTotal": 6,
                "yatTotal": 9,
            },
            "rows": [
                {
                    "time": "01:00",
                    "ptf": 100,
                    "smf": 110,
                    "ptfByCurrency": {"TRY": 100, "EUR": 4, "USD": 5},
                    "smfByCurrency": {"TRY": 110},
                    "yal": 6,
                    "yat": -9,
                    "direction": "Enerji Açığı",
                }
            ],
        }
        workbook = APP._market_xlsx(dashboard)
        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            self.assertIn("xl/worksheets/sheet1.xml", archive.namelist())
            self.assertIn("xl/worksheets/sheet2.xml", archive.namelist())
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            hourly_xml = archive.read("xl/worksheets/sheet2.xml").decode(
                "utf-8"
            )
            self.assertIn('name="Özet"', workbook_xml)
            self.assertIn('name="Saatlik Veri"', workbook_xml)
            self.assertIn("PTF (EUR/MWh)", hourly_xml)
            self.assertNotIn("SMF (EUR/MWh)", hourly_xml)
            self.assertNotIn("SMF (USD/MWh)", hourly_xml)


class SuiteHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.token = APP.AUTH.create_session("test@baha.local", "TGT-test")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), APP.RequestHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        APP.AUTH.revoke(cls.token)
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    @classmethod
    def get(cls, path):
        request = urllib.request.Request(
            cls.base_url + path,
            headers={
                "Cookie": f"{APP.AUTH.cookie_name}={cls.token}",
                "Accept": "text/html,application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers

    def test_shared_session_is_visible_to_all_modules(self):
        for path in (
            "/piyasa/api/session",
            "/baraj/api/session",
            "/uretim/api/session",
        ):
            status, content, _ = self.get(path)
            payload = json.loads(content)
            self.assertEqual(status, 200)
            self.assertTrue(payload["authenticated"])
            self.assertEqual(payload["username"], "test@baha.local")

    def test_first_visit_redirects_to_single_shared_login(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request("GET", "/")
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/login")
        connection.close()

    def test_login_is_a_clean_shared_portal_page(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request("GET", "/login")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertNotIn('class="baha-suite-nav"', html)
        self.assertNotIn('href="/portal-shell.css"', html)
        self.assertNotIn('href="/piyasa/"', html)
        self.assertIn('href="/suite-assets/icon-192.png"', html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png"',
            html,
        )
        self.assertIn("BAHA ENERJİ YÖNETİM PANELİ", html)
        self.assertIn("Enerjinin nabzı", html)
        self.assertIn("3</strong><span>entegre panel", html)
        self.assertIn('class="mobile-topbar"', html)
        self.assertIn('class="mobile-status"', html)
        self.assertIn('class="mobile-panel-tags"', html)
        self.assertNotIn("BAHA UEVM", html)
        connection.close()

        status, content, _ = self.get("/manifest.webmanifest")
        manifest = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["name"], "Baha Enerji Yönetim Paneli")
        self.assertEqual(
            manifest["icons"][0]["src"],
            "/suite-assets/icon-192.png",
        )
        self.assertEqual(
            [icon["sizes"] for icon in manifest["icons"]],
            ["192x192", "512x512"],
        )

        status, content, _ = self.get("/sw.js")
        worker = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("baha-enerji-shell-v13", worker)
        self.assertNotIn("baha-uretim-logo.svg", worker)

        for path in (
            "/suite-assets/icon-192.png",
            "/suite-assets/icon-512.png",
            "/suite-assets/apple-touch-icon.png",
        ):
            with self.subTest(path=path):
                status, _, headers = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers.get_content_type(), "image/png")

        status, content, headers = self.get("/login.css")
        login_css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("input::-ms-reveal", login_css)
        self.assertIn("input::-webkit-credentials-auto-fill-button", login_css)
        self.assertIn("Mobil giriş: kurumsal kart", login_css)
        self.assertIn(".mobile-status", login_css)
        self.assertIn(".mobile-panel-tags", login_css)
        self.assertIn("backdrop-filter: blur(16px)", login_css)
        self.assertIn("overscroll-behavior: none", login_css)
        self.assertEqual(headers.get_content_type(), "text/css")

    def test_legacy_login_and_dashboard_paths_are_canonicalized(self):
        cases = {
            "/login/": "/login",
            "/dashboard": "/piyasa/",
            "/panel/": "/piyasa/",
            "/index.html": "/piyasa/",
        }
        for path, expected_location in cases.items():
            with self.subTest(path=path):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.server.server_port, timeout=5
                )
                connection.request(
                    "GET",
                    path,
                    headers={
                        "Cookie": f"{APP.AUTH.cookie_name}={self.token}"
                    },
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 303)
                self.assertEqual(
                    response.getheader("Location"),
                    expected_location,
                )
                connection.close()

    def test_authenticated_first_visit_opens_piyasa(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(
            "GET",
            "/",
            headers={"Cookie": f"{APP.AUTH.cookie_name}={self.token}"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 303)
        self.assertEqual(response.getheader("Location"), "/piyasa/")
        connection.close()

    def test_piyasa_page_paths_are_mounted_under_piyasa(self):
        status, content, _ = self.get("/piyasa/")
        html = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("<title>Baha Piyasa Paneli</title>", html)
        self.assertIn('src="/piyasa/app.js?v=30"', html)
        self.assertIn(
            'class="suite-footer" data-suite-footer="piyasa"',
            html,
        )
        self.assertIn("BAHA<br>ENERJ&#304;", html)
        self.assertIn('id="piyasaFooterUpdated"', html)
        self.assertIn('href="/piyasa/styles.css?v=25"', html)
        self.assertIn('href="/piyasa/" aria-current="page"', html)
        self.assertIn('src="/piyasa-charts.js?v=8"', html)
        self.assertIn('href="/piyasa-suite.css?v=19"', html)
        self.assertIn('src="/theme-sync.js"', html)
        self.assertIn('href="/piyasa/assets/apple-touch-icon.png"', html)
        self.assertNotIn("cdn.jsdelivr.net/npm/apexcharts", html)
        self.assertNotIn("cdn.sheetjs.com", html)
        self.assertIn(
            'id="login-view" class="login-shell" style="display:none!important"',
            html,
        )
        self.assertIn('id="app-view" class="app-shell"', html)
        sidebar = html.split('<aside class="sidebar">', 1)[1].split(
            "</aside>", 1
        )[0]
        self.assertNotIn("<small>Piyasa Paneli</small>", sidebar)
        self.assertIn('class="piyasa-header-actions"', html)
        header_actions = html.split('class="piyasa-header-actions"', 1)[1].split(
            "</header>", 1
        )[0]
        self.assertLess(
            header_actions.index('id="theme-toggle"'),
            header_actions.index('class="user-pill"'),
        )
        self.assertIn("EPİAŞ · EPİAŞ canlı", html)
        self.assertIn('id="menu-close"', html)
        self.assertIn('id="sidebar-overlay"', html)
        self.assertIn('aria-label="Menüyü aç" aria-expanded="false"', html)
        self.assertIn('data-currency="EUR"', html)
        self.assertIn('data-currency="USD"', html)
        self.assertIn('aria-label="PTF fiyat birimi"', html)
        self.assertIn('id="price-chart-title"', html)
        self.assertIn('id="currency-note"', html)

    def test_piyasa_never_shows_its_old_login_screen(self):
        status, content, _ = self.get("/piyasa/app.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("show('login')", script)
        self.assertIn("window.location.replace('/login')", script)
        self.assertIn("/piyasa/api/export.xlsx", script)
        self.assertNotIn("window.XLSX", script)
        self.assertIn("baha:themechange", script)
        self.assertIn("EPİAŞ · EPİAŞ canlı", script)
        self.assertIn("fontFamily:SUITE_FONT", script)
        self.assertIn("ptfByCurrency", script)
        self.assertIn("ptfAverageByCurrency", script)
        self.assertIn("smfAverageByCurrency?.TRY", script)
        self.assertNotIn("tryPerUnit", script)
        self.assertNotIn("SMF aynÄ± EPÄ°AÅ gÃ¶sterge kuruyla", script)
        self.assertIn("baha-market-currency", script)
        self.assertIn("baha-market-v4:", script)
        self.assertIn("epias-ptf-direct", script)
        self.assertIn("supportsCurrencyPayload", script)
        self.assertIn("&refresh=1", script)

    def test_piyasa_local_chart_and_styles_are_served(self):
        status, content, headers = self.get("/piyasa-charts.js")
        self.assertEqual(status, 200)
        chart_script = content.decode("utf-8")
        self.assertIn("window.ApexCharts = LocalEnergyChart", chart_script)
        self.assertIn("tooltip.offsetHeight", chart_script)
        self.assertIn("spaceAbove < tooltipHeight", chart_script)
        self.assertIn("tooltip.dataset.placement", chart_script)
        self.assertIn("const compact = width <= 700", chart_script)
        self.assertIn("const labelStep = 3", chart_script)
        self.assertIn("const labelIndexes", chart_script)
        self.assertIn("const visualRatioFor", chart_script)
        self.assertIn("labelIndexes.length - 1", chart_script)
        self.assertIn("closestDistance", chart_script)
        self.assertIn('"font-size": compact ? "9" : "10"', chart_script)
        self.assertIn(
            "index % labelStep === 0 || index === categories.length - 1",
            chart_script,
        )
        self.assertEqual(headers.get_content_type(), "text/javascript")

        status, content, headers = self.get("/piyasa-suite.css")
        self.assertEqual(status, 200)
        css = content.decode("utf-8")
        self.assertIn(".baha-suite-piyasa .metric-grid", css)
        self.assertIn("repeat(12, minmax(0, 1fr))", css)
        self.assertIn(".baha-suite-piyasa #direction-chart", css)
        self.assertIn("overflow: hidden !important", css)
        self.assertIn(".baha-suite-piyasa .piyasa-header-actions", css)
        self.assertIn(".baha-suite-piyasa .menu-close", css)
        self.assertIn("minmax(220px, 1.2fr)", css)
        self.assertIn(".baha-suite-piyasa .last-update", css)
        self.assertIn("display: none", css)
        self.assertIn("max-width: calc(100% - 16px)", css)
        self.assertIn("transform: none", css)
        self.assertIn("transform: translateX(-105%)", css)
        self.assertIn("z-index: 100030", css)
        self.assertIn("display: grid !important", css)
        self.assertIn("padding: 8px 0 16px", css)
        self.assertIn(".baha-suite-piyasa #quantity-chart", css)
        self.assertIn("margin-inline: -10px", css)
        self.assertIn(".baha-suite-piyasa .direction-hour strong", css)
        self.assertIn("writing-mode: horizontal-tb", css)
        self.assertIn(".baha-suite-piyasa .direction-hour span", css)
        self.assertIn(".baha-suite-piyasa .table th:last-child", css)
        self.assertIn("min-width: 960px", css)
        self.assertIn("min-width: 780px", css)
        self.assertIn("padding-inline: 12px", css)
        self.assertIn("table-layout: fixed", css)
        self.assertIn("width: calc(100% / 6)", css)
        self.assertIn("text-align: left", css)
        self.assertIn("white-space: nowrap", css)
        self.assertIn("display: inline-flex", css)
        self.assertIn('html[data-theme="dark"] .baha-suite-piyasa .direction.up', css)
        self.assertIn('html[data-theme="dark"] .baha-suite-piyasa .direction.down', css)
        self.assertIn(
            'html[data-theme="dark"] .baha-suite-piyasa .direction-hour.missing',
            css,
        )
        self.assertIn('html[data-theme="dark"] .baha-suite-piyasa .menu-button', css)
        self.assertIn(".baha-suite-piyasa h1", css)
        self.assertIn("font-size: clamp(25px, 2.2vw, 36px)", css)
        self.assertEqual(headers.get_content_type(), "text/css")

    def test_baraj_inline_api_paths_are_mounted_under_baraj(self):
        status, content, _ = self.get("/baraj/")
        html = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(
            "<title>Baha Aktif Baraj Doluluk Paneli</title>",
            html,
        )
        self.assertIn("/baraj/api/active-fullness", html)
        self.assertIn('href="/baraj/" aria-current="page"', html)
        self.assertNotIn("showLogin();", html)
        self.assertIn(
            'class="page page-center login-screen" style="display:none!important"',
            html,
        )
        self.assertIn('id="dashboard" class="page"', html)
        self.assertIn('id="baraj-summary"', html)
        self.assertIn('id="baraj-list"', html)
        self.assertIn('id="baraj-regime"', html)
        self.assertIn('id="basinSelect"', html)
        self.assertIn('class="baraj-basin-toolbar"', html)
        self.assertIn('id="basinToolbarPeriod"', html)
        self.assertIn("Havzalar alınamadı", html)
        self.assertIn("/baraj/api/basin-history", html)
        self.assertIn('id="basinDamTableBody"', html)
        self.assertIn('id="basinXlsxButton"', html)
        self.assertIn("/baraj/api/basin-export.xlsx?basin=", html)
        self.assertIn('id="sortSelect"', html)
        self.assertIn('id="barajXlsxButton"', html)
        self.assertIn('id="barajDateSelect"', html)
        self.assertNotIn('id="barajDataSource"', html)
        self.assertIn("/baraj/api/export.xlsx?", html)
        self.assertIn("Doluluk: yüksekten düşüğe", html)
        self.assertIn("Baraj adı: A–Z", html)
        self.assertIn('class="suite-sidebar"', html)
        self.assertIn("data-suite-theme-toggle", html)
        self.assertIn("EPİAŞ · EPİAŞ canlı", html)
        self.assertIn('class="suite-menu-close"', html)
        self.assertIn('href="/module-suite.css?v=9"', html)
        self.assertIn('src="/module-suite.js"', html)
        self.assertIn('src="/theme-sync.js"', html)
        self.assertIn('href="/baraj/apple-touch-icon.png"', html)
        self.assertIn(
            'class="suite-footer" data-suite-footer="baraj"',
            html,
        )

        status, content, _ = self.get("/baraj/manifest.webmanifest")
        manifest = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(
            [icon["sizes"] for icon in manifest["icons"]],
            ["192x192", "512x512"],
        )
        self.assertIn('id="barajFooterUpdated"', html)
        self.assertIn('href="/portal-shell.css?v=2"', html)

    def test_baraj_archive_date_is_served_from_pivot(self):
        status, content, _ = self.get(
            "/baraj/api/active-fullness?date=2026-06-24"
        )
        payload = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "excel")
        self.assertEqual(payload["selectedDate"], "2026-06-24")
        self.assertEqual(payload["sourceLabel"], "Arşiv")
        self.assertEqual(
            payload["archiveDates"],
            APP._baraj_archive()["availableDates"],
        )
        self.assertTrue(
            any(item["dam"] == "ADATEPE" for item in payload["items"])
        )

    def test_baraj_archive_date_can_be_downloaded_as_xlsx(self):
        status, content, headers = self.get(
            "/baraj/api/export.xlsx?sort=name-asc&date=2026-06-24"
        )
        self.assertEqual(status, 200)
        self.assertIn(
            "baha-enerji-baraj-aktif-2026-06-24.xlsx",
            headers["Content-Disposition"],
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            summary_xml = archive.read("xl/worksheets/sheet1.xml").decode(
                "utf-8"
            )
            list_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn("Arşiv", summary_xml)
            self.assertIn("ADATEPE", list_xml)

    def test_selected_basin_history_can_be_downloaded_as_xlsx(self):
        original_history = APP._baraj_basin_history
        APP._baraj_basin_history = lambda _client: {
            "startDate": "2026-06-24",
            "endDate": "2026-07-21",
            "basins": [
                {
                    "name": "Ceyhan",
                    "points": [
                        {
                            "date": "2026-07-21",
                            "average": 65,
                            "minimum": 65,
                            "maximum": 65,
                            "damCount": 1,
                        }
                    ],
                    "analysis": {"regime": "Azalan rejim"},
                    "dams": [
                        {
                            "name": "ADATEPE",
                            "points": [
                                {
                                    "date": "2026-07-21",
                                    "activeFullnessAmount": 65,
                                    "source": "EPİAŞ",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        try:
            status, content, headers = self.get(
                "/baraj/api/basin-export.xlsx?basin=Ceyhan"
            )
        finally:
            APP._baraj_basin_history = original_history

        self.assertEqual(status, 200)
        self.assertIn(
            "baha-enerji-havza-baraj-doluluk.xlsx",
            headers["Content-Disposition"],
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            detail_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn("ADATEPE", detail_xml)

    def test_baraj_xlsx_export_uses_selected_sort(self):
        original_active_fullness = APP._active_fullness
        APP._active_fullness = lambda _client: {
            "availableDates": ["2026-07-20"],
            "items": [
                {
                    "dam": "Zamantı",
                    "basin": "Seyhan",
                    "activeFullnessAmount": 20,
                    "date": "2026-07-20",
                },
                {
                    "dam": "Altınkaya",
                    "basin": "Kızılırmak",
                    "activeFullnessAmount": 70,
                    "date": "2026-07-20",
                },
            ],
        }
        try:
            status, content, headers = self.get(
                "/baraj/api/export.xlsx?sort=name-asc"
            )
        finally:
            APP._active_fullness = original_active_fullness

        self.assertEqual(status, 200)
        self.assertEqual(
            headers.get_content_type(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn(
            "baha-enerji-baraj-aktif-2026-07-20.xlsx",
            headers["Content-Disposition"],
        )
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            list_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertLess(list_xml.index("Altınkaya"), list_xml.index("Zamantı"))

    def test_uretim_script_paths_are_mounted_under_uretim(self):
        status, content, _ = self.get("/uretim/app.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('fetch(`/uretim/api/dashboard?', script)
        self.assertIn('window.location.replace("/uretim/login")', script)
        self.assertNotIn("Kaynak bazında UEÇM yayımlamıyor", script)
        self.assertNotIn("Yayımlanmıyor</td>", script)
        self.assertIn("dataAlertClose", script)
        self.assertIn('addEventListener("click", hideDataAlert)', script)
        self.assertIn("const sortedGroups = [...groups].sort", script)
        self.assertIn("elements.mixLegend.innerHTML = sortedGroups", script)

    def test_uretim_page_uses_equalized_logo_size(self):
        status, content, _ = self.get("/uretim/")
        html = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("<title>Baha Üretim Paneli</title>", html)
        self.assertIn('class="baha-suite-page baha-suite-uretim"', html)
        self.assertIn('src="/suite-assets/baha-logo.png"', html)
        self.assertIn('class="suite-sidebar"', html)
        self.assertIn("data-suite-theme-toggle", html)
        self.assertIn("EPİAŞ · EPİAŞ canlı", html)
        self.assertIn('class="suite-menu-close"', html)
        self.assertIn("UEVM &amp; UEÇM Üretim Özeti", html)
        self.assertIn('id="dataAlertClose"', html)
        self.assertIn('aria-label="Uyarıyı kapat"', html)
        self.assertIn(
            '<div class="eyebrow">PANEL / GENEL BAKIŞ</div>',
            html,
        )
        self.assertLess(
            html.index('class="eyebrow"'),
            html.index("UEVM &amp; UEÇM Üretim Özeti"),
        )
        self.assertNotIn('<th class="numeric">Fark</th>', html)
        self.assertNotIn(
            'title="Kaynak bazında yayımlanmıyor"',
            html,
        )
        self.assertIn('href="/module-suite.css?v=9"', html)
        self.assertIn('src="/module-suite.js"', html)
        self.assertIn('src="/theme-sync.js"', html)
        self.assertIn('href="/uretim/manifest.webmanifest"', html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png"',
            html,
        )
        self.assertIn(
            'class="suite-footer" data-suite-footer="uretim"',
            html,
        )

        status, content, _ = self.get("/uretim/manifest.webmanifest")
        manifest = json.loads(content)
        self.assertEqual(status, 200)
        self.assertEqual(
            [icon["src"] for icon in manifest["icons"]],
            [
                "/suite-assets/icon-192.png",
                "/suite-assets/icon-512.png",
            ],
        )
        self.assertIn("BAHA<br />ENERJ&#304;", html)
        footer = html.split(
            'class="suite-footer" data-suite-footer="uretim"',
            1,
        )[1].split("</footer>", 1)[0]
        self.assertNotIn("BAHA ÜRETİM", footer)
        sidebar = html.split('<aside class="suite-sidebar">', 1)[1].split(
            "</aside>", 1
        )[0]
        self.assertNotIn("<small>", sidebar)
        self.assertNotIn(
            'src="/uretim/assets/baha-uretim-logo.svg"',
            html,
        )

        status, content, _ = self.get("/portal-shell.css")
        css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(".baha-suite-uretim .topbar .brand-mark", css)
        self.assertIn("body.baha-suite-page .suite-footer", css)
        self.assertIn("margin: 48px 0 0 252px", css)
        self.assertIn("grid-template-columns: 1fr", css)
        self.assertIn("width: 54px", css)
        self.assertIn("padding: 5px", css)
        self.assertIn("transform: none", css)
        self.assertIn("text-decoration-line: underline", css)

        status, content, headers = self.get("/suite-assets/baha-logo.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/png")
        self.assertGreater(len(content), 400_000)

    def test_shared_module_theme_assets_are_served(self):
        status, content, headers = self.get("/module-suite.css")
        css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(".suite-sidebar", css)
        self.assertIn(".baha-suite-baraj #baraj-summary", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-toolbar", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-detail", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-table", css)
        self.assertIn(
            'html[data-theme="dark"] .baha-suite-baraj .baraj-basin-detail',
            css,
        )
        self.assertIn(
            ".baha-suite-baraj .baraj-basin-select-wrap::after",
            css,
        )
        self.assertIn("--tblr-table-striped-bg: #121e33", css)
        self.assertIn("background-color: #121e33 !important", css)
        self.assertIn(
            ".baha-suite-uretim .hero-grid > div:first-child",
            css,
        )
        self.assertIn(".baha-suite-uretim .mix-panel .donut-layout", css)
        self.assertIn("max-width: 520px", css)
        self.assertIn("min-height: 58px", css)
        self.assertIn("flex: 1 1 auto", css)
        self.assertIn("grid-template-rows: repeat(4, minmax(58px, 1fr))", css)
        self.assertIn("overflow-wrap: normal", css)
        self.assertIn("word-break: normal", css)
        self.assertIn("#barajDateSelect", css)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn('grid-template-areas:', css)
        self.assertIn('"components share"', css)
        self.assertEqual(headers.get_content_type(), "text/css")

        status, content, headers = self.get("/module-suite.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("suite-sidebar-open", script)
        self.assertIn('fetch("/api/session"', script)
        self.assertIn("EPİAŞ · EPİAŞ canlı", script)
        self.assertIn("navigationLockUntil", script)
        self.assertIn("getBoundingClientRect", script)
        self.assertNotIn("IntersectionObserver", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

        status, content, headers = self.get("/theme-sync.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('const STORAGE_KEY = "baha-theme"', script)
        self.assertIn('window.addEventListener("storage"', script)
        self.assertIn("window.BahaTheme", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")


if __name__ == "__main__":
    unittest.main()
