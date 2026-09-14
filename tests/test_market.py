import datetime
import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import market

_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class FakeFM:
    """照 ipo-earnings/fetch_market.py 实测的真实返回结构(2026-09-14 联网探测 + 读源码确认):

    - fetch_a_ipo(today) 返回 dict {"upcoming": [...], "ballot_today": [...]},
      不是 list。"upcoming" 每项含 name / apply_date(申购日历用这个)。
      "ballot_today" 是"今日中签公布",不属于日历,要排除。
    - fetch_hk_ipo() 返回 list,每项没有 deadline 字段,只有
      listing_date(格式 "YYYY/MM/DD",上市日)和 status(模糊文本,如"認購中")。
    - fetch_us_calendar(day, watch) 返回 list,每项没有 date 字段(日期由调用方
      按查询的 day 贴上去);每项字段是 symbol/name/time/eps_forecast/
      last_year_eps/fiscal_quarter。__file__ 指向 fixtures 目录,让
      build_market 能 open() 到一个真实存在的 watchlist.yaml(内容不重要,
      parse_watchlist 在这里不做真实解析)。
    """
    __file__ = os.path.join(_FIXTURES_DIR, "fake_fetch_market.py")

    @staticmethod
    def beijing_today():
        return datetime.date(2026, 9, 13)

    @staticmethod
    def fetch_a_ipo(today):
        return {
            "upcoming": [
                {"code": "001", "name": "某公司", "apply_date": "2026-09-15"},
            ],
            "ballot_today": [
                {"code": "002", "name": "某中签公司", "apply_date": "2026-09-10"},
            ],
        }

    @staticmethod
    def fetch_hk_ipo():
        return [
            {"code": "00001", "name": "某港股", "listing_date": "2026/09/16",
             "status": "認購中"},
        ]

    @staticmethod
    def parse_watchlist(text):
        return ["AAPL"]  # 真实解析已在 ipo-earnings 联网探测过,这里只当占位

    @staticmethod
    def fetch_us_calendar(day, watch):
        if day == datetime.date(2026, 9, 14):
            return [{"symbol": "AAPL", "name": "苹果", "time": "盘后",
                     "eps_forecast": "$1.50", "last_year_eps": "$1.40",
                     "fiscal_quarter": "Sep/2026"}]
        return []


