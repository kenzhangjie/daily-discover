#!/usr/bin/env python3
"""按名字查播客,打印一行可以直接贴进 sources.yaml 的 podcast 条目。

    python3 tools/podfind.py "Dwarkesh"                    # 按名字搜
    python3 tools/podfind.py "a16z" --all                  # 列出全部候选
    python3 tools/podfind.py 1668002688                    # 按 Apple id 直查
    python3 tools/podfind.py https://podcasts.apple.com/jp/podcast/x/id958230465

走 Apple 的公开目录接口(免 key、免登录)。Apple 只是**目录**:它不存节目,
只存作者自己那个 feed 的地址 —— 所以返回里的 `feedUrl` 才是真源,
托管商是 libsyn / substack / transistor / anchor / simplecast 里的哪个,
取决于作者当初选了谁,跟我们无关。

id 这个东西**用不上**:search 一次就把 feedUrl 给了,不需要再拿 id 去 lookup。
打印它只是方便你去 podcasts.apple.com/…/id<数字> 核对是不是同一个节目。

顺带拉一次 feed 报最新一集的日期 —— 播客「还活着吗」比什么都重要:
BG2 在 Apple 目录里好端端的,feed 却停在 2026-03。加之前先看这一行。
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

UA = "daily-discover/1.0"
SEARCH = "https://itunes.apple.com/search"
LOOKUP = "https://itunes.apple.com/lookup"
TIMEOUT = 25
# 抓 feed 本身要宽得多:libsyn 从国内出去经常十几二十秒才有第一个字节
# (2026-09-15 实测 20VC 那个 feed 在 25 秒超时下失败,而同一地址在云沙盒里秒回)。
# 这里慢一点无所谓 —— 这是个手动跑的工具,不是每天那条流水线。
FEED_TIMEOUT = 60
FEED_RETRIES = 2


def _get(url, timeout=TIMEOUT, retries=1):
    last = None
    for _ in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last


_ID_RE = re.compile(r"(?:^|/id)(\d{6,})\s*$")


def resolve(term, limit=5):
    """term 可以是名字、Apple id、或者整条 podcasts.apple.com 链接。

    给了 id 就走 lookup(精确,不会搜岔);只有名字才走 search。
    Apple 链接里的国家段(/tw/ /us/ /jp/)无所谓 —— id 是全球唯一的,
    lookup 不带国家也查得到。"""
    m = _ID_RE.search(term.strip())
    if m:
        q = urllib.parse.urlencode({"id": m.group(1)})
        return json.loads(_get(f"{LOOKUP}?{q}")).get("results") or []
    q = urllib.parse.urlencode({"term": term, "entity": "podcast", "limit": limit})
    return json.loads(_get(f"{SEARCH}?{q}")).get("results") or []


def latest(feed_url):
    """返回 (集数, 最新一集日期字符串)。拉不到就说拉不到,不猜。

    **日期必须从第一个 <item> 里面取**,不能对整篇 XML 做全局匹配:很多 feed
    在 channel 级也有一个 <pubDate>,全局匹配的第 0 个是它、第 1 个才是最新一集;
    而另一些 feed(anchor.fm)根本没有 channel 级那个,于是同一份代码在两种 feed
    上分别拿到「最新一集」和「第二新一集」。2026-09-15 就是这么把 BG2 的
    2026-06-11 读成了 2026-03-15 —— 差了一整集,结论差了三个月。"""
    try:
        xml = _get(feed_url, timeout=FEED_TIMEOUT,
                   retries=FEED_RETRIES).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return None, f"拉不到({exc})"
    blocks = re.findall(r"<item[ >](.*?)</item>", xml, re.S) or \
        re.findall(r"<entry[ >](.*?)</entry>", xml, re.S)
    if not blocks:
        return 0, "feed 里一集都没有"
    m = re.search(r"<pubDate>(.*?)</pubDate>", blocks[0]) or \
        re.search(r"<published>(.*?)</published>", blocks[0])
    return len(blocks), (m.group(1).strip() if m else "没有日期字段")


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "", name.lower())
    return s[:12] or "podcast"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("term", help="播客名字 / Apple id / podcasts.apple.com 链接")
    ap.add_argument("--all", action="store_true", help="列出全部候选")
    args = ap.parse_args(argv)

    rows = resolve(args.term)
    if not rows:
        print(f"Apple 目录里找不到「{args.term}」—— 可能它只有 YouTube,"
              f"那走 README 里 YouTube 那条路。", file=sys.stderr)
        return 1

    for it in (rows if args.all else rows[:1]):
        name = it.get("collectionName") or ""
        feed = it.get("feedUrl")
        author = it.get("artistName") or ""
        print(f"\n{name}")
        print(f"  作者      {author}")
        print(f"  Apple id  {it.get('collectionId')}"
              f"   https://podcasts.apple.com/podcast/id{it.get('collectionId')}")
        if not feed:
            print("  feed      **没有公开**(作者在 Apple 后台藏了),这个源加不进来")
            continue
        n, newest = latest(feed)
        print(f"  托管在    {urllib.parse.urlparse(feed).netloc}")
        print(f"  共 {n if n is not None else '?'} 集,最新一集 {newest}")
        print("  贴进 sources.yaml 的 podcast: 下面 ——")
        print(f'  - {{id: "{slug(name)}", name: "{name[:40]}", note: "{author[:40]}",')
        print(f'     feed: "{feed}"}}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
