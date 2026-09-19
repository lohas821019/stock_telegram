import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

import scanner
import main_smc


class RsiRuleTests(unittest.TestCase):
    def setUp(self):
        self.settings = scanner.Settings("token", "chat")

    def test_rsi_for_rising_prices(self):
        self.assertEqual(scanner.calculate_rsi(pd.DataFrame({"Close": range(1, 31)}), 14), 100.0)

    def test_transition_sends_only_on_change(self):
        state = {}
        first = scanner.rsi_transition_signals("tw", "2330.TW", {"day": 25.0}, self.settings, state)
        repeated = scanner.rsi_transition_signals("tw", "2330.TW", {"day": 24.0}, self.settings, state)
        recovered = scanner.rsi_transition_signals("tw", "2330.TW", {"day": 45.0}, self.settings, state)
        self.assertEqual(first[0].direction, "oversold")
        self.assertEqual(repeated, [])
        self.assertEqual(recovered[0].direction, "normal")

    def test_market_ticker_rules(self):
        self.assertEqual(scanner.MARKETS["tw"].ticker_candidates("2330"), ["2330.TW", "2330.TWO"])
        self.assertEqual(scanner.MARKETS["us"].ticker_candidates("NVDA"), ["NVDA"])

    def test_official_name_parser(self):
        page = "<tr><td>2330&nbsp; 台積電</td><td>TW0002330008</td></tr>"
        self.assertEqual(scanner.parse_twse_names(page), {"2330": "台積電"})

    def test_etf_indicator_summary_contains_only_tracked_etfs(self):
        results = [
            {"ticker": "0050.TW", "name": "元大台灣50", "price": 200.0, "kd": {"day": 45.5, "week": 52.0}, "rsi": {"day": 48.3, "week": 51.1}},
            {"ticker": "0052.TW", "name": "富邦科技", "price": 250.0, "kd": {"day": 35.0, "week": 40.0}, "rsi": {"day": 42.0, "week": 46.0}},
            {"ticker": "2330.TW", "name": "台積電", "price": 1000.0, "kd": {"day": 55.0, "week": 60.0}, "rsi": {"day": 58.0, "week": 62.0}},
            {"ticker": "006208.TW", "name": "富邦台50", "price": 150.0, "kd": {"day": None, "week": 42.0}, "rsi": {"day": 39.0, "week": None}},
        ]
        summary = scanner.build_etf_indicator_summary(results)
        self.assertIn("0050 元大台灣50", summary)
        self.assertIn("0052 富邦科技", summary)
        self.assertIn("006208 富邦台50", summary)
        self.assertIn("日 KD: -- | RSI: 39.0", summary)
        self.assertNotIn("2330", summary)

    def test_report_settings_support_global_and_market_overrides(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text("""[Telegram]
token = token
chat_id = chat
[Reports]
enabled = rsi, completion
tw_enabled = etf, completion
""", encoding="utf-8")
            settings = scanner.load_settings(config_path)
        self.assertEqual(settings.enabled_reports, ("rsi", "completion"))
        self.assertEqual(settings.market_reports["tw"], ("etf", "completion"))

    def test_smc_is_a_taiwan_only_report_category(self):
        settings = scanner.Settings("token", "chat", enabled_reports=("smc",))
        self.assertIn("smc", scanner.enabled_reports_for_market(scanner.MARKETS["tw"], settings))
        self.assertNotIn("smc", scanner.enabled_reports_for_market(scanner.MARKETS["us"], settings))
        self.assertIn("smc", scanner.REPORT_TYPES)

    def test_smc_dashboard_html_contains_hit_data_and_filters(self):
        records = [{"ticker": "2330.TW", "name": "台積電", "group": "半導體", "price": "1,000", "change": 2.5, "dayK": "65.0", "weekK": "59.0", "chart": None, "url": "https://example.test", "signals": [{"type": "結構（BOS／CHoCH）", "direction": "多方", "timeframe": "日K", "text": "突破 990"}]}]
        page = main_smc.build_smc_dashboard_html(records, 1, "2026-09-06 12:00:00")
        self.assertIn("台積電", page)
        self.assertIn("結構（BOS／CHoCH）", page)
        self.assertIn('id="search"', page)
        self.assertIn("2026-09-06 12:00:00", page)

    def test_smc_uses_chinese_presentation_and_taiwan_yahoo_link(self):
        self.assertEqual(main_smc.yahoo_url("2330.TW"), "https://tw.stock.yahoo.com/quote/2330/")
        self.assertEqual(main_smc.direction_label("Bullish"), "多方")
        self.assertEqual(main_smc.scope_label("Swing"), "波段結構")

    def test_smc_can_update_web_without_telegram(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.ini"
            config_path.write_text("""[Telegram]
token = token
chat_id = chat
[Settings]
smc_send_telegram = false
smc_write_dashboard = true
""", encoding="utf-8")
            with patch.object(main_smc, "CONFIG_FILE", str(config_path)):
                settings = main_smc.load_settings()
        self.assertFalse(settings.send_telegram)
        self.assertTrue(settings.write_dashboard)

    def test_report_messages_are_addressable_by_category(self):
        market = scanner.MARKETS["tw"]
        messages = scanner.build_report_messages(market, [], ["2330"], self.settings)
        self.assertEqual(set(messages), {"completion"})


if __name__ == "__main__":
    unittest.main()
