"""主入口。串起抓取 → 去重 → 分片 → 上传。"""
import concurrent.futures
import os
import sys
from datetime import datetime, timedelta, timezone

import blogs
import dedup
import linkedin
import models
import publish
import radar as radar_mod
import rss
import social
import xiaoyuzhou
import xueqiu

CUTOFF_HOURS = 36
# 播客 / 订阅 / 博客是**周更级**的。36 小时的窗口意味着一周里有六天完全看不到
# 它们:2026-09-15 实测,两个小宇宙播客最新一集分别是 12 天和 17 天前,stats 记
# `0, ok:true` —— 如实,但页面上等于这个渠道不存在,连侧栏都没有它。
# 给低频渠道 7 天窗口:一集播出后在页面上留一周,哪天打开都看得到本周更新了什么。
# 帖子仍按自己的时间戳排序和分档,不会假装是今天发的。
SLOW_CUTOFF_HOURS = 24 * 7
# linkedin 在这里不是因为它更得慢(虽然也慢:chamath 四个月 50 条,约一周三条),
# 是因为它**四天才轮到一次**(见 rotate_slots)。36 小时窗口配四天轮询,等于每
# 4 天里有 2.5 天发的帖永远进不来 —— 抓回来了,被截断悄悄丢掉,stats 还显示
# ok:true count:0,看着就像「他这几天没发」。
# 规矩:**窗口必须 ≥ 轮询周期**。四天轮一圈,七天窗口留了三天余量。
SLOW_CHANNELS = ("podcast", "rss", "blog", "linkedin")
CONCURRENCY = 4


def fetch_podcast(client, person, get=None):
    """播客有两种源,名单上是同一个渠道:
    - 小宇宙**没有公开 RSS**,只能抓页面里的 __NEXT_DATA__(xiaoyuzhou.py)
    - 英文播客一律有 RSS(20VC / Lenny's / Acquired ...),走通用的 rss.py

    按 person 里有没有 `feed` 分流。这样 sources.yaml 里中英文播客混在一张
    名单上,页面上也是同一个「播客」渠道 —— 读者不关心它后面是哪种协议。
    """
    if person.get("feed"):
        return rss.fetch_rss(client, person, get=get, channel="podcast")
    return xiaoyuzhou.fetch_xiaoyuzhou(client, person, get=get)


DEFAULT_FETCHERS = {
    # twitter / xhs / wechat 2026-09-26 从 TikHub 切到 Asklear(TikHub 欠费 402)
    "twitter": social.fetch_twitter,
    "xueqiu": xueqiu.fetch_xueqiu,   # 直连雪球,不走任何数据商
    "xhs": social.fetch_xhs,
    "wechat": social.fetch_wechat,
    "rss": rss.fetch_rss,                # Substack / 个人博客,任何自带 feed 的人
    "podcast": fetch_podcast,   # 小宇宙 + 英文 RSS 播客,见上面的分流
    "blog": blogs.fetch_blog,      # Anthropic / OpenAI,没有 RSS 只能直接抓
    "linkedin": linkedin.fetch_linkedin,  # 走 Asklear,TikHub 的 LinkedIn 全线 400
}


def rotate_slots(sources, day_index=None):
    """按 slot 轮询,返回**只用于抓取**的名单副本。

    为什么要轮:LinkedIn 是 84 积分/次,全员每天拉 = 12 × 84 = 1008 积分/天,
    别的渠道加起来才 $0.1/天。分四组每天只拉一组 → 252 积分/天。
    LinkedIn 的 KOL 一周发 2-3 条,四天延迟够用;而且一次就返回 50 条、
    跨度近四个月(2026-09-21 实测 chamath),拉勤了纯属浪费。

    只影响抓谁,**不影响 sources 本身** —— main 里的 authors(侧栏"我的信息源
    清单")仍按全量建。否则侧栏会一天只剩三个人,看着像名单被删了。

    没有 slot 字段的条目每天都抓,所以其他渠道完全不受影响。
    """
    if day_index is None:
        day_index = datetime.now(timezone.utc).timetuple().tm_yday
    out = {}
    for channel, people in sources.items():
        slotted = [p for p in people if p.get("slot") is not None]
        if not slotted:
            out[channel] = people
            continue
        groups = sorted({int(p["slot"]) for p in slotted})
        today = groups[day_index % len(groups)]
        out[channel] = [p for p in people
                        if p.get("slot") is None or int(p["slot"]) == today]
    return out


def warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def load_market(here, pm_config=None):
    """打新日历是补充品,帖子流才是主交付 —— 导入/构建失败要降级,不能拖垮已抓到的帖子。"""
    try:
        sys.path.insert(0, os.path.join(here, "..", "ipo-earnings"))
        import fetch_market as fm
        import market as market_mod
        return market_mod.build_market(fm, pm_config=pm_config)
    except Exception as e:
        warn(f"市场数据不可用,跳过(帖子流不受影响): {e}")
        return {"polymarket": [], "ipo": [], "earnings": []}


