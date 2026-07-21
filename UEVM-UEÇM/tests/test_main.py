import io
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

    def test_auth_service_creates_and_revokes_session(self):
        auth = main.AuthService(ttl_minutes=60)

        token = auth.create_session("admin@example.com", "TGT-test-ticket")
        session = auth.get_session(token)

        self.assertIsNotNone(session)
        self.assertEqual(session.tgt, "TGT-test-ticket")
        self.assertEqual(auth.get_username(token), "admin@example.com")
        auth.revoke(token)
        self.assertIsNone(auth.get_username(token))

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
