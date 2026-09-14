"""市场数据:只取日历,不取解读。

打新日历是「前瞻」(未来日期),与帖子流的「回顾」方向相反 ——
所以它在页面上必须独立成块,不能倒序混进时间线。

字段名来自 ipo-earnings/fetch_market.py 的真实返回结构(2026-09-14 联网探测 +
读源码确认,不是 brief 里推测的 apply_date/deadline/name 那套):

- fetch_a_ipo(today) 返回 dict {"upcoming": [...], "ballot_today": [...]},
  不是 list。"upcoming" 每项含 name / apply_date,是真正的申购日历;
  "ballot_today" 是"今日中签公布",不属于日历,排除。
- fetch_hk_ipo() 返回 list,每项**没有 deadline 字段**。只有
  listing_date(格式 "YYYY/MM/DD",上市日)和 status(模糊文本,不可靠地解析成日期)。
  故港股一栏用 listing_date 转 ISO,note 写"港股上市"而非"招股截止"。
- fetch_us_calendar(day, watch) 返回 list,每项**没有 date 字段** —— 日期是
  隐含的:这个函数本身只查"day 这一天"watch 里哪些代码要发财报,同一 symbol
  在不同 day 调用会分别命中。所以日历里的 date 由调用方(这里)在循环 day 时
  自己贴上去,不是从返回行里读出来的。每行实际字段是
  symbol/name/time/eps_forecast/last_year_eps/fiscal_quarter —— 只取 name,
  不取 eps_forecast/last_year_eps 等解读字段(那是「打新&财报」群卡片的内容)。
  watchlist 从 ipo-earnings/watchlist.yaml 读,路径由 fm.__file__ 所在目录
  推出,不进 build_market 的签名。

实测中 etnet.com.hk 在本环境直接连接超时(curl 验证过,和网络代码无关)。
A股/港股各自只有一次调用,try/except 包在外面即可——单源失败只跳过那一份
日历,不拖垮其余两个(呼应 fetch_market.py 自己的 run_source 单源容错设计,
以及 run.py 对各社交渠道"部分失败不算挂"的处理)。

美股财报要循环 US_LOOKAHEAD_DAYS+1 天,容错粒度不能包在循环外面——
那样会重复 Task 10 里"一条 created_at 为 null 害死整个人 20 条推文"的
同一个 bug 模式:某一天抖动,循环直接被打断,已经跑到的几天算白抓,
后面几天连试都没试。所以拆成两层:外层 try/except 只包"读取 watchlist"
(没有 watchlist 确实无从查起,这一整源该跳过);循环体内层再单独包每一天,
失败就 _warn 并 continue,不影响其余天数。

Polymarket(fetch_polymarket)同样是补充品,失败策略与打新/财报日历相反于
upload_r2:必须降级,不能抛错。容错粒度同理落在每个关键词、每个 tag 的
循环体内 —— 一个关键词抖动不该连累其余关键词。
"""
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

US_LOOKAHEAD_DAYS = 7  # 跟 A股 upcoming 窗口(今日起 7 天)保持一致

USER_AGENT = "daily-discover/1.0"

PM_GAMMA = "https://gamma-api.polymarket.com"
PM_MIN_VOLUME = 10000          # lifetime 成交额低于此值的僵尸市场丢弃
PM_MAX_PER_KEYWORD = 3
PM_KEYWORDS = ["anthropic", "claude", "openai", "gpt", "grok", "tiktok",
               "us china tariff", "bytedance", "tencent", "nvidia", "magnificent"]
PM_TRENDING_TAGS = ["ai", "stocks", "china", "earnings"]
PM_TRENDING_TOP_N = 3
PM_TOTAL_CAP = 12              # 页面上这一块只有十几行的体量,封顶防刷屏


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _pm_parse_prices(raw):
    """outcomePrices 是 JSON 编码的字符串,如 '["0.21","0.79"]'。畸形值返回 []
    让调用方跳过该 market,而不是让 float() 抛错炸穿整个 polymarket 段。"""
    if not raw:
        return []
    try:
        arr = json.loads(raw) if isinstance(raw, str) else raw
        return [float(x) for x in arr]
    except (TypeError, ValueError):
        return []


def _pm_event_entry(event):
    """一个 event -> (lifetime_volume, entry) 或 None(没有合法的代表 market)。

    每个 event 只取一个代表 market:开放且 active、volumeNum 最高、且
    ≥ PM_MIN_VOLUME、且有合法 Yes 价的那一个 —— 不这么做,bracket 事件会
    用 7-10 个子市场刷屏。lifetime_volume 一并返回,给关键词模式按它排序。
    """
    if event.get("closed"):
        return None
    best = None
    for m in event.get("markets") or []:
        if not isinstance(m, dict):
            continue
        if m.get("closed") or not m.get("active", True):
            continue
        prices = _pm_parse_prices(m.get("outcomePrices"))
        if not prices:
            continue
        vol_total = float(m.get("volumeNum") or 0)
        if vol_total < PM_MIN_VOLUME:
            continue
        if best is None or vol_total > best[0]:
            best = (vol_total, m, prices)
    if best is None:
        return None
    vol_total, m, prices = best
    slug = event.get("slug") or ""
    url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"
    entry = {
        "q": m.get("question") or event.get("title") or "",
        "yes": round(prices[0] * 100),
        # Gamma 给的是 [-1,1] 的小数,-0.015 表示 -1.5 个百分点
        "chg24": round(float(m.get("oneDayPriceChange") or 0) * 100, 1),
        "vol24": int(float(m.get("volume24hr") or 0)),
        "url": url,
    }
    return vol_total, entry