def collect(client, sources, cutoff_iso, fetchers=None, slow_cutoff_iso=None):
    """cutoff_iso 是常规窗口;低频渠道(SLOW_CHANNELS)用 slow_cutoff_iso。
    不传 slow 就退回常规值,测试里想只验一个窗口时不必两个都构造。"""
    fetchers = fetchers or DEFAULT_FETCHERS
    slow_cutoff_iso = slow_cutoff_iso or cutoff_iso
    all_posts, stats = [], {}
    for channel, people in sources.items():
        fetch = fetchers.get(channel)
        if fetch is None:
            warn(f"没有 {channel} 的适配器,跳过")
            continue
        got, errors = [], []
        with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as pool:
            futures = {pool.submit(fetch, client, p): p for p in people}
            for fut in concurrent.futures.as_completed(futures):
                person = futures[fut]
                try:
                    got.extend(fut.result())
                except Exception as exc:
                    errors.append(f"{person['id']}: {exc}")
                    warn(f"{channel} {person['id']} 抓取失败: {exc}")
        limit = slow_cutoff_iso if channel in SLOW_CHANNELS else cutoff_iso
        fresh = [p for p in got if p.ts >= limit]
        # 全员失败才算渠道挂了;部分失败仍算 ok,但把错误记下来
        ok = len(errors) < len(people) if people else True
        stats[channel] = {"count": len(fresh), "ok": ok}
        if errors:
            stats[channel]["error"] = "; ".join(errors[:3])
            if len(errors) > 3:
                stats[channel]["error"] += f" (+{len(errors) - 3} more)"
        all_posts.extend(fresh)
    return all_posts, stats


def main(argv=None):
    argv = argv or sys.argv[1:]
    here = os.path.dirname(os.path.abspath(__file__))
    sources_path = os.path.join(here, "sources.yaml")
    sources = models.load_sources(sources_path)
    settings = models.load_settings(sources_path)

    # 没有渠道再走 TikHub 了。fetch(client, person) 的签名保留,client 传 None
    client = None

    now = datetime.now(timezone.utc)
    iso = lambda h: (now - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    # 抓取用轮询后的名单,authors(侧栏)仍用全量 sources
    posts, stats = collect(client, rotate_slots(sources), iso(CUTOFF_HOURS),
                           slow_cutoff_iso=iso(SLOW_CUTOFF_HOURS))
    posts = dedup.dedup(posts)

    # count 要按去重后的口径重新统计,否则跟 index.total(同样是去重后)对不上;
    # ok/error 不变 —— 它们讲的是接口是否挂了,与去重无关,渠道被去重清空
    # 后 count 归 0 但仍要留在 stats 里(这正是"没有"与"没抓到"的区别)。
    post_counts = {}
    for p in posts:
        post_counts[p.channel] = post_counts.get(p.channel, 0) + 1
    for ch in stats:
        stats[ch]["count"] = post_counts.get(ch, 0)

    beijing = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    market = load_market(here, settings.get("polymarket"))
    radar = radar_mod.build_radar()      # 内部逐源容错,全挂返回空壳
    shard = publish.build_shard(beijing, posts, stats, market, radar)

    # 分片
    shard_key = f"discover/{beijing}.json"
    publish.upload_r2(shard_key, shard)

    # 索引:关注名单每次按 sources 重建,日期条目按 date 覆盖
    authors = [{"channel": ch, "handle": p["id"], "name": p.get("name") or p["id"],
                "note": p.get("note", "")}
               for ch, people in sources.items() for p in people]
    existing = publish.fetch_index()
    existing["authors"] = authors
    day_entry = {"date": beijing, "total": len(posts),
                 "channels": {ch: st["count"] for ch, st in stats.items()}}
    merged = publish.merge_index(existing, day_entry)
    # 索引是唯一的目录,它缩过一次水(见 publish.fetch_index 的注释):分片还在桶里,
    # 索引却不再指向它,页面上表现为「加载更早一天」永远不出现 —— 没有任何报错。
    # 把天数打进日志,下一次缩水当场看得见,不用等人发现按钮没了。
    before = len(existing.get("days") or [])
    if len(merged["days"]) < before:
        print(f"WARN 索引天数 {before} → {len(merged['days'])},历史在丢",
              file=sys.stderr)
    publish.upload_r2("discover/index.json", merged)

    print(f"共 {len(posts)} 条")
    print(f"已上传 {shard_key} 与 discover/index.json")
    print(f"索引 {len(merged['days'])} 天: {[d['date'] for d in merged['days']]}")
    print(f"stats: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