class TestBuildMarket(unittest.TestCase):
    def test_ipo_entries_have_name_date_note(self):
        m = market.build_market(FakeFM, polymarket=[])
        self.assertTrue(m["ipo"])
        e = m["ipo"][0]
        self.assertIn("name", e)
        self.assertIn("date", e)
        self.assertIn("note", e)

    def test_sorted_by_date(self):
        m = market.build_market(FakeFM, polymarket=[])
        dates = [e["date"] for e in m["ipo"]]
        self.assertEqual(dates, sorted(dates))

    def test_ballot_today_excluded_from_calendar(self):
        # ballot_today 是"中签公布",不是"申购日历",不该混进来
        m = market.build_market(FakeFM, polymarket=[])
        names = [e["name"] for e in m["ipo"]]
        self.assertNotIn("某中签公司", names)

    def test_hk_listing_date_converted_to_iso(self):
        m = market.build_market(FakeFM, polymarket=[])
        hk = [e for e in m["ipo"] if e["name"] == "某港股"]
        self.assertEqual(len(hk), 1)
        self.assertEqual(hk[0]["date"], "2026-09-16")

    def test_hk_note_is_listing_not_deadline(self):
        # 数据源没有招股截止日字段,note 不能声称"截止"
        m = market.build_market(FakeFM, polymarket=[])
        hk = [e for e in m["ipo"] if e["name"] == "某港股"][0]
        self.assertNotIn("截止", hk["note"])

    def test_polymarket_passthrough(self):
        m = market.build_market(FakeFM, polymarket=[{"q": "x"}])
        self.assertEqual(m["polymarket"], [{"q": "x"}])

    def test_polymarket_none_delegates_to_fetch_polymarket(self):
        # polymarket=None(即省略该参数)时才去真拉 —— 不打真实网络,靠 mock
        # 验证 build_market 确实委托给了 fetch_polymarket。
        with mock.patch.object(market, "fetch_polymarket", return_value=[{"q": "x"}]) as m_fetch:
            m = market.build_market(FakeFM)
        m_fetch.assert_called_once_with()
        self.assertEqual(m["polymarket"], [{"q": "x"}])

    def test_polymarket_explicit_empty_list_skips_fetch(self):
        # 显式传 [] 时不该去真拉(与 None 的语义区分开)
        with mock.patch.object(market, "fetch_polymarket") as m_fetch:
            m = market.build_market(FakeFM, polymarket=[])
        m_fetch.assert_not_called()
        self.assertEqual(m["polymarket"], [])

    def test_polymarket_total_failure_degrades_to_empty(self):
        with mock.patch.object(market, "fetch_polymarket", side_effect=RuntimeError("gamma 全挂")):
            m = market.build_market(FakeFM)
        self.assertEqual(m["polymarket"], [])
        self.assertTrue(m["ipo"])  # 帖子/日历流不受影响

    def test_missing_upcoming_key_does_not_crash(self):
        class NoUpcomingFM(FakeFM):
            @staticmethod
            def fetch_a_ipo(today):
                return {}

            @staticmethod
            def fetch_hk_ipo():
                return []

            @staticmethod
            def fetch_us_calendar(day, watch):
                return []

        m = market.build_market(NoUpcomingFM, polymarket=[])
        self.assertEqual(m["ipo"], [])

    def test_us_earnings_entry_has_name_date_note(self):
        m = market.build_market(FakeFM, polymarket=[])
        us = [e for e in m["ipo"] if e["name"] == "苹果"]
        self.assertEqual(len(us), 1)
        self.assertEqual(us[0]["date"], "2026-09-14")
        self.assertEqual(us[0]["note"], "美股财报")

    def test_us_earnings_does_not_leak_eps_fields(self):
        # 只取日历,不取解读——eps_forecast/last_year_eps 等不该出现在条目里
        m = market.build_market(FakeFM, polymarket=[])
        us = [e for e in m["ipo"] if e["name"] == "苹果"][0]
        self.assertEqual(set(us.keys()), {"name", "date", "note"})

    def test_three_sources_merged_and_sorted(self):
        # A股(09-15) / 港股(09-16) / 美股(09-14) 三源混排,仍按 date 升序
        m = market.build_market(FakeFM, polymarket=[])
        dates = [e["date"] for e in m["ipo"]]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(
            {e["name"] for e in m["ipo"]},
            {"某公司", "某港股", "苹果"},
        )

    def test_us_calendar_failure_does_not_crash_other_two_calendars(self):
        class UsDownFM(FakeFM):
            @staticmethod
            def fetch_us_calendar(day, watch):
                raise RuntimeError("nasdaq api 超时")

        m = market.build_market(UsDownFM, polymarket=[])
        names = [e["name"] for e in m["ipo"]]
        self.assertIn("某公司", names)
        self.assertIn("某港股", names)
        self.assertNotIn("苹果", names)

    def test_hk_fetch_failure_does_not_crash_a_share_calendar(self):
        # 实测 etnet.com.hk 会连接超时;单源失败不该拖垮 A 股那一半日历
        class HkDownFM(FakeFM):
            @staticmethod
            def fetch_hk_ipo():
                raise TimeoutError("etnet 连接超时")

        m = market.build_market(HkDownFM, polymarket=[])
        names = [e["name"] for e in m["ipo"]]
        self.assertIn("某公司", names)
        self.assertNotIn("某港股", names)

    def test_us_calendar_day_failure_is_isolated_per_day(self):
        # 某一天的 fetch_us_calendar 抖动,不该让循环提前停下——
        # 后面几天要照样试,而不是"抓到的保住,剩下的全丢"
        calls = []

        class MidDayFailFM(FakeFM):
            @staticmethod
            def fetch_us_calendar(day, watch):
                calls.append(day)
                today = FakeFM.beijing_today()
                if day == today + datetime.timedelta(days=3):
                    raise RuntimeError("nasdaq 抖动")
                if day == today:
                    return [{"symbol": "AAA", "name": "第0天公司", "time": "盘后"}]
                if day == today + datetime.timedelta(days=market.US_LOOKAHEAD_DAYS):
                    return [{"symbol": "ZZZ", "name": "第7天公司", "time": "盘后"}]
                return []

        m = market.build_market(MidDayFailFM, polymarket=[])
        # 完整天数都该被调用过,不是在第 3 天(第 4 次调用)就停下
        self.assertEqual(len(calls), market.US_LOOKAHEAD_DAYS + 1)
        names = {e["name"] for e in m["ipo"] if e["note"] == "美股财报"}
        self.assertIn("第0天公司", names)
        self.assertIn("第7天公司", names)

    def test_a_share_fetch_failure_does_not_crash_hk_calendar(self):
        class AShareDownFM(FakeFM):
            @staticmethod
            def fetch_a_ipo(today):
                raise RuntimeError("eastmoney 空结果")

        m = market.build_market(AShareDownFM, polymarket=[])
        names = [e["name"] for e in m["ipo"]]
        self.assertIn("某港股", names)
        self.assertNotIn("某公司", names)


import json
import urllib.parse


