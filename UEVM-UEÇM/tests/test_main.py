import io
import shutil
import threading
import unittest
import urllib.error
import zipfile
from datetime import date
from unittest import mock
from xml.etree import ElementTree

import main


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.date_range = main.DateRange(date(2026, 7, 1), date(2026, 7, 2))

    @staticmethod
    def dashboard_row(timestamp, uevm, uecm):
        sources = {source["id"]: 10.0 for source in main.SOURCE_DEFINITIONS}
        groups = {
            group["id"]: sum(
                sources[source["id"]]
                for source in main.SOURCE_DEFINITIONS
                if source["group"] == group["id"]
            )
            for group in main.GROUP_DEFINITIONS
        }
        return {
            "timestamp": timestamp,
            "uevm": uevm,
            "uecm": uecm,
            "sources": sources,
            "groups": groups,
        }

    def test_dashboard_calculates_system_level_difference(self):
        rows = [
            self.dashboard_row("2026-07-01T00:00:00+03:00", 100, 95),
            self.dashboard_row("2026-07-01T01:00:00+03:00", 110, 108),
        ]
        payload = main.build_dashboard(rows, self.date_range)

        expected = sum(row["uevm"] - row["uecm"] for row in rows)
        self.assertAlmostEqual(payload["summary"]["difference"], expected, places=2)
        self.assertEqual(payload["period"]["comparableHours"], 2)
        self.assertEqual(payload["meta"]["source"], "epias")
        self.assertTrue(all("uecm" not in source for source in payload["sources"]))
        first_hour = payload["series"][0]
        self.assertEqual(first_hour["sun"], 10.0)
        self.assertEqual(first_hour["wind"], 10.0)
        self.assertEqual(first_hour["hydro"], 20.0)
        self.assertEqual(first_hour["thermal"], 70.0)
        self.assertEqual(first_hour["naturalGas"], 10.0)

    def test_xlsx_export_contains_summary_source_and_hourly_sheets(self):
        rows = [
            self.dashboard_row("2026-07-01T00:00:00+03:00", 100, 95),
            self.dashboard_row("2026-07-01T01:00:00+03:00", 110, 108),
        ]
        workbook = main.build_xlsx(main.build_dashboard(rows, self.date_range))

        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            expected_files = {
                "[Content_Types].xml",
                "_rels/.rels",
                "xl/workbook.xml",
                "xl/styles.xml",
                "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml",
                "xl/worksheets/sheet3.xml",
            }
            self.assertTrue(expected_files.issubset(archive.namelist()))
            for filename in expected_files:
                ElementTree.fromstring(archive.read(filename))
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn('name="Özet"', workbook_xml)
            self.assertIn('name="Kaynaklar"', workbook_xml)
            self.assertIn('name="Saatlik Veri"', workbook_xml)
            sources_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn("UEVM (MWh)", sources_xml)
            self.assertIn("Pay", sources_xml)
            self.assertLess(sources_xml.index("Ana grup"), sources_xml.index("Kaynak"))
            self.assertNotIn("UEÇM", sources_xml)
            self.assertNotIn("Fark", sources_xml)
            self.assertNotIn("Kaynak bazında yayımlanmıyor", sources_xml)
            hourly_xml = archive.read("xl/worksheets/sheet3.xml").decode("utf-8")
            self.assertIn("Güneş (MWh)", hourly_xml)
            self.assertIn("Hidroelektrik (MWh)", hourly_xml)

    def test_normalizes_uevm_and_uecm_without_inventing_source_uecm(self):
        uevm = [
            {
                "date": "2026-07-01T00:00:00+03:00",
                "hour": 1,
                "total": 100,
                "sun": 10,
                "wind": 20,
                "dam": 5,
                "river": 5,
                "importedCoal": 10,
                "lignite": 10,
                "stoneCoal": 5,
                "asphaltite": 5,
                "naturalGas": 20,
            }
        ]
        uecm = [{"hour": "2026-07-01T00:00:00+03:00", "swv": 95}]

        rows = main.normalize_epias_data(uevm, uecm)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["uecm"], 95)
        self.assertEqual(rows[0]["groups"]["renewable"], 40)
        self.assertEqual(rows[0]["groups"]["thermal"], 30)
        self.assertEqual(rows[0]["groups"]["natural_gas"], 20)

    def test_reads_every_official_epias_uevm_field_and_keeps_direct_total(self):
        uevm = [
            {
                "date": "2026-07-01T00:00:00+03:00",
                "hour": 1,
                "total": 999,
                "sun": 1,
                "wind": 2,
                "dam": 3,
                "river": 4,
                "biomass": 5,
                "geothermal": 6,
                "importedCoal": 7,
                "lignite": 8,
                "stoneCoal": 9,
                "asphaltite": 10,
                "fueloil": 11,
                "lng": 12,
                "naphtha": 13,
                "naturalGas": 14,
                "other": 15,
                "internationalImport": 16,
                "internationalExport": 17,
            }
        ]

        row = main.normalize_epias_data(uevm, [])[0]

        self.assertEqual(row["uevm"], 999)
        self.assertEqual(row["groups"]["renewable"], 21)
        self.assertEqual(row["groups"]["thermal"], 70)
        self.assertEqual(row["groups"]["natural_gas"], 14)
        self.assertEqual(row["groups"]["other"], 48)

    def test_keeps_uecm_only_hours_and_totals_each_epias_stream_independently(self):
        rows = main.normalize_epias_data(
            [
                {
                    "date": "2026-07-01T00:00:00+03:00",
                    "hour": 1,
                    "total": 100,
                }
            ],
            [
                {"hour": "2026-07-01T00:00:00+03:00", "swv": 95},
                {"hour": "2026-07-01T01:00:00+03:00", "swv": 110},
            ],
        )

        payload = main.build_dashboard(rows, self.date_range)

        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1]["uevm"])
        self.assertEqual(payload["period"]["uevmHours"], 1)
        self.assertEqual(payload["period"]["uecmHours"], 2)
        self.assertEqual(payload["period"]["comparableHours"], 1)
        self.assertEqual(payload["summary"]["uevmTotal"], 100)
        self.assertEqual(payload["summary"]["uecmTotal"], 205)
        self.assertEqual(payload["summary"]["difference"], 5)

    def test_detects_zero_based_epias_hours(self):
        rows = main.normalize_epias_data(
            [
                {
                    "date": "2026-07-01T00:00:00+03:00",
                    "hour": 0,
                    "total": 100,
                },
                {
                    "date": "2026-07-01T00:00:00+03:00",
                    "hour": 1,
                    "total": 110,
                },
            ],
            [
                {"hour": "2026-07-01T00:00:00+03:00", "swv": 90},
                {"hour": "2026-07-01T01:00:00+03:00", "swv": 95},
            ],
        )

        self.assertEqual(rows[0]["timestamp"], "2026-07-01T00:00:00+03:00")
        self.assertEqual(rows[1]["timestamp"], "2026-07-01T01:00:00+03:00")
        self.assertEqual([row["uecm"] for row in rows], [90, 95])

    def test_rejects_ranges_over_limit(self):
        with self.assertRaisesRegex(ValueError, "en fazla"):
            main.parse_date_range(
                {"start": ["2026-01-01"], "end": ["2026-07-01"]}
            )

    def test_dashboard_uses_nearest_published_period_when_requested_data_is_empty(self):
        class StubClient:
            def fetch_uevm(self, date_range):
                cutoff = date(2026, 6, 24)
                current = date_range.start
                items = []
                while current <= min(date_range.end, cutoff):
                    items.append(
                        {
                            "date": f"{current.isoformat()}T00:00:00+03:00",
                            "hour": 1,
                            "total": 100,
                        }
                    )
                    current = date.fromordinal(current.toordinal() + 1)
                return items

            def fetch_uecm(self, date_range):
                return [
                    {
                        "hour": f"{date_range.start.isoformat()}T00:00:00+03:00",
                        "swv": 95,
                    }
                ]

        requested = main.DateRange(date(2026, 7, 10), date(2026, 7, 15))
        payload = main.DashboardService().dashboard(
            requested,
            client=StubClient(),
        )

        self.assertEqual(payload["period"]["end"], "2026-06-24")
        self.assertEqual(payload["meta"]["latestAvailableDate"], "2026-06-24")
        self.assertIsNotNone(payload["meta"]["warning"])
        self.assertEqual(payload["summary"]["uecmTotal"], 95)

    def test_dashboard_keeps_requested_range_when_some_days_are_published(self):
        class StubClient:
            def fetch_uevm(self, date_range):
                cutoff = date(2026, 6, 30)
                current = date_range.start
                items = []
                while current <= min(date_range.end, cutoff):
                    items.append(
                        {
                            "date": f"{current.isoformat()}T00:00:00+03:00",
                            "hour": 1,
                            "total": 100,
                        }
                    )
                    current = date.fromordinal(current.toordinal() + 1)
                return items

            def fetch_uecm(self, date_range):
                return [
                    {
                        "hour": f"{date_range.start.isoformat()}T00:00:00+03:00",
                        "swv": 95,
                    }
                ]

        requested = main.DateRange(date(2026, 6, 18), date(2026, 7, 17))
        payload = main.DashboardService(cache_ttl_seconds=0).dashboard(
            requested,
            client=StubClient(),
        )

        self.assertEqual(payload["period"]["start"], "2026-06-18")
        self.assertEqual(payload["period"]["end"], "2026-07-17")
        self.assertEqual(payload["period"]["days"], 30)
        self.assertEqual(payload["meta"]["availableStartDate"], "2026-06-18")
        self.assertEqual(payload["meta"]["availableEndDate"], "2026-06-30")
        self.assertEqual(payload["period"]["hours"], 13)
        self.assertIsNotNone(payload["meta"]["warning"])

    def test_dashboard_discards_epias_rows_outside_requested_day(self):
        class StubClient:
            def fetch_uevm(self, date_range):
                return [
                    {
                        "date": "2026-06-23T00:00:00+03:00",
                        "hour": 1,
                        "total": 100,
                    },
                    {
                        "date": "2026-06-24T00:00:00+03:00",
                        "hour": 1,
                        "total": 200,
                    },
                ]

            def fetch_uecm(self, date_range):
                return [
                    {"hour": "2026-06-23T00:00:00+03:00", "swv": 95},
                    {"hour": "2026-06-24T00:00:00+03:00", "swv": 190},
                ]

        requested = main.DateRange(date(2026, 6, 23), date(2026, 6, 23))
        payload = main.DashboardService(cache_ttl_seconds=0).dashboard(
            requested,
            client=StubClient(),
        )

        self.assertEqual(payload["period"]["start"], "2026-06-23")
        self.assertEqual(payload["period"]["end"], "2026-06-23")
        self.assertEqual(payload["period"]["hours"], 1)
        self.assertEqual(payload["period"]["comparableHours"], 1)
        self.assertEqual(payload["summary"]["uevmTotal"], 100)
        self.assertEqual(payload["summary"]["uecmTotal"], 95)
        self.assertEqual(
            [row["timestamp"] for row in payload["series"]],
            ["2026-06-23T00:00:00+03:00"],
        )

    def test_dashboard_reuses_cached_date_range(self):
        class StubClient:
            def __init__(self):
                self.uevm_calls = 0
                self.uecm_calls = 0

            def fetch_uevm(self, date_range):
                self.uevm_calls += 1
                return [
                    {
                        "date": f"{date_range.start.isoformat()}T00:00:00+03:00",
                        "hour": 1,
                        "total": 100,
                    }
                ]

            def fetch_uecm(self, date_range):
                self.uecm_calls += 1
                return [
                    {
                        "hour": f"{date_range.start.isoformat()}T00:00:00+03:00",
                        "swv": 95,
                    }
                ]

        client = StubClient()
        service = main.DashboardService(cache_ttl_seconds=300)
        requested = main.DateRange(date(2026, 7, 1), date(2026, 7, 1))

        first = service.dashboard(requested, client=client)
        second = service.dashboard(requested, client=client)

        self.assertIs(first, second)
        self.assertEqual(client.uevm_calls, 1)
        self.assertEqual(client.uecm_calls, 1)

    def test_epias_rate_limit_hides_gateway_detail(self):
        client = main.EpiasClient(tgt="TGT-test-ticket")
        rate_limit = urllib.error.HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"internal gateway detail: BLOCKED"),
        )

        with mock.patch("urllib.request.urlopen", side_effect=rate_limit):
            with self.assertRaises(main.EpiasError) as raised:
                client._request("https://example.test")

        self.assertEqual(raised.exception.status_code, 429)
        self.assertIn("istek sınırına", str(raised.exception))
        self.assertNotIn("BLOCKED", str(raised.exception))

    def test_epias_rate_limiter_waits_before_burst_limit(self):
        current = [0.0]
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            current[0] += seconds

        limiter = main.RollingRateLimiter(
            per_minute=100,
            per_second=2,
            time_fn=lambda: current[0],
            sleep_fn=fake_sleep,
        )

        limiter.wait()
        limiter.wait()
        limiter.wait()

        self.assertGreaterEqual(sum(sleeps), 1.0)

    def test_epias_defaults_stay_well_below_official_ip_limits(self):
        self.assertLessEqual(
            main.DEFAULT_EPIAS_TGT_REQUESTS_PER_MINUTE,
            main.OFFICIAL_CAS_TGT_REQUESTS_PER_MINUTE // 5,
        )
        self.assertLessEqual(
            main.DEFAULT_EPIAS_TGT_BURST_PER_SECOND,
            main.OFFICIAL_CAS_TGT_BURST_PER_SECOND // 2,
        )
        self.assertLessEqual(main.DEFAULT_EPIAS_API_REQUESTS_PER_MINUTE, 24)
        self.assertLessEqual(main.DEFAULT_EPIAS_API_BURST_PER_SECOND, 2)
        self.assertLess(
            main.DEFAULT_EPIAS_TGT_CACHE_SECONDS,
            main.OFFICIAL_CAS_TGT_TTL_SECONDS,
        )
        self.assertEqual(main.DEFAULT_AUTH_SESSION_MINUTES, 450)

    def test_epias_429_triggers_gateway_backoff(self):
        class StubLimiter:
            def __init__(self):
                self.waits = 0
                self.backoffs = []

            def wait(self):
                self.waits += 1

            def backoff(self, seconds=None, **kwargs):
                self.backoffs.append((seconds, kwargs.get("reason", "")))

        stub = StubLimiter()
        original = main.EPIAS_API_RATE_LIMITER
        client = main.EpiasClient(tgt="TGT-test-ticket")
        rate_limit = urllib.error.HTTPError(
            f"{client.api_base}/v1/test",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            io.BytesIO(b"gateway detail"),
        )

        try:
            main.EPIAS_API_RATE_LIMITER = stub
            with mock.patch("urllib.request.urlopen", side_effect=rate_limit):
                with self.assertRaises(main.EpiasError):
                    client._request(f"{client.api_base}/v1/test")
        finally:
            main.EPIAS_API_RATE_LIMITER = original

        self.assertEqual(stub.waits, 1)
        self.assertEqual(stub.backoffs, [(2.0, "EPİAŞ 429 throttling")])

    def test_epias_403_client_ban_triggers_emergency_backoff(self):
        class StubLimiter:
            def __init__(self):
                self.waits = 0
                self.backoffs = []

            def wait(self):
                self.waits += 1

            def backoff(self, seconds=None, **kwargs):
                self.backoffs.append((seconds, kwargs.get("reason", "")))

        stub = StubLimiter()
        original = main.EPIAS_API_RATE_LIMITER
        client = main.EpiasClient(tgt="TGT-test-ticket")
        banned = urllib.error.HTTPError(
            f"{client.api_base}/v1/test",
            403,
            "Forbidden",
            {"Retry-After": "3"},
            io.BytesIO(b"Client is banned for a while!"),
        )

        try:
            main.EPIAS_API_RATE_LIMITER = stub
            with mock.patch("urllib.request.urlopen", side_effect=banned):
                with self.assertRaises(main.EpiasError) as raised:
                    client._request(f"{client.api_base}/v1/test")
        finally:
            main.EPIAS_API_RATE_LIMITER = original

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(stub.waits, 1)
        self.assertEqual(stub.backoffs, [(3.0, "EPİAŞ 403 temporary block")])

    def test_singleflight_shares_inflight_epias_payloads(self):
        monitor = main.EpiasProtectionMonitor()
        singleflight = main.SingleFlight(monitor)
        ready = threading.Event()
        release = threading.Event()
        calls = []
        results = []

        def work():
            calls.append("call")
            ready.set()
            release.wait(2)
            return {"ok": True}

        first = threading.Thread(target=lambda: results.append(singleflight.run("same", work)))
        second = threading.Thread(target=lambda: results.append(singleflight.run("same", work)))
        first.start()
        ready.wait(2)
        second.start()
        release.set()
        first.join(2)
        second.join(2)

        self.assertEqual(calls, ["call"])
        self.assertEqual(results, [{"ok": True}, {"ok": True}])
        self.assertEqual(monitor.snapshot()["singleflightJoined"], 1)

    def test_epias_json_cache_reuses_payload_without_token_or_http_request(self):
        endpoint = "/v1/test/data"
        payload = {
            "startDate": "2026-07-01T00:00:00+03:00",
            "endDate": "2026-07-01T00:00:00+03:00",
            "page": {"number": 1, "size": 100},
        }
        response = {"body": {"items": [{"value": 42}]}}

        class FirstClient(main.EpiasClient):
            def __init__(self):
                super().__init__(tgt="TGT-first")
                self.http_calls = 0

            def _request(self, url, *, data=None, headers=None):
                self.http_calls += 1
                return main.HTTPStatus.OK, main.json.dumps(response).encode("utf-8")

        class CachedClient(main.EpiasClient):
            def __init__(self):
                super().__init__(tgt=None)

            def get_tgt(self, force_refresh=False):
                raise AssertionError("JSON cache hit should not request a TGT")

            def _request(self, url, *, data=None, headers=None):
                raise AssertionError("JSON cache hit should not call EPİAŞ")

        cache_dir = main.ROOT / f".test-epias-json-cache-{main.secrets.token_hex(8)}"
        try:
            with mock.patch.dict(
                main.os.environ,
                {
                    "BAHA_EPIAS_JSON_CACHE_DIR": str(cache_dir),
                    "BAHA_EPIAS_JSON_CACHE_ENABLED": "true",
                    "BAHA_EPIAS_JSON_CACHE_HISTORY_SECONDS": "86400",
                },
            ):
                first_client = FirstClient()
                first = first_client._post_json(endpoint, payload)
                second = CachedClient()._post_json(endpoint, payload)

                self.assertEqual(first, response)
                self.assertEqual(second, response)
                self.assertEqual(first_client.http_calls, 1)
                self.assertEqual(
                    len(list(cache_dir.glob("*/*.json"))),
                    1,
                )
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_epias_json_cache_guards_repeated_force_refresh(self):
        endpoint = "/v1/test/data"
        payload = {
            "startDate": "2026-07-01T00:00:00+03:00",
            "endDate": "2026-07-01T00:00:00+03:00",
        }

        class CountingClient(main.EpiasClient):
            def __init__(self):
                super().__init__(tgt="TGT-count")
                self.http_calls = 0

            def _request(self, url, *, data=None, headers=None):
                self.http_calls += 1
                body = {"call": self.http_calls}
                return main.HTTPStatus.OK, main.json.dumps(body).encode("utf-8")

        cache_dir = main.ROOT / f".test-epias-json-cache-{main.secrets.token_hex(8)}"
        try:
            with mock.patch.dict(
                main.os.environ,
                {
                    "BAHA_EPIAS_JSON_CACHE_DIR": str(cache_dir),
                    "BAHA_EPIAS_JSON_CACHE_ENABLED": "true",
                    "BAHA_EPIAS_JSON_CACHE_HISTORY_SECONDS": "86400",
                    "BAHA_EPIAS_FORCE_REFRESH_MIN_SECONDS": "60",
                },
            ):
                client = CountingClient()
                self.assertEqual(client._post_json(endpoint, payload), {"call": 1})
                self.assertEqual(
                    client._post_json(endpoint, payload, force_refresh=True),
                    {"call": 1},
                )
                self.assertEqual(client.http_calls, 1)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_epias_json_cache_can_force_refresh_after_guard_is_disabled(self):
        endpoint = "/v1/test/data"
        payload = {
            "startDate": "2026-07-01T00:00:00+03:00",
            "endDate": "2026-07-01T00:00:00+03:00",
        }

        class CountingClient(main.EpiasClient):
            def __init__(self):
                super().__init__(tgt="TGT-count")
                self.http_calls = 0

            def _request(self, url, *, data=None, headers=None):
                self.http_calls += 1
                body = {"call": self.http_calls}
                return main.HTTPStatus.OK, main.json.dumps(body).encode("utf-8")

        cache_dir = main.ROOT / f".test-epias-json-cache-{main.secrets.token_hex(8)}"
        try:
            with mock.patch.dict(
                main.os.environ,
                {
                    "BAHA_EPIAS_JSON_CACHE_DIR": str(cache_dir),
                    "BAHA_EPIAS_JSON_CACHE_ENABLED": "true",
                    "BAHA_EPIAS_JSON_CACHE_HISTORY_SECONDS": "86400",
                    "BAHA_EPIAS_FORCE_REFRESH_MIN_SECONDS": "0",
                },
            ):
                client = CountingClient()
                self.assertEqual(client._post_json(endpoint, payload), {"call": 1})
                self.assertEqual(
                    client._post_json(endpoint, payload, force_refresh=True),
                    {"call": 2},
                )
                self.assertEqual(client.http_calls, 2)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_epias_protection_snapshot_exposes_limits(self):
        snapshot = main.epias_protection_snapshot()

        self.assertEqual(snapshot["configured"]["tgt"]["perMinute"], 10)
        self.assertEqual(snapshot["configured"]["api"]["perMinute"], 24)
        self.assertEqual(snapshot["official"]["tgt"]["ipPerMinute"], 1000)
        self.assertTrue(snapshot["configured"]["singleFlight"]["enabled"])
        self.assertTrue(snapshot["configured"]["jsonCache"]["enabled"])
        self.assertEqual(
            snapshot["configured"]["jsonCache"]["forceRefreshMinimumSeconds"],
            60,
        )

    def test_auth_service_creates_and_revokes_session(self):
        auth = main.AuthService(ttl_minutes=60)

        token = auth.create_session("admin@example.com", "TGT-test-ticket")
        session = auth.get_session(token)

        self.assertIsNotNone(session)
        self.assertEqual(session.tgt, "TGT-test-ticket")
        self.assertEqual(auth.get_username(token), "admin@example.com")
        auth.revoke(token)
        self.assertIsNone(auth.get_username(token))

    def test_auth_service_returns_latest_active_session(self):
        current = [100.0]
        original_time = main.time.time
        auth = main.AuthService(ttl_minutes=60)
        try:
            main.time.time = lambda: current[0]
            auth.create_session("first@example.com", "TGT-first")
            current[0] += 10
            auth.create_session("second@example.com", "TGT-second")
            latest = auth.latest_session()
        finally:
            main.time.time = original_time

        self.assertIsNotNone(latest)
        self.assertEqual(latest.username, "second@example.com")
        self.assertEqual(latest.tgt, "TGT-second")

    def test_epias_client_accepts_existing_session_ticket(self):
        client = main.EpiasClient(tgt="TGT-test-ticket")

        self.assertTrue(client.configured)
        self.assertEqual(client.get_tgt(), "TGT-test-ticket")

    def test_epias_client_accepts_nested_items_response(self):
        client = main.EpiasClient(tgt="TGT-test-ticket")
        client._post_json = lambda *args, **kwargs: {
            "body": {
                "items": [{"hour": "2026-07-01T00:00:00+03:00", "swv": 95}],
                "page": {"number": 1, "size": 100, "total": 1},
            }
        }

        items = client.fetch_uecm(self.date_range)

        self.assertEqual(items, [{"hour": "2026-07-01T00:00:00+03:00", "swv": 95}])

    def test_epias_login_exchanges_credentials_for_ticket(self):
        client = main.EpiasClient(
            username="user@example.com",
            password="secret",
        )
        client._request = lambda *args, **kwargs: (201, b"TGT-test-ticket")

        self.assertEqual(client.get_tgt(), "TGT-test-ticket")


if __name__ == "__main__":
    unittest.main()
