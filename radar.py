"""榜单块。**不进时间线** —— 时间线只放我关注的人,这里是算法排出来的东西,
两种性质混在一起会让页面从「我的关注流」退化成「什么都有的简报」。
页面上它和市场块并列,顶部可折叠。

失败策略与 market.py 一致、与 publish.upload_r2 刻意相反:榜单是补充品,
任一源挂掉只跳过那一源,绝不拖垮已经抓到的帖子。R2 上传才是唯一交付出口,
那个必须抛错。

各源的可达性(2026-09-14 实测):
- HN Algolia  免费无 key,直连可用
- GitHub 搜索 免费,未认证限流 10 次/分,本模块每次运行只打 3 次
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

from datetime import date, timedelta

import blogs

USER_AGENT = "daily-discover/1.0"
TIMEOUT = 20

HN_API = "https://hn.algolia.com/api/v1/search_by_date"
HN_MIN_POINTS = 150          # 降噪:24h 内不到这个分数的不看
HN_TOP_N = 10
HN_HOURS = 24

GH_SEARCH = "https://api.github.com/search/repositories"
GH_RELEASES = "https://api.github.com/repos/{}/releases/latest"
GH_MIN_STARS = 200
GH_LANGUAGES = ("python", "typescript")
GH_TRENDING_DAYS = 7         # 「近期新建且已达 min_stars」,不是 GitHub 官方的 trending
GH_PER_LANG = 5
GH_WATCH_REPOS = ("anthropics/claude-code", "openai/openai-python")


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    """带 UA。GitHub 对空 UA 直接 403,HN 不挑但统一带着。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_hn(get=None, min_points=HN_MIN_POINTS, top_n=HN_TOP_N, hours=HN_HOURS,
             now=None):
    doer = get or _default_get
    since = int((now or time.time()) - hours * 3600)
    q = urllib.parse.urlencode({
        "tags": "story",
        "numericFilters": f"points>{int(min_points)},created_at_i>{since}",
        "hitsPerPage": int(top_n),
    })
    hits = (doer(f"{HN_API}?{q}") or {}).get("hits") or []
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


def fetch_github_trending(get=None, languages=GH_LANGUAGES, min_stars=GH_MIN_STARS,
                          days=GH_TRENDING_DAYS, per_lang=GH_PER_LANG, today=None):
    """按语言各查一次。容错粒度在循环体内 —— 一种语言限流不该连累另一种
    (与 market.py 按天隔离、run.collect 按人隔离是同一个判例)。"""
    doer = get or _default_get
    since = ((today or date.today()) - timedelta(days=days)).isoformat()
    out = []
    for lang in languages:
        q = urllib.parse.urlencode({
            "q": f"created:>{since} stars:>{int(min_stars)} language:{lang}",
            "sort": "stars", "order": "desc", "per_page": int(per_lang),
        })
        try:
            data = doer(f"{GH_SEARCH}?{q}")
        except Exception as exc:  # noqa: BLE001
            _warn(f"github trending {lang} 失败,跳过: {exc}")
            continue
        for it in (data or {}).get("items") or []:
            out.append({
                "title": it.get("full_name") or "",
                "url": it.get("html_url") or "",
                "score": int(it.get("stargazers_count") or 0),
                "note": (it.get("description") or "")[:120],
                "lang": lang,
            })
    out.sort(key=lambda e: e["score"], reverse=True)
    return out


def fetch_github_releases(get=None, repos=GH_WATCH_REPOS):
    doer = get or _default_get
    out = []
    for repo in repos:
        try:
            r = doer(GH_RELEASES.format(repo))
        except Exception as exc:  # noqa: BLE001
            _warn(f"github release {repo} 失败,跳过: {exc}")
            continue
        if not r or not r.get("tag_name"):
            continue
        out.append({
            "title": f"{repo} {r.get('tag_name')}",
            "url": r.get("html_url") or f"https://github.com/{repo}/releases",
            "published": (r.get("published_at") or "")[:10],
            "note": (r.get("name") or "")[:120],
        })
    return out


EMPTY_RADAR = {"hn": [], "github_trending": [], "github_releases": [], "paulgraham": []}


def build_radar(get=None, pg_get=None):
    """各源独立容错。全挂就返回空壳 —— 页面上那一块显示「今天没有」,
    帖子流完全不受影响。

    pg_get 单独一个注入点,不能和 get 合并:本模块的 get 返回**已解析的 JSON**,
    而 Paul Graham 那条抓的是 HTML 文本,同一个 get 喂不了两种消费者。

    **注入了 get 就意味着「不许碰网络」**:这时没给 pg_get 就整条跳过 PG,
    而不是让它落回默认取数器。否则测试里注入了 get 仍会静默打真实 HTTP ——
    实测就是这么让整套测试从 0.1s 变成 22s 的。
    """
    sources = [("hn", fetch_hn, get),
               ("github_trending", fetch_github_trending, get),
               ("github_releases", fetch_github_releases, get)]
    # PG 没有任何可用时间戳,进不了时间线 —— 榜单块本来就是「此刻的状态」
    # 而不是「最近 36 小时发生了什么」,正好放他
    if pg_get is not None or get is None:
        sources.append(("paulgraham", blogs.fetch_pg_latest, pg_get))

    radar = dict(EMPTY_RADAR)
    for key, fn, doer in sources:
        try:
            radar[key] = fn(get=doer)
        except Exception as exc:  # noqa: BLE001
            _warn(f"榜单 {key} 不可用,跳过(帖子流不受影响): {exc}")
    return radar