def _pm_market(volumeNum=50000, active=True, closed=False,
               outcomePrices='["0.5","0.5"]', oneDayPriceChange=0.0,
               volume24hr=1000, question="Q?"):
    return {"active": active, "closed": closed, "volumeNum": volumeNum,
            "outcomePrices": outcomePrices, "oneDayPriceChange": oneDayPriceChange,
            "volume24hr": volume24hr, "question": question}


def _pm_event(slug="ev1", title="Event", closed=False, markets=None):
    return {"slug": slug, "title": title, "closed": closed, "markets": markets or [_pm_market()]}


def _pm_mode_and_key(url):
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "q" in qs:
        return "keyword", qs["q"][0]
    if "tag_slug" in qs:
        return "tag", qs["tag_slug"][0]
    raise AssertionError(f"unexpected polymarket url: {url}")


def _make_pm_get(keyword_events=None, tag_events=None, keyword_errors=None, tag_errors=None):
    """假 get(url):按 url 里的 q=/tag_slug= 分派到预置的响应,不打真实网络。
    没配置的关键词/tag 一律回空结果,只让测试关心的那一两个产出数据。"""
    keyword_events = keyword_events or {}
    tag_events = tag_events or {}
    keyword_errors = keyword_errors or set()
    tag_errors = tag_errors or set()

    def get(url):
        mode, key = _pm_mode_and_key(url)
        if mode == "keyword":
            if key in keyword_errors:
                raise RuntimeError(f"gamma 请求失败: {key}")
            return json.dumps({"events": keyword_events.get(key, [])}).encode()
        if key in tag_errors:
            raise RuntimeError(f"gamma 请求失败: {key}")
        return json.dumps(tag_events.get(key, [])).encode()

    return get


class TestFetchPolymarket(unittest.TestCase):
    def test_picks_highest_volume_submarket_per_event(self):
        kw = market.PM_KEYWORDS[0]
        markets = [
            _pm_market(volumeNum=5000, question="low"),
            _pm_market(volumeNum=90000, question="high"),
            _pm_market(volumeNum=20000, question="mid"),
        ]
        get = _make_pm_get(keyword_events={kw: [_pm_event(markets=markets)]})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["q"], "high")

    def test_outcome_prices_json_string_parsed(self):
        kw = market.PM_KEYWORDS[0]
        get = _make_pm_get(keyword_events={
            kw: [_pm_event(markets=[_pm_market(outcomePrices='["0.85","0.15"]')])]})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(entries[0]["yes"], 85)

    def test_chg24_and_vol24_coerced(self):
        kw = market.PM_KEYWORDS[0]
        get = _make_pm_get(keyword_events={
            kw: [_pm_event(markets=[_pm_market(oneDayPriceChange=0.065, volume24hr=None)])]})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(entries[0]["chg24"], 6.5)
        self.assertEqual(entries[0]["vol24"], 0)

    def test_below_min_volume_not_produced(self):
        kw = market.PM_KEYWORDS[0]
        get = _make_pm_get(keyword_events={
            kw: [_pm_event(markets=[_pm_market(volumeNum=market.PM_MIN_VOLUME - 1)])]})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(entries, [])

    def test_one_keyword_failure_does_not_block_others(self):
        kw_bad, kw_good = market.PM_KEYWORDS[0], market.PM_KEYWORDS[1]
        get = _make_pm_get(
            keyword_events={kw_good: [_pm_event(slug="good-event")]},
            keyword_errors={kw_bad})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(len(entries), 1)

    def test_same_event_dedup_across_keywords(self):
        kw1, kw2 = market.PM_KEYWORDS[0], market.PM_KEYWORDS[1]
        same_event = [_pm_event(slug="same-event")]
        get = _make_pm_get(keyword_events={kw1: same_event, kw2: same_event})
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(len(entries), 1)

    def test_all_requests_failing_returns_empty_list(self):
        get = _make_pm_get(keyword_errors=set(market.PM_KEYWORDS),
                            tag_errors=set(market.PM_TRENDING_TAGS))
        entries = market.fetch_polymarket(get=get)
        self.assertEqual(entries, [])


class TestPolymarketDefaultGet(unittest.TestCase):
    """默认取数器必须带 UA:Gamma 的 WAF 对空 UA 回 403,而注入 get 的单测
    永远碰不到这一层,线上只会静默变成每天空的 polymarket 段。"""

    def test_default_get_sends_nonempty_user_agent(self):
        captured = []

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _Resp()

        with mock.patch.object(market.urllib.request, "urlopen", fake_urlopen):
            market._pm_default_get("https://example.invalid/x")

        self.assertTrue(captured[0].get_header("User-agent"))


if __name__ == "__main__":
    unittest.main()