def _pm_default_get(url):
    """必须带 User-Agent —— Gamma 的 WAF 对默认的 Python-urllib/3.x 直接回 403
    (2026-09-14 实测:无 UA 403,任意非空 UA 200)。和 TikHub 那次是同一种坑,
    且单测注入 get 永远发现不了,只会表现为线上 polymarket 天天是空的。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def fetch_polymarket(get=None):
    """拉 Polymarket 两种视图:关键词监控(Mode A)+ 趋势榜(Mode B)。

    容错粒度在每个关键词 / 每个 tag 的循环体内 —— 一个失败 _warn 后
    continue,不影响其余的(呼应美股财报日历按天隔离容错的同一设计)。
    """
    doer = get or _pm_default_get
    entries, seen_urls = [], set()

    for kw in PM_KEYWORDS:
        try:
            url = f"{PM_GAMMA}/public-search?q={urllib.parse.quote(kw)}&limit_per_type=20"
            events = json.loads(doer(url)).get("events") or []
        except Exception as e:  # noqa: BLE001 —— 单个关键词失败不拖垮其余关键词
            _warn(f"polymarket 关键词 {kw!r} 请求失败,跳过: {e}")
            continue
        ranked = [r for r in (_pm_event_entry(ev) for ev in events) if r]
        ranked.sort(key=lambda r: r[0], reverse=True)
        for _, entry in ranked[:PM_MAX_PER_KEYWORD]:
            if entry["url"] in seen_urls:
                continue
            seen_urls.add(entry["url"])
            entries.append(entry)

    for tag in PM_TRENDING_TAGS:
        try:
            url = (f"{PM_GAMMA}/events?closed=false&active=true&tag_slug={tag}"
                   "&order=volume24hr&ascending=false&limit=30")
            events = json.loads(doer(url))
            if not isinstance(events, list):
                events = []
        except Exception as e:  # noqa: BLE001 —— 单个 tag 失败不拖垮其余 tag
            _warn(f"polymarket 趋势标签 {tag!r} 请求失败,跳过: {e}")
            continue
        taken = 0
        for ev in events:
            if taken >= PM_TRENDING_TOP_N:
                break
            r = _pm_event_entry(ev)
            if r is None:
                continue
            taken += 1
            _, entry = r
            if entry["url"] in seen_urls:
                continue
            seen_urls.add(entry["url"])
            entries.append(entry)

    entries.sort(key=lambda e: e["vol24"], reverse=True)
    return entries[:PM_TOTAL_CAP]


def build_market(fm, polymarket=None):
    ipo = []
    try:
        a_result = fm.fetch_a_ipo(fm.beijing_today()) or {}
    except Exception as e:  # noqa: BLE001 —— 单源失败不拖垮整批
        _warn(f"fetch_a_ipo 失败,跳过 A股日历: {e}")
        a_result = {}
    for row in a_result.get("upcoming") or []:
        if row.get("apply_date") and row.get("name"):
            ipo.append({"name": row["name"], "date": row["apply_date"], "note": "A股申购"})

    try:
        hk_rows = fm.fetch_hk_ipo() or []
    except Exception as e:  # noqa: BLE001
        _warn(f"fetch_hk_ipo 失败,跳过港股日历: {e}")
        hk_rows = []
    for row in hk_rows:
        listing = row.get("listing_date")
        if listing and row.get("name"):
            ipo.append({"name": row["name"], "date": listing.replace("/", "-"),
                        "note": "港股上市"})

    try:
        wl_path = os.path.join(os.path.dirname(fm.__file__), "watchlist.yaml")
        with open(wl_path, encoding="utf-8") as f:
            watch = fm.parse_watchlist(f.read())
        today = fm.beijing_today()
    except Exception as e:  # noqa: BLE001 —— 没有 watchlist 就无从查起,整源跳过
        _warn(f"读取 watchlist 失败,跳过美股财报日历: {e}")
        watch = None

    if watch is not None:
        for i in range(US_LOOKAHEAD_DAYS + 1):
            day = today + datetime.timedelta(days=i)
            try:
                rows = fm.fetch_us_calendar(day, watch) or []
            except Exception as e:  # noqa: BLE001 —— 单天失败不该拖累其余天数
                _warn(f"fetch_us_calendar({day}) 失败,跳过这一天: {e}")
                continue
            for row in rows:
                if row.get("name"):
                    ipo.append({"name": row["name"], "date": day.isoformat(),
                                "note": "美股财报"})

    ipo.sort(key=lambda e: e["date"])

    if polymarket is None:
        try:
            polymarket = fetch_polymarket()
        except Exception as e:  # noqa: BLE001 —— 补充品,全挂了就降级为空
            _warn(f"polymarket 整体失败,降级为空: {e}")
            polymarket = []
    else:
        polymarket = list(polymarket)

    return {"polymarket": polymarket, "ipo": ipo}
