"""主入口。串起抓取 → 去重 → 分片 → 上传,并执行成本闸与余额告警。"""
import concurrent.futures
import os
import sys
from datetime import datetime, timedelta, timezone

import dedup
import models
import publish
import rss
import tikhub
import xiaoyuzhou
import xueqiu

CUTOFF_HOURS = 36
CONCURRENCY = 4

DEFAULT_FETCHERS = {
    "twitter": tikhub.fetch_twitter,
    "xueqiu": xueqiu.fetch_xueqiu,   # 不走 TikHub,它没有雪球
    "xhs": tikhub.fetch_xhs,
    "wechat": tikhub.fetch_wechat,
    "rss": rss.fetch_rss,                # Substack / 个人博客,任何自带 feed 的人
    "podcast": xiaoyuzhou.fetch_xiaoyuzhou,
}


def warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def load_market(here):
    """打新日历是补充品,帖子流才是主交付 —— 导入/构建失败要降级,不能拖垮已抓到的帖子。"""
    try:
        sys.path.insert(0, os.path.join(here, "..", "ipo-earnings"))
        import fetch_market as fm
        import market as market_mod
        return market_mod.build_market(fm)
    except Exception as e:
        warn(f"市场数据不可用,跳过(帖子流不受影响): {e}")
        return {"polymarket": [], "ipo": []}


def collect(client, sources, cutoff_iso, fetchers=None):
    fetchers = fetchers or DEFAULT_FETCHERS
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
        fresh = [p for p in got if p.ts >= cutoff_iso]
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
    sources = models.load_sources(os.path.join(here, "sources.yaml"))

    client = tikhub.TikHub(os.environ.get("TIKHUB_API_KEY"))
    try:
        bal = client.balance()
    except Exception as e:
        warn(f"余额查询失败,跳过告警(不影响抓取): {e}")
        bal = None
    if bal is not None and bal < 1.0:
        warn(f"TikHub 余额仅剩 ${bal:.2f},按 $0.1/天约够 {int(bal / 0.1)} 天")

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=CUTOFF_HOURS)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    posts, stats = collect(client, sources, cutoff)
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
    market = load_market(here)
    shard = publish.build_shard(beijing, posts, stats, market)

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
    publish.upload_r2("discover/index.json", publish.merge_index(existing, day_entry))

    print(f"共 {len(posts)} 条,TikHub 调用 {client.calls} 次")
    print(f"已上传 {shard_key} 与 discover/index.json")
    print(f"stats: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
