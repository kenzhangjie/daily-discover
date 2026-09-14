"""榜单块。**不进时间线** —— 时间线只放我关注的人,这里是算法排出来的东西,
两种性质混在一起会让页面从「我的关注流」退化成「什么都有的简报」。
页面上它和市场块并列,顶部可折叠。

失败策略与 market.py 一致、与 publish.upload_r2 刻意相反:榜单是补充品,
任一源挂掉只跳过那一源,绝不拖垮已经抓到的帖子。R2 上传才是唯一交付出口,
那个必须抛错。

各源的可达性(2026-09-14 实测):
- HN Algolia  免费无 key,直连可用
- GitHub      **沙盒里够不着**。代理把 api.github.com 和 github.com 全拦了
              (2026-09-14 连续两次线上实测,403;换 releases.atom 和 /trending
              的 HTML 也一样),只放行 raw.githubusercontent.com。
              所以数据由 **GitHub Actions 预先算好摆到 R2**(sync_to_r2.py 的
              build_github_radar,Actions runner 在 GitHub 自己机器上没有限制),
              这里只负责读 R2 那个 JSON。跟代码同步走同一条通路。
- Reddit      **未接**。www/old.reddit.com 的 .json 现在一律 403(换浏览器 UA
              也一样),要走 TikHub 的 /api/v1/reddit/app/fetch_subreddit_feed。
              没接是因为拿不到返回结构:key 只在云 routine 的环境变量里。
              接的时候照 tikhub.py 里其他适配器的结构写,别猜字段名。
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import blogs

USER_AGENT = "daily-discover/1.0"
TIMEOUT = 20

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_MIN_POINTS = 150          # 降噪:24h 内不到这个分数的不看
HN_TOP_N = 10
HN_HOURS = 24

# 由 GitHub Actions(sync_to_r2.py)预先算好写在这里 —— 沙盒够不着 github.com
GH_RADAR_JSON = ("https://pub-42a2b5c1ec984024833f48ca358f4571.r2.dev/"
                 "radar/github.json")
GH_TRENDING_TOP_N = 8


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    """**返回文本,不是解析好的 JSON。**

    本模块有三种载荷:HN 是 JSON、releases 是 Atom、trending 是 HTML。
    让取数器统一返回文本、由各自的解析函数决定怎么读,就只需要一个注入点 ——
    否则注入一个 get 就得同时满足两种消费者,必有一边解析失败。

    带 UA:GitHub 对空 UA 直接 403,HN 不挑但统一带着。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def fetch_hn(get=None, min_points=HN_MIN_POINTS, top_n=HN_TOP_N, hours=HN_HOURS,
             now=None):
    doer = get or _default_get
    since = int((now or time.time()) - hours * 3600)
    q = urllib.parse.urlencode({
        "tags": "story",
        "numericFilters": f"points>{int(min_points)},created_at_i>{since}",
        "hitsPerPage": int(top_n),
    })
    hits = (json.loads(doer(f"{HN_API}?{q}")) or {}).get("hits") or []
    out = []
    for h in hits:
        oid = h.get("objectID")
        if not oid:
            continue
        out.append({
            "title": h.get("title") or "",
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
            "score": int(h.get("points") or 0),
            "comments": int(h.get("num_comments") or 0),
            "discuss": f"https://news.ycombinator.com/item?id={oid}",
        })
    out.sort(key=lambda e: e["score"], reverse=True)
    return out


def _github_radar(get=None):
    """读 Actions 摆渡到 R2 的那份 JSON。整个函数只打一次 R2,两组共用。"""
    doer = get or _default_get
    return json.loads(doer(GH_RADAR_JSON)) or {}


def fetch_github_trending(get=None, top_n=GH_TRENDING_TOP_N):
    rows = list(_github_radar(get).get("trending") or [])
    rows.sort(key=lambda e: e.get("score") or 0, reverse=True)
    return rows[:top_n]


def fetch_github_releases(get=None):
    return list(_github_radar(get).get("releases") or [])


EMPTY_RADAR = {"hn": [], "github_trending": [], "github_releases": [], "paulgraham": []}


def build_radar(get=None):
    """各源独立容错。全挂就返回空壳 —— 页面上那一块显示「今天没有」,
    帖子流完全不受影响。

    取数器统一返回文本(见 _default_get),所以一个注入的 get 能服务全部四个源,
    包括 Paul Graham 那条抓 HTML 的。注入了 get 就等于「不许碰网络」,不会有
    哪一条偷偷落回默认实现。
    """
    radar = dict(EMPTY_RADAR)
    for key, fn in (("hn", fetch_hn),
                    ("github_trending", fetch_github_trending),
                    ("github_releases", fetch_github_releases),
                    # PG 没有任何可用时间戳,进不了时间线 —— 榜单块本来就是
                    # 「此刻的状态」而不是「最近 36 小时发生了什么」,正好放他
                    ("paulgraham", blogs.fetch_pg_latest)):
        try:
            radar[key] = fn(get=get)
        except Exception as exc:  # noqa: BLE001
            _warn(f"榜单 {key} 不可用,跳过(帖子流不受影响): {exc}")
    return radar
