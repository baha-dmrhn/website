import importlib.util
import http.client
import json
import io
import sys
import threading
import unittest
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("baha_suite_test_app", ROOT / "app.py")
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class SuiteHelpersTests(unittest.TestCase):
    def test_login_rate_limiter_blocks_and_recovers_after_cooldown(self):
        now = [100.0]
        limiter = APP.LoginRateLimiter(
            max_attempts=3,
            window_seconds=60,
            block_seconds=30,
            clock=lambda: now[0],
        )
        self.assertEqual(limiter.record_failure("127.0.0.1"), 0)
        self.assertEqual(limiter.record_failure("127.0.0.1"), 0)
        self.assertEqual(limiter.record_failure("127.0.0.1"), 30)
        self.assertEqual(limiter.retry_after("127.0.0.1"), 30)
        now[0] += 31
        self.assertEqual(limiter.retry_after("127.0.0.1"), 0)

    def test_next_day_ptf_publication_changes_at_13_and_14(self):
        target_day = date(2026, 7, 23)
        timezone = APP.URETIM.TR_TZ
        waiting = APP._next_day_ptf_publication(
            target_day,
            datetime(2026, 7, 22, 12, 59, tzinfo=timezone),
        )
        preliminary = APP._next_day_ptf_publication(
            target_day,
            datetime(2026, 7, 22, 13, 0, tzinfo=timezone),
        )
        final = APP._next_day_ptf_publication(
            target_day,
            datetime(2026, 7, 22, 14, 0, tzinfo=timezone),
        )
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(preliminary["status"], "preliminary")
        self.assertEqual(preliminary["label"], "Kesinleşmemiş PTF")
        self.assertEqual(final["status"], "final")
        self.assertEqual(final["label"], "Kesinleşmiş PTF")
        self.assertIsNone(final["nextRefreshAt"])

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
        self.assertIn('href="/tuketim/"', navigation)
        self.assertIn("Tüketim", navigation)
        self.assertNotIn("Ana Sayfa", navigation)

    def test_module_sidebar_uses_matching_sections(self):
        baraj = APP._module_sidebar("baraj")
        uretim = APP._module_sidebar("uretim")
        self.assertIn('href="#baraj-summary"', baraj)
        self.assertIn('href="#baraj-regime"', baraj)
        self.assertIn('href="#baraj-map"', baraj)
        self.assertIn('href="#baraj-compare"', baraj)
        self.assertIn("Karşılaştır", baraj)
        self.assertIn('href="#baraj-list"', baraj)
        self.assertIn('href="#trendTitle"', uretim)
        self.assertIn('href="#detailsTitle"', uretim)
        tuketim = APP._module_sidebar("tuketim")
        self.assertIn('href="#consumption-summary"', tuketim)
        self.assertIn('href="#consumption-chart"', tuketim)
        self.assertIn('href="#consumption-table"', tuketim)
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
        self.assertEqual(dashboard["summary"]["ptfSmfCommonHours"], 1)
        self.assertEqual(dashboard["summary"]["ptfCommonAverage"], 100)
        self.assertEqual(dashboard["summary"]["smfCommonAverage"], 110)
        self.assertEqual(
            dashboard["summary"]["smfPtfAverageDifference"], 10
        )

    def test_basin_depletion_requires_enough_reliable_observations(self):
        short_series = [
            {"date": "2026-07-20", "average": 70},
            {"date": "2026-07-21", "average": 69},
        ]
        short_analysis = APP._basin_regime_analysis(short_series)
        self.assertIsNone(short_analysis["projectedDepletionDate"])
        self.assertEqual(short_analysis["confidence"], "yetersiz veri")
        self.assertIn("En az 7 yayın", short_analysis["projectionStatus"])

        start = date(2026, 7, 1)
        reliable_series = [
            {
                "date": (start + timedelta(days=index * 2)).isoformat(),
                "average": 80 - index * 2,
            }
            for index in range(8)
        ]
        reliable_analysis = APP._basin_regime_analysis(reliable_series)
        self.assertIsNotNone(reliable_analysis["projectedDepletionDate"])
        self.assertEqual(reliable_analysis["confidence"], "yüksek")
        self.assertIn("Deneysel", reliable_analysis["projectionStatus"])

    def test_basin_regime_analysis_includes_weekly_and_monthly_drop(self):
        start = date(2026, 7, 1)
        series = [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "average": 90 - index,
            }
            for index in range(35)
        ]
        analysis = APP._basin_regime_analysis(series)
        weekly = analysis["periodComparisons"]["weekly"]
        monthly = analysis["periodComparisons"]["monthly"]
        self.assertTrue(weekly["available"])
        self.assertTrue(monthly["available"])
        self.assertEqual(weekly["actualDays"], 7)
        self.assertEqual(monthly["actualDays"], 30)
        self.assertAlmostEqual(weekly["drop"], 7)
        self.assertAlmostEqual(monthly["drop"], 30)
        self.assertAlmostEqual(weekly["change"], -7)

    def test_basin_risk_combines_fullness_decline_and_critical_horizon(self):
        start = date(2026, 7, 1)
        declining = [
            {
                "date": (start + timedelta(days=index * 2)).isoformat(),
                "average": 52 - index * 3,
            }
            for index in range(8)
        ]
        analysis = APP._basin_regime_analysis(declining)
        risk = APP._basin_risk_analysis(declining, analysis)
        self.assertEqual(risk["level"], "Yüksek")
        self.assertGreater(risk["score"], 0)
        self.assertIsNotNone(risk["daysToCritical"])
        self.assertIsNotNone(risk["criticalDate"])
        self.assertIn("weeklyDrop", risk)
        self.assertIn("monthlyDrop", risk)

        stable = [
            {"date": (start + timedelta(days=index * 2)).isoformat(), "average": 82}
            for index in range(8)
        ]
        stable_analysis = APP._basin_regime_analysis(stable)
        stable_risk = APP._basin_risk_analysis(stable, stable_analysis)
        self.assertEqual(stable_risk["level"], "Düşük")
        self.assertIsNone(stable_risk["daysToCritical"])

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

    def test_market_dashboard_rejects_future_dates_before_epias_call(self):
        future = (
            APP.datetime.now(APP.URETIM.TR_TZ).date() + timedelta(days=1)
        ).isoformat()

        class FailClient:
            def _post_json(self, endpoint, body):
                raise AssertionError("Gelecek tarih için EPİAŞ çağrılmamalı")

        with self.assertRaisesRegex(ValueError, "Bugünden ileri"):
            APP._market_dashboard(future, FailClient())

    def test_next_day_ptf_uses_epias_interim_mcp_during_preliminary_window(self):
        selected_day = date(2026, 7, 22)
        target_day = selected_day + timedelta(days=1)
        APP.NEXT_DAY_PTF_CACHE.pop(target_day.isoformat(), None)

        class StubClient:
            def __init__(self):
                self.calls = []

            def _post_json(self, endpoint, body):
                self.calls.append((endpoint, body))
                return {
                    "items": [
                        {
                            "hour": "00:00",
                            "marketTradePrice": 2_500,
                        },
                        {
                            "hour": "01:00",
                            "marketTradePrice": 3_000,
                        },
                    ],
                    "statistic": {
                        "interimMcpAvg": 2_750,
                    },
                }

        client = StubClient()
        payload = APP._next_day_ptf_dashboard(
            selected_day.isoformat(),
            client,
            now_tr=datetime(
                2026, 7, 22, 13, 30, tzinfo=APP.URETIM.TR_TZ
            ),
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(
            client.calls[0][0].endswith("/markets/dam/data/interim-mcp")
        )
        self.assertEqual(
            client.calls[0][1]["startDate"],
            f"{target_day.isoformat()}T00:00:00+03:00",
        )
        self.assertNotIn("endDate", client.calls[0][1])
        self.assertEqual(payload["date"], target_day.isoformat())
        self.assertTrue(payload["published"])
        self.assertEqual(payload["publication"]["status"], "preliminary")
        self.assertEqual(payload["publication"]["source"], "interim-mcp")
        self.assertIn("preliminaryAt", payload["publication"])
        self.assertIn("finalAt", payload["publication"])
        self.assertEqual(payload["summary"]["publishedHours"], 2)
        self.assertEqual(
            payload["summary"]["ptfAverageByCurrency"],
            {"TRY": 2_750, "EUR": None, "USD": None},
        )
        self.assertEqual(
            payload["currencyInfo"]["available"],
            ["TRY"],
        )

    def test_next_day_ptf_does_not_query_interim_before_13(self):
        selected_day = date(2026, 7, 22)
        target_day = selected_day + timedelta(days=1)
        APP.NEXT_DAY_PTF_CACHE.pop(target_day.isoformat(), None)

        class StubClient:
            def _post_json(self, endpoint, body):
                raise AssertionError("13.00 öncesinde PTF servisi çağrılmamalı")

        payload = APP._next_day_ptf_dashboard(
            selected_day.isoformat(),
            StubClient(),
            now_tr=datetime(
                2026, 7, 22, 12, 59, tzinfo=APP.URETIM.TR_TZ
            ),
        )

        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["publication"]["status"], "waiting")
        self.assertIsNone(payload["publication"]["source"])

    def test_next_day_ptf_uses_final_mcp_after_14(self):
        selected_day = date(2026, 7, 22)
        target_day = selected_day + timedelta(days=1)
        APP.NEXT_DAY_PTF_CACHE.pop(target_day.isoformat(), None)

        class StubClient:
            def __init__(self):
                self.calls = []

            def _post_json(self, endpoint, body):
                self.calls.append((endpoint, body))
                return {
                    "items": [{"hour": "00:00", "price": 3_000}],
                    "statistic": {"priceAvg": 3_000},
                }

        client = StubClient()
        payload = APP._next_day_ptf_dashboard(
            selected_day.isoformat(),
            client,
            now_tr=datetime(
                2026, 7, 22, 14, 5, tzinfo=APP.URETIM.TR_TZ
            ),
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0][0].endswith("/markets/dam/data/mcp"))
        self.assertIn("endDate", client.calls[0][1])
        self.assertEqual(payload["publication"]["status"], "final")
        self.assertEqual(payload["publication"]["source"], "mcp")

    def test_next_day_ptf_does_not_fall_back_to_interim_after_14(self):
        selected_day = date(2026, 7, 22)
        target_day = selected_day + timedelta(days=1)
        APP.NEXT_DAY_PTF_CACHE.pop(target_day.isoformat(), None)

        class StubClient:
            def __init__(self):
                self.calls = []

            def _post_json(self, endpoint, body):
                self.calls.append(endpoint)
                return {"items": []}

        client = StubClient()
        payload = APP._next_day_ptf_dashboard(
            selected_day.isoformat(),
            client,
            now_tr=datetime(
                2026, 7, 22, 14, 1, tzinfo=APP.URETIM.TR_TZ
            ),
        )

        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.calls[0].endswith("/markets/dam/data/mcp"))
        self.assertEqual(payload["rows"], [])
        self.assertEqual(payload["publication"]["status"], "final")
        self.assertEqual(payload["publication"]["label"], "Kesinleşmiş PTF")
        self.assertEqual(payload["publication"]["source"], "mcp")
        self.assertEqual(
            payload["publication"]["nextRefreshAt"],
            "2026-07-22T14:03:00+03:00",
        )

    def test_consumption_dashboard_normalizes_epias_hourly_values(self):
        selected_date = "2026-07-20"
        APP.CONSUMPTION_CACHE.pop(selected_date, None)

        class StubClient:
            def _post_json(self, endpoint, body):
                self.endpoint = endpoint
                self.body = body
                return {
                    "items": [
                        {
                            "date": "2026-07-20T00:00:00+03:00",
                            "consumption": 31_000,
                        },
                        {
                            "time": "2026-07-20T01:00:00+03:00",
                            "consumption": 32_500,
                        },
                    ],
                    "statistics": {
                        "consumptionAvg": 31_750,
                        "consumptionMax": 32_500,
                        "consumptionMin": 31_000,
                        "consumptionTotal": 63_500,
                    },
                }

        client = StubClient()
        payload = APP._consumption_dashboard(selected_date, client)
        self.assertEqual(
            client.endpoint,
            "/v1/consumption/data/realtime-consumption",
        )
        self.assertEqual(client.body["startDate"], "2026-07-20T00:00:00+03:00")
        self.assertEqual(client.body["endDate"], "2026-07-20T00:00:00+03:00")
        self.assertEqual(len(payload["rows"]), 24)
        self.assertEqual(payload["rows"][0]["consumption"], 31_000)
        self.assertEqual(payload["rows"][1]["consumption"], 32_500)
        self.assertIsNone(payload["rows"][2]["consumption"])
        self.assertEqual(payload["summary"]["latest"], 32_500)
        self.assertEqual(payload["summary"]["latestHour"], "01:00")
        self.assertEqual(payload["summary"]["availableHours"], 2)
        self.assertEqual(payload["summary"]["missingHours"], 22)
        self.assertEqual(payload["publicationDelayHours"], 2)

    def test_consumption_dashboard_rejects_future_dates(self):
        future = (
            APP.datetime.now(APP.URETIM.TR_TZ).date() + timedelta(days=1)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "Bugünden ileri"):
            APP._consumption_dashboard(future, object())

    def test_consumption_forecast_builds_24_hour_weighted_profile(self):
        today = datetime.now(APP.URETIM.TR_TZ).date()
        training_start = today - timedelta(days=13)

        class StubClient:
            def __init__(self):
                self.calls = []

            def _post_json(self, endpoint, body):
                self.calls.append((endpoint, body))
                items = []
                for day_offset in range(14):
                    current = training_start + timedelta(days=day_offset)
                    for hour in range(24):
                        items.append({
                            "date": f"{current.isoformat()}T{hour:02d}:00:00+03:00",
                            "consumption": 40_000 + hour * 100,
                        })
                return {"items": items}

        APP.CONSUMPTION_FORECAST_CACHE.clear()
        client = StubClient()
        payload = APP._consumption_forecast(today.isoformat(), client)
        self.assertEqual(payload["date"], (today + timedelta(days=1)).isoformat())
        self.assertEqual(payload["summary"]["trainingDays"], 14)
        self.assertEqual(payload["summary"]["forecastHours"], 24)
        self.assertEqual(payload["summary"]["maximumHour"], "23:00")
        self.assertEqual(payload["rows"][3]["forecast"], 40_300)
        self.assertEqual(len(client.calls), 1)

    def test_consumption_forecast_compares_published_actual_hours(self):
        today = datetime.now(APP.URETIM.TR_TZ).date()
        base_day = today - timedelta(days=1)
        training_start = base_day - timedelta(days=13)

        class StubClient:
            def _post_json(self, endpoint, body):
                start = str(body["startDate"])[:10]
                end = str(body["endDate"])[:10]
                if start == end:
                    return {"items": [
                        {"date": f"{today.isoformat()}T00:00:00+03:00", "consumption": 41_000},
                        {"date": f"{today.isoformat()}T01:00:00+03:00", "consumption": 41_100},
                    ]}
                return {"items": [
                    {
                        "date": f"{(training_start + timedelta(days=day_offset)).isoformat()}T{hour:02d}:00:00+03:00",
                        "consumption": 40_000 + hour * 100,
                    }
                    for day_offset in range(14)
                    for hour in range(24)
                ]}

        APP.CONSUMPTION_CACHE.clear()
        APP.CONSUMPTION_FORECAST_CACHE.clear()
        payload = APP._consumption_forecast(base_day.isoformat(), StubClient())
        self.assertEqual(payload["summary"]["actualHours"], 2)
        self.assertEqual(payload["summary"]["comparedHours"], 2)
        self.assertEqual(payload["summary"]["meanAbsoluteError"], 1_000)
        self.assertEqual(payload["rows"][0]["actual"], 41_000)

    def test_consumption_xlsx_contains_summary_and_hourly_sheets(self):
        workbook = APP._consumption_xlsx(
            {
                "date": "2026-07-20",
                "source": "EPİAŞ Şeffaflık Platformu",
                "summary": {
                    "latest": 32_500,
                    "latestHour": "01:00",
                    "average": 31_750,
                    "maximum": 32_500,
                    "maximumHour": "01:00",
                    "minimum": 31_000,
                    "minimumHour": "00:00",
                    "total": 63_500,
                    "availableHours": 2,
                },
                "rows": [
                    {"time": "00:00", "consumption": 31_000},
                    {"time": "01:00", "consumption": 32_500},
                    {"time": "02:00", "consumption": None},
                ],
            }
        )
        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            detail_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn('name="Özet"', workbook_xml)
            self.assertIn('name="Saatlik Tüketim"', workbook_xml)
            self.assertIn("32500", detail_xml)
            self.assertIn("Veri bekleniyor", detail_xml)

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
        self.assertIn(ceyhan["risk"]["level"], {"Yüksek", "Orta", "Düşük"})
        self.assertIn("criticalLevel", ceyhan["risk"])
        self.assertIn("periodComparisons", ceyhan["analysis"])
        self.assertIn("weekly", ceyhan["analysis"]["periodComparisons"])
        self.assertIn("monthly", ceyhan["analysis"]["periodComparisons"])
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
                        "periodComparisons": {
                            "weekly": {
                                "available": True,
                                "drop": 1.5,
                                "baselineDate": "2026-07-14",
                            },
                            "monthly": {
                                "available": True,
                                "drop": 4.2,
                                "baselineDate": "2026-06-24",
                            },
                        },
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
            summary_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
            detail_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
            self.assertIn('name="Baraj Dolulukları"', workbook_xml)
            self.assertIn('name="Havza Ortalaması"', workbook_xml)
            self.assertIn("Haftalık düşüş", summary_xml)
            self.assertIn("Aylık düşüş", summary_xml)
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

    def test_executive_report_builds_branded_html_and_five_sheet_xlsx(self):
        report = {
            "date": "2026-07-22",
            "generatedAt": "2026-07-22T14:20:00+03:00",
            "errors": {},
            "availableModules": ["market", "nextDayPtf", "dams", "production", "consumption"],
            "damSummary": {
                "count": 2,
                "average": 72.5,
                "highest": {"name": "Gölova", "value": 98.2},
                "lowest": {"name": "Yenice", "value": 46.8},
                "source": "EPİAŞ Şeffaflık Platformu",
                "date": "2026-07-22",
                "previousDate": "2026-07-21",
                "previousAverage": 72.92,
                "dailyChange": -0.42,
            },
            "modules": {
                "market": {
                    "summary": {
                        "ptfAverageByCurrency": {"TRY": 3000},
                        "smfAverage": 2900,
                        "yalTotal": 120,
                        "yatTotal": 80,
                    },
                    "rows": [{"time": "00:00", "ptf": 3000, "smf": 2900, "yal": 5, "yat": 3, "direction": "Enerji Açığı"}],
                },
                "nextDayPtf": {
                    "publication": {"label": "Kesinleşmiş PTF"},
                    "summary": {"ptfAverageByCurrency": {"TRY": 3150}},
                },
                "dams": {"items": [
                    {"dam": "Gölova", "basin": "Yeşilırmak", "activeFullnessAmount": 98.2, "date": "2026-07-22"},
                    {"dam": "Yenice", "basin": "Sakarya", "activeFullnessAmount": 46.8, "date": "2026-07-22"},
                ]},
                "production": {
                    "summary": {"uevmTotal": 1000, "uecmTotal": 980, "difference": 20, "deviationPct": 2.04},
                    "groups": [{"label": "Yenilenebilir", "value": 700, "share": 70}],
                    "series": [{"timestamp": "2026-07-22T00:00:00+03:00", "uevm": 50, "uecm": 49, "renewable": 35, "thermal": 12, "naturalGas": 3}],
                },
                "consumption": {
                    "summary": {"latest": 41000, "average": 43000, "maximum": 48000, "maximumHour": "18:00", "availableHours": 20},
                    "rows": [{"time": "00:00", "consumption": 41000}],
                },
            },
        }
        html = APP._executive_report_html(report, auto_print=True)
        self.assertIn("GÜNLÜK YÖNETİCİ RAPORU", html)
        self.assertIn('data-auto-print="true"', html)
        self.assertIn("PDF / Yazdır", html)
        self.assertIn("↓ XLSX", html)
        self.assertIn("Öne çıkan gelişmeler", html)
        highlights = html.split(
            '<section class="report-card report-highlights">', 1
        )[1]
        self.assertIn("SİSTEM DENGESİ", highlights)
        self.assertIn("Enerji Açığı baskın", highlights)
        self.assertIn("ÜRETİM KARMASI", highlights)
        self.assertIn("Yenilenebilir · %70,0", highlights)
        self.assertIn("+%2,04", html)
        self.assertIn("48.000,00 MWh", html)
        self.assertIn("Günlük değişim", html)
        self.assertIn("↓ −0,42 puan", html)
        self.assertIn("21.07.2026 tarihine göre", html)
        self.assertNotIn("PİYASA ZİRVESİ", highlights)
        self.assertNotIn("KRİTİK DOLULUK", highlights)
        self.assertNotIn("Kaynak durumu", html)
        self.assertNotIn("VERİ KALİTESİ", html)
        workbook = APP._executive_xlsx(report)
        self.assertTrue(workbook.startswith(b"PK"))
        with zipfile.ZipFile(io.BytesIO(workbook)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
            self.assertIn('name="Yönetici Özeti"', workbook_xml)
            self.assertIn('name="Piyasa"', workbook_xml)
            self.assertIn('name="Barajlar"', workbook_xml)
            self.assertIn('name="Üretim"', workbook_xml)
            self.assertIn('name="Tüketim"', workbook_xml)

    def test_executive_dashboard_combines_all_modules_for_one_date(self):
        originals = (
            APP._market_dashboard,
            APP._next_day_ptf_dashboard,
            APP._baraj_data,
            APP.URETIM_SERVICE.dashboard,
            APP._consumption_dashboard,
        )
        calls = []
        APP._market_dashboard = lambda selected, client: calls.append(("market", selected)) or {"summary": {}, "rows": []}
        APP._next_day_ptf_dashboard = lambda selected, client: calls.append(("next", selected)) or {"summary": {}, "rows": []}
        def fake_baraj_data(client, selected):
            calls.append(("dams", selected))
            values = (80, 60) if selected == "2026-07-20" else (79, 59)
            return {
                "items": [
                    {"dam": "A", "activeFullnessAmount": values[0]},
                    {"dam": "B", "activeFullnessAmount": values[1]},
                ],
                "availableDates": ["2026-07-19", "2026-07-20"],
                "sourceLabel": "Arşiv",
                "selectedDate": selected,
            }

        APP._baraj_data = fake_baraj_data
        APP.URETIM_SERVICE.dashboard = lambda date_range, client: calls.append(("production", date_range.start.isoformat(), date_range.end.isoformat())) or {"summary": {}, "series": []}
        APP._consumption_dashboard = lambda selected, client: calls.append(("consumption", selected)) or {"summary": {}, "rows": []}
        try:
            report = APP._executive_dashboard("2026-07-20", object())
        finally:
            (
                APP._market_dashboard,
                APP._next_day_ptf_dashboard,
                APP._baraj_data,
                APP.URETIM_SERVICE.dashboard,
                APP._consumption_dashboard,
            ) = originals

        self.assertEqual(report["damSummary"]["average"], 70)
        self.assertEqual(report["damSummary"]["highest"]["name"], "A")
        self.assertEqual(report["damSummary"]["lowest"]["name"], "B")
        self.assertEqual(report["damSummary"]["previousDate"], "2026-07-19")
        self.assertEqual(report["damSummary"]["dailyChange"], 1)
        self.assertEqual(len(report["availableModules"]), 5)
        self.assertIn(("production", "2026-07-20", "2026-07-20"), calls)


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
            "/tuketim/api/session",
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

    def test_login_endpoint_rate_limits_repeated_failures_by_ip(self):
        original = APP.LOGIN_LIMITER
        limiter = APP.LoginRateLimiter(
            max_attempts=1,
            window_seconds=60,
            block_seconds=45,
        )
        limiter.record_failure("127.0.0.1")
        APP.LOGIN_LIMITER = limiter
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            body = json.dumps(
                {"username": "test@example.com", "password": "wrong"}
            )
            connection.request(
                "POST",
                "/api/login",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            self.assertEqual(response.status, 429)
            self.assertEqual(response.getheader("Retry-After"), "45")
            self.assertIn("Çok fazla", payload["error"])
        finally:
            APP.LOGIN_LIMITER = original
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
        self.assertIn('href="/suite-assets/icon-192.png?v=2"', html)
        self.assertIn('href="/favicon.ico?v=2"', html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png?v=2"',
            html,
        )
        self.assertIn("BAHA ENERJİ YÖNETİM PANELİ", html)
        self.assertIn("Enerjinin nabzı", html)
        self.assertIn("4</strong><span>entegre panel", html)
        self.assertIn("Gerçek Zamanlı Tüketim", html)
        self.assertIn("<span>Tüketim</span>", html)
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
        self.assertIn("baha-enerji-shell-v14", worker)
        self.assertNotIn("baha-uretim-logo.svg", worker)

        for path in (
            "/suite-assets/icon-192.png",
            "/suite-assets/icon-512.png",
            "/suite-assets/apple-touch-icon.png",
            "/favicon.ico",
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

    def test_logged_out_page_is_public_and_links_back_to_login(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request("GET", "/oturum-kapatildi")
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("<title>Oturum Kapatıldı | Baha Enerji</title>", html)
        self.assertIn("Oturum başarıyla kapatıldı.", html)
        self.assertIn(
            "Tarayıcınızı kapatabilirsiniz, ya da tekrar giriş yapabilirsiniz.",
            html,
        )
        self.assertIn('class="logout-login-link" href="/login"', html)
        self.assertIn("Tekrar giriş yap", html)
        self.assertNotIn('class="baha-suite-nav"', html)
        connection.close()

        status, content, headers = self.get("/oturum-kapatildi.css")
        css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(".logout-card", css)
        self.assertIn(".logout-login-link", css)
        self.assertEqual(headers.get_content_type(), "text/css")

    def test_logout_revokes_session_before_showing_confirmation(self):
        token = APP.AUTH.create_session("logout@baha.local", "TGT-logout")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        connection.request(
            "POST",
            "/api/logout",
            headers={"Cookie": f"{APP.AUTH.cookie_name}={token}"},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertFalse(payload["authenticated"])
        self.assertIn("Max-Age=0", response.getheader("Set-Cookie"))
        self.assertIsNone(APP.AUTH.get_session(token))
        connection.close()

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
        self.assertIn('src="/piyasa/app.js?v=42"', html)
        self.assertIn('value="year-weekday"', html)
        self.assertIn("Önceki yıl · aynı hafta ve gün", html)
        self.assertIn(
            'class="suite-footer" data-suite-footer="piyasa"',
            html,
        )
        self.assertIn("BAHA<br>ENERJ&#304;", html)
        self.assertIn('id="piyasaFooterUpdated"', html)
        self.assertIn('href="/piyasa/styles.css?v=25"', html)
        self.assertIn('href="/piyasa/" aria-current="page"', html)
        self.assertIn('src="/piyasa-charts.js?v=9"', html)
        self.assertIn('href="/piyasa-suite.css?v=28"', html)
        self.assertIn('src="/theme-sync.js?v=2"', html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png?v=2"', html
        )
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
        self.assertIn('href="#next-day-ptf"', html)
        self.assertIn('id="next-day-ptf-chart"', html)
        self.assertIn('id="next-day-ptf-status"', html)
        self.assertIn('class="next-day-ptf-schedule"', html)
        self.assertIn("kesinleşmemiş PTF saat 13.00’te", html)
        self.assertIn("kesinleşmiş PTF ise saat 14.00’te", html)

    def test_piyasa_never_shows_its_old_login_screen(self):
        status, content, _ = self.get("/piyasa/app.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertNotIn("show('login')", script)
        self.assertIn("window.location.replace('/login')", script)
        self.assertIn("window.location.replace('/oturum-kapatildi')", script)
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
        self.assertIn("constrainMarketDate", script)
        self.assertIn("input.max=today", script)
        self.assertIn("Bugünden ileri bir tarih seçilemez.", script)
        self.assertIn("/piyasa/api/next-day-ptf", script)
        self.assertIn("renderNextDayPtf", script)
        self.assertIn("scheduleNextDayPtfRefresh", script)
        self.assertIn("Kesinleşmemiş PTF", script)
        self.assertIn("Kesinleşmiş PTF", script)
        self.assertIn("baha-sidebar-collapsed", script)
        self.assertIn("desktopSidebar", script)
        self.assertIn("setDesktopSidebarHover", script)
        self.assertIn("suite-sidebar-hovered", script)
        self.assertIn("mouseenter", script)
        self.assertNotIn("sharedMarketDate", script)
        self.assertNotIn("BahaDateSync", script)
        self.assertIn("window.BahaTracking?.publish", script)
        self.assertIn("desktopSidebarPointerInside", script)
        self.assertIn("clearMarketDashboard", script)
        self.assertIn("ptfSmfCommonHours", script)
        self.assertIn("yalnızca ${commonHours} ortak saatte", script)
        self.assertIn("setMarketConnectionPart('main','error'", script)
        self.assertIn("previousYearSameWeekday", script)
        self.assertIn("isoWeekParts", script)
        self.assertIn("dateFromIsoWeek", script)
        self.assertIn("önceki yılın aynı hafta ve gününe", script)
        self.assertIn("comparisonBaseline", script)

    def test_piyasa_local_chart_and_styles_are_served(self):
        status, content, headers = self.get("/piyasa-charts.js")
        self.assertEqual(status, 200)
        chart_script = content.decode("utf-8")
        self.assertIn("window.ApexCharts = LocalEnergyChart", chart_script)
        self.assertIn("tooltip.offsetHeight", chart_script)
        self.assertIn("spaceAbove < tooltipHeight", chart_script)
        self.assertIn("tooltip.dataset.placement", chart_script)
        self.assertIn("const compact = width <= 700", chart_script)
        self.assertIn("const labelStep = Math.max", chart_script)
        self.assertIn("const labelIndexes", chart_script)
        self.assertIn("const visualRatioFor", chart_script)
        self.assertIn("labelIndexes.length - 1", chart_script)
        self.assertIn("closestDistance", chart_script)
        self.assertIn("xaxisLabels.compactFontSize", chart_script)
        self.assertIn('class: "energy-chart-x-label"', chart_script)
        self.assertIn(
            "index % labelStep === 0 || index === categories.length - 1",
            chart_script,
        )
        self.assertEqual(headers.get_content_type(), "text/javascript")

    def test_all_panels_include_shared_chart_fullscreen_controls(self):
        for path in ("/piyasa/", "/baraj/", "/uretim/", "/tuketim/"):
            with self.subTest(path=path):
                status, content, _ = self.get(path)
                html = content.decode("utf-8")
                self.assertEqual(status, 200)
                self.assertIn('href="/chart-fullscreen.css?v=2"', html)
                self.assertIn('src="/chart-fullscreen.js?v=2"', html)

        status, content, headers = self.get("/chart-fullscreen.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("#hourly-data", script)
        self.assertIn("#basinRegimeChart", script)
        self.assertIn(".trend-panel", script)
        self.assertIn("#consumption-forecast", script)
        self.assertIn("suite-chart-maximized", script)
        self.assertIn("suite-chart-viewer-canvas", script)
        self.assertIn("viewerCanvas.replaceChildren", script)
        self.assertIn("setApexHeight", script)
        self.assertNotIn('panel: "#direction-chart"', script)
        self.assertIn('event.key === "Escape"', script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

        status, content, headers = self.get("/chart-fullscreen.css")
        css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(".suite-chart-fullscreen-button", css)
        self.assertIn(".suite-chart-maximized", css)
        self.assertIn(".suite-chart-viewer-canvas", css)
        self.assertIn('[data-chart-kind="apex"]', css)
        self.assertIn("height: 100% !important", css)
        self.assertIn("env(safe-area-inset-top", css)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertEqual(headers.get_content_type(), "text/css")

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
        self.assertIn("grid-template-columns: 42px minmax(0, 1fr) 42px", css)
        self.assertIn(".baha-suite-piyasa .last-update", css)
        self.assertIn("display: none", css)
        self.assertIn("max-width: calc(100% - 16px)", css)
        self.assertIn("transform: none", css)
        self.assertIn("transform: translateX(-105%)", css)
        self.assertIn("translateX(calc(-100% + 22px))", css)
        self.assertIn("suite-sidebar-hovered", css)
        self.assertIn("z-index: 100030", css)
        self.assertIn("display: grid !important", css)
        self.assertIn(".baha-suite-piyasa #xlsx-button", css)
        self.assertIn(".baha-suite-piyasa #today-button", css)
        self.assertIn(".baha-suite-piyasa .metric-head i", css)
        self.assertIn("padding: 8px 0 16px", css)
        self.assertIn(
            ".baha-suite-piyasa.suite-sidebar-collapsed .main-content",
            css,
        )
        self.assertIn(".baha-suite-piyasa #quantity-chart", css)
        self.assertIn("margin-inline: -10px", css)
        self.assertIn(".baha-suite-piyasa .direction-hour strong", css)
        self.assertIn("writing-mode: horizontal-tb", css)
        self.assertIn(".baha-suite-piyasa .direction-hour span", css)
        self.assertIn(".baha-suite-piyasa .table th:last-child", css)
        self.assertIn('input[type="date"]::-webkit-calendar-picker-indicator', css)
        self.assertIn(".date-nav button:disabled", css)
        self.assertIn("border-color: #47658f", css)
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
        self.assertIn(".live-dot.warning i", css)
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
        self.assertIn("window.location.replace('/oturum-kapatildi')", html)
        self.assertIn(
            'class="page page-center login-screen" style="display:none!important"',
            html,
        )
        self.assertIn('id="dashboard" class="page"', html)
        self.assertIn('id="baraj-summary"', html)
        self.assertIn('id="baraj-list"', html)
        self.assertIn('id="baraj-regime"', html)
        self.assertIn('id="basin-risk"', html)
        self.assertIn('id="basinRiskBody"', html)
        self.assertIn("renderBasinRisks", html)
        self.assertIn("7 gün düşüş", html)
        self.assertIn("30 gün düşüş", html)
        self.assertIn('id="basinWeeklyDrop"', html)
        self.assertIn('id="basinMonthlyDrop"', html)
        self.assertIn("periodDropText", html)
        self.assertIn("periodComparisons.weekly", html)
        self.assertIn("%30 kritik seviye", html)
        self.assertIn('id="baraj-map"', html)
        self.assertIn('id="turkeyMapShape"', html)
        self.assertIn('id="basinMapDams"', html)
        self.assertIn("polygonLabelPoint(feature)", html)
        self.assertIn("geometryPath(feature.geometry)", html)
        self.assertIn("/baraj/turkiye-havzalari.geojson", html)
        self.assertIn("baraj-basin-shape", html)
        self.assertNotIn("BASIN_MAP_COORDINATES", html)
        self.assertNotIn("TURKEY_MAP_LAKES", html)
        self.assertNotIn("baraj-map-lakes", html)
        self.assertNotIn("baraj-map-coordinate-grid", html)
        self.assertNotIn("function layoutBasinMapLabels()", html)
        self.assertIn("group.append(path, label); svg.append(group)", html)
        self.assertIn("renderBasinMap(selected || null)", html)
        self.assertNotIn("Natural Earth 1:110m", html)
        self.assertIn("T.C. Tarım ve Orman Bakanlığı CBS", html)
        self.assertIn('id="baraj-compare"', html)
        self.assertIn('data-compare-mode="date"', html)
        self.assertIn('id="compareFirst"', html)
        self.assertIn('id="compareSecond"', html)
        self.assertIn('id="basinSelect"', html)
        self.assertIn('id="basinMapPanelToggle"', html)
        self.assertIn("$('basinSelect').addEventListener('change', renderBasinHistory)", html)
        self.assertNotIn("setBasinMapPanel(true)", html)
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
        self.assertNotIn("BahaDateSync", html)
        self.assertIn("window.BahaTracking?.publish", html)
        self.assertIn("option.textContent = displayDate(value)", html)
        self.assertNotIn("archiveDates.has(value) ? 'Arşiv' : 'EPİAŞ'", html)
        self.assertNotIn('id="barajDataSource"', html)
        self.assertIn("/baraj/api/export.xlsx?", html)
        self.assertIn("Doluluk: yüksekten düşüğe", html)
        self.assertIn("Baraj adı: A–Z", html)
        self.assertIn("Basit ort. doluluk", html)
        self.assertIn("BASİT ORTALAMA AKTİF DOLULUK", html)

        self.assertIn("clearBarajOverview", html)
        self.assertIn("setConnectionState('error'", html)
        self.assertIn("Deneysel tükenme", html)
        self.assertIn('class="suite-sidebar"', html)
        self.assertIn("data-suite-theme-toggle", html)
        self.assertIn("EPİAŞ · EPİAŞ canlı", html)
        self.assertIn('class="suite-menu-close"', html)
        self.assertIn('href="/module-suite.css?v=34"', html)
        self.assertIn('src="/module-suite.js?v=7"', html)
        self.assertIn('src="/theme-sync.js?v=2"', html)
        self.assertNotIn('src="/date-sync.js', html)
        self.assertNotIn("tracking-center.js", html)
        self.assertNotIn("data-tracking-toggle", html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png?v=2"', html
        )
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
        self.assertIn('href="/portal-shell.css?v=6"', html)
        self.assertIn('class="suite-command-toggle"', html)
        self.assertIn('class="suite-command-menu"', html)
        self.assertIn('src="/command-center.js?v=2"', html)

    def test_baraj_serves_official_basin_geojson_locally(self):
        status, content, headers = self.get("/baraj/turkiye-havzalari.geojson")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "application/geo+json")
        payload = json.loads(content.decode("utf-8"))
        self.assertEqual(payload["type"], "FeatureCollection")
        self.assertEqual(len(payload["features"]), 25)
        names = {
            feature["properties"]["HAVZA_ADI"]
            for feature in payload["features"]
        }
        self.assertIn("Kızılırmak", names)
        self.assertIn("Fırat - Dicle", names)
        self.assertTrue(
            all(
                feature["geometry"]["type"] in {"Polygon", "MultiPolygon"}
                for feature in payload["features"]
            )
        )

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

    def test_consumption_page_and_assets_are_mounted_under_tuketim(self):
        status, content, _ = self.get("/tuketim/")
        html = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("<title>Baha Tüketim Paneli</title>", html)
        self.assertIn('class="baha-suite-page baha-suite-tuketim"', html)
        self.assertIn('href="/tuketim/" aria-current="page"', html)
        self.assertIn('href="/tuketim/consumption.css?v=4"', html)
        self.assertIn('src="/tuketim/consumption.js?v=7"', html)
        self.assertIn('id="consumption-summary"', html)
        self.assertIn('id="consumption-chart"', html)
        self.assertIn('id="consumption-forecast"', html)
        self.assertIn('id="consumptionForecastChart"', html)
        self.assertIn('id="consumption-table"', html)
        self.assertIn('data-suite-theme-toggle', html)
        self.assertIn('class="suite-menu-close"', html)
        self.assertIn('href="/module-suite.css?v=34"', html)
        self.assertIn(
            'class="suite-footer" data-suite-footer="tuketim"',
            html,
        )
        self.assertIn('id="consumptionFooterUpdated"', html)
        self.assertNotIn('class="consumption-live-status"', html)
        self.assertNotIn("EPİAŞ canlı veri", html)

        status, content, headers = self.get("/tuketim/consumption.css")
        css = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn(".consumption-kpis", css)
        self.assertIn(".consumption-hours", css)
        self.assertIn(".consumption-forecast-summary", css)
        self.assertIn(".consumption-kpi > div i", css)
        self.assertIn("background: rgba(55, 113, 221, .14)", css)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn("@media (max-width: 820px)", css)
        self.assertEqual(headers.get_content_type(), "text/css")

        status, content, headers = self.get("/tuketim/consumption.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("/tuketim/api/data?date=", script)
        self.assertIn("/tuketim/api/forecast?baseDate=", script)
        self.assertIn("/tuketim/api/export.xlsx?date=", script)
        self.assertIn("input.max = today", script)
        self.assertIn("baha:themechange", script)
        self.assertNotIn("BahaDateSync", script)
        self.assertIn("window.BahaTracking?.publish", script)
        self.assertIn("clearConsumptionDashboard", script)
        self.assertIn('setConnectionState("error"', script)
        self.assertIn("comparisonFailed", script)
        self.assertIn("renderForecast", script)
        self.assertIn('compactFontSize: "7.2"', script)
        self.assertIn("labels: {step: 3", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

    def test_consumption_api_and_xlsx_use_shared_epias_session(self):
        fixture = {
            "date": "2026-07-20",
            "source": "EPÄ°AÅ ÅeffaflÄ±k Platformu",
            "updatedAt": "2026-07-20T10:00:00Z",
            "summary": {
                "latest": 32_500,
                "latestHour": "01:00",
                "average": 31_750,
                "maximum": 32_500,
                "maximumHour": "01:00",
                "minimum": 31_000,
                "minimumHour": "00:00",
                "total": 63_500,
                "availableHours": 2,
                "missingHours": 22,
            },
            "rows": [
                {"hour": 0, "time": "00:00", "consumption": 31_000},
                {"hour": 1, "time": "01:00", "consumption": 32_500},
            ],
        }
        forecast_fixture = {
            "baseDate": "2026-07-20",
            "date": "2026-07-21",
            "summary": {"forecastHours": 24, "trainingDays": 14},
            "rows": [{"hour": 0, "time": "00:00", "forecast": 32_000}],
        }
        original = APP._consumption_dashboard
        original_forecast = APP._consumption_forecast
        APP._consumption_dashboard = lambda *_args, **_kwargs: fixture
        APP._consumption_forecast = lambda *_args, **_kwargs: forecast_fixture
        try:
            status, content, _ = self.get(
                "/tuketim/api/data?date=2026-07-20"
            )
            payload = json.loads(content)
            self.assertEqual(status, 200)
            self.assertEqual(payload["summary"]["latest"], 32_500)

            status, content, _ = self.get(
                "/tuketim/api/forecast?baseDate=2026-07-20"
            )
            forecast_payload = json.loads(content)
            self.assertEqual(status, 200)
            self.assertEqual(forecast_payload["summary"]["forecastHours"], 24)

            status, content, headers = self.get(
                "/tuketim/api/export.xlsx?date=2026-07-20"
            )
        finally:
            APP._consumption_dashboard = original
            APP._consumption_forecast = original_forecast

        self.assertEqual(status, 200)
        self.assertIn(
            "baha-enerji-tuketim-2026-07-20.xlsx",
            headers["Content-Disposition"],
        )
        self.assertTrue(content.startswith(b"PK"))

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
        self.assertLess(
            script.index("<td>${groupLabels[source.group]}</td>"),
            script.index('<td><span class="source-name">'),
        )
        self.assertIn("constrainDateRange", script)
        self.assertIn("elements.start.max = today", script)
        self.assertIn("elements.end.max = today", script)
        self.assertNotIn("${kpi.index}", script)
        self.assertIn("clearDashboard", script)
        self.assertIn('window.location.replace("/oturum-kapatildi")', script)
        self.assertIn('publishConnectionState("error"', script)
        self.assertIn("data.period.comparableHours < data.period.hours", script)
        self.assertNotIn("BahaDateSync", script)
        self.assertIn("window.BahaTracking?.publish", script)
        self.assertNotIn("elements.presets", script)

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
        self.assertLess(html.index("<th>Grup</th>"), html.index("<th>Kaynak</th>"))
        self.assertIn('href="/module-suite.css?v=34"', html)
        self.assertNotIn('data-range="30"', html)
        self.assertNotIn("Son 30 g\u00fcn", html)
        self.assertIn('src="/module-suite.js?v=7"', html)
        self.assertIn('src="/theme-sync.js?v=2"', html)
        self.assertIn('src="/uretim/app.js?v=9"', html)
        self.assertIn('href="/uretim/manifest.webmanifest"', html)
        self.assertIn(
            'href="/suite-assets/apple-touch-icon.png?v=2"',
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
        self.assertIn("suite-motion-enabled", css)
        self.assertIn("suiteSectionArrive", css)
        self.assertNotIn(".suite-tracking-drawer", css)
        self.assertNotIn(".suite-tracking-toggle", css)

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
        self.assertIn(".baha-suite-baraj .baraj-map-card", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-feature.active", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-shape", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-map-label", css)
        self.assertIn(".baha-suite-baraj .baraj-map-dams", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-detail", css)
        self.assertIn(".baha-suite-baraj .baraj-basin-table", css)
        self.assertIn(".baha-suite-baraj .baraj-risk-table td.neutral", css)
        self.assertIn("repeat(auto-fit, minmax(160px, 1fr))", css)
        self.assertIn(".baha-suite-baraj #baraj-compare", css)
        self.assertIn(".baha-suite-baraj .baraj-compare-results", css)
        self.assertIn(".baha-suite-baraj .baraj-compare-difference", css)
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
        self.assertIn('"start end"', css)
        self.assertIn('"submit submit"', css)
        self.assertIn(".date-form label:nth-of-type(2)", css)
        self.assertIn("grid-area: submit", css)
        self.assertIn("@media (min-width: 740px) and (max-width: 1220px)", css)
        self.assertIn("grid-template-columns: minmax(270px, .85fr)", css)
        self.assertIn("width: clamp(270px, 30vw, 330px)", css)
        self.assertIn(".baha-suite-uretim .mix-panel .donut-layout", css)
        self.assertIn(".baha-suite-uretim .balance-panel .balance-copy", css)
        self.assertIn(
            'html[data-theme="dark"] .baha-suite-uretim .balance-panel',
            css,
        )
        self.assertIn("background: #fff", css)
        self.assertIn(
            'html[data-theme="dark"] .baha-suite-uretim .balance-panel {\n'
            "  border-color: #293850;\n"
            "  color: #fff;\n"
            "  background: #121e33;\n"
            "}",
            css,
        )
        self.assertIn("max-width: 520px", css)
        self.assertIn("min-height: 58px", css)
        self.assertIn("flex: 1 1 auto", css)
        self.assertIn("grid-template-rows: repeat(4, minmax(58px, 1fr))", css)
        self.assertIn("overflow-wrap: normal", css)
        self.assertIn("word-break: normal", css)
        self.assertIn("#barajDateSelect", css)
        self.assertIn('input[type="date"]::-webkit-calendar-picker-indicator', css)
        self.assertIn("border-color: #47658f", css)
        self.assertIn('html[data-theme="dark"]', css)
        self.assertIn('grid-template-areas:', css)
        self.assertIn('"components share"', css)
        self.assertIn(".suite-live-dot.warning i", css)
        self.assertIn(".source-card:nth-child(1) .source-icon", css)
        self.assertIn("rgba(218, 157, 35, .16)", css)
        self.assertIn(".source-share", css)
        self.assertNotIn(".baha-suite-uretim .preset", css)
        self.assertEqual(headers.get_content_type(), "text/css")

        status, content, headers = self.get("/module-suite.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("suite-sidebar-open", script)
        self.assertIn("suite-sidebar-collapsed", script)
        self.assertIn("suite-sidebar-hovered", script)
        self.assertIn("setDesktopSidebarHover", script)
        self.assertIn("desktopPointerInside", script)
        self.assertIn('sidebar.addEventListener("mouseenter"', script)
        self.assertIn('"baha-sidebar-collapsed"', script)
        self.assertIn('window.matchMedia("(min-width: 821px)")', script)
        self.assertIn('fetch("/api/session"', script)
        self.assertIn("EPİAŞ · EPİAŞ canlı", script)
        self.assertIn("baha:connectionstate", script)
        self.assertIn('body.dataset.epiasState', script)
        self.assertIn("EPİAŞ · Eksik veri", script)
        self.assertIn("navigationLockUntil", script)
        self.assertIn("getBoundingClientRect", script)
        self.assertIn("suite-section-arriving", script)
        self.assertIn('window.location.replace("/oturum-kapatildi")', script)
        self.assertNotIn("IntersectionObserver", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

        status, content, headers = self.get("/theme-sync.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn('const STORAGE_KEY = "baha-theme"', script)
        self.assertIn('window.addEventListener("storage"', script)
        self.assertIn("window.BahaTheme", script)
        self.assertIn("suite-page-leaving", script)
        self.assertIn("connectPageMotion", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

    def test_tv_mode_and_command_center_assets_are_served(self):
        status, content, headers = self.get("/tv/")
        html = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("ENERJİ KOMUTA MERKEZİ", html)
        self.assertEqual(html.count('data-slide="'), 5)
        self.assertIn('id="tvFullscreen"', html)
        self.assertIn('href="tv.css?v=2"', html)
        self.assertIn('src="tv.js?v=2"', html)
        self.assertIn('class="tv-side-kpis tv-market-kpis"', html)
        self.assertIn('class="tv-side-kpis tv-consumption-kpis"', html)
        self.assertEqual(headers.get_content_type(), "text/html")

        status, content, headers = self.get("/tv/tv.js")
        script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("/api/command-center?date=", script)
        self.assertIn("requestFullscreen", script)
        self.assertIn("ROTATE_MS = 15000", script)
        self.assertIn('addEventListener("touchstart"', script)
        self.assertIn('addEventListener("touchend"', script)
        self.assertIn("touchStartX", script)
        self.assertEqual(headers.get_content_type(), "text/javascript")

        status, content, headers = self.get("/tv/tv.css")
        stylesheet = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("@media(max-width:600px)", stylesheet)
        self.assertIn("100dvh", stylesheet)
        self.assertIn(".tv-market-kpis", stylesheet)
        self.assertIn(".tv-consumption-kpis", stylesheet)
        self.assertEqual(headers.get_content_type(), "text/css")

        status, content, _ = self.get("/command-center.js")
        command_script = content.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("data-suite-report-link", command_script)
        self.assertIn("/rapor?date=", command_script)
        self.assertIn("suite-command-toggle", command_script)
        self.assertIn("setCommandMenu", command_script)
        self.assertIn("aria-expanded", command_script)

    def test_executive_report_http_page_and_xlsx_endpoint(self):
        fake_report = {
            "date": "2026-07-22",
            "generatedAt": "2026-07-22T14:20:00+03:00",
            "modules": {},
            "damSummary": {},
            "errors": {"market": "Örnek uyarı"},
            "availableModules": [],
        }
        original = APP._executive_dashboard
        APP._executive_dashboard = lambda selected_date, client: {
            **fake_report,
            "date": selected_date,
        }
        try:
            status, content, headers = self.get(
                "/rapor?date=2026-07-22&print=1"
            )
            html = content.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn("GÜNLÜK YÖNETİCİ RAPORU", html)
            self.assertIn('data-auto-print="true"', html)
            self.assertEqual(headers.get_content_type(), "text/html")

            status, content, headers = self.get(
                "/api/executive-report.xlsx?date=2026-07-22"
            )
            self.assertEqual(status, 200)
            self.assertTrue(content.startswith(b"PK"))
            self.assertIn(
                "baha-enerji-yonetici-raporu-2026-07-22.xlsx",
                headers.get("Content-Disposition", ""),
            )
        finally:
            APP._executive_dashboard = original


if __name__ == "__main__":
    unittest.main()
