"""榜单块。**不进时间线** —— 时间线只放我关注的人,这里是算法排出来的东西,
两种性质混在一起会让页面从「我的关注流」退化成「什么都有的简报」。
页面上它和市场块并列,顶部可折叠。

失败策略与 market.py 一致、与 publish.upload_r2 刻意相反:榜单是补充品,
任一源挂掉只跳过那一源,绝不拖垮已经抓到的帖子。R2 上传才是唯一交付出口,
那个必须抛错。

各源的可达性(2026-09-14 实测):
- HN Algolia  免费无 key,直连可用
- GitHub      **不能走 api.github.com**。云沙盒的代理把它整个拦了,带不带 GH_TOKEN
              都返回代理自己的提示(2026-09-14 线上实测:trending 与 release 四次
              全部 403)。改走 github.com 上不经过 API 的两条路:
              releases.atom(标准 Atom,稳)和 /trending(HTML,GitHub 会改版,
              解析不到要出声而不是静默返回空)
- Reddit      **未接**。www/old.reddit.com 的 .json 现在一律 403(换浏览器 UA
              也一样),要走 TikHub 的 /api/v1/reddit/app/fetch_subreddit_feed。
              没接是因为拿不到返回结构:key 只在云 routine 的环境变量里。
              接的时候照 tikhub.py 里其他适配器的结构写,别猜字段名。
"""
import html
import json
import re
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

GH_TRENDING = "https://github.com/trending?since=daily&spoken_language_code="
GH_RELEASES = "https://github.com/{}/releases.atom"
GH_TRENDING_TOP_N = 8
GH_WATCH_REPOS = ("anthropics/claude-code", "openai/openai-python")
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    """**返回文本,不是解析好的 JSON。**

    本模块有三种载荷:HN 是 JSON、releases 是 Atom、trending 是 HTML。
    让取数器统一返回文本、由各自的解析函数决定怎么读,就只需要一个注入点 ——
    否则注入一个 get 就得同时满足两种消费者,必有一边解析失败。

    带 UA:GitHub 对空 UA 直接 403,HN 不挑但统一带着。"""
    ua = BROWSER_UA if "github.com/trending" in url else USER_AGENT
    req = urllib.request.Request(url, headers={"User-Agent": ua})
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


def parse_trending(page):
    """/trending 是 HTML,每个仓库一个 <article class="Box-row">。

    GitHub 会改版这个页面。一行都解析不到时**抛错**而不是返回空列表 ——
    静默返回空会被读成「今天没有 trending」,一个坏掉的源就这么消失几个月。
    build_radar 在外层接住并 WARN,榜单块只少这一组,帖子流不受影响。
    """
    rows = re.split(r'<article class="Box-row">', page or "")[1:]
    out = []
    for block in rows:
        # <a> 上 data-hydro-click 排在 href 前面,所以不能直接接 <a href=;
        # 先圈出 <h2> 块,再在块里找第一个仓库链接
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if not h2:
            continue
        m = re.search(r'href="/([^"/]+/[^"?#]+)"', h2.group(1))
        if not m:
            continue
        full = m.group(1).strip()
        desc = re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', block, re.S)
        # star 数在 <a href=".../stargazers" ...> 之后,中间隔着一整个 <svg>
        stars = re.search(r'href="/[^"]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)',
                          block, re.S)
        today = re.search(r'([\d,]+)\s*stars? today', block)
        out.append({
            "title": full,
            "url": f"https://github.com/{full}",
            "score": int((stars.group(1) if stars else "0").replace(",", "")),
            "note": (html.unescape(
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", desc.group(1)))).strip()[:120]
                if desc else ""),
            "today": int((today.group(1) if today else "0").replace(",", "")),
        })
    if not out:
        raise ValueError("/trending 一行都没解析出来,GitHub 大概改版了")
    return out


def fetch_github_trending(get=None, top_n=GH_TRENDING_TOP_N):
    """走 github.com/trending 而不是 api.github.com —— 后者在云沙盒里被代理整个拦掉,
    带不带 token 都 403(2026-09-14 线上实测,四次全 403)。"""
    doer = get or _default_get
    rows = parse_trending(doer(GH_TRENDING))
    rows.sort(key=lambda e: (e.get("today") or 0, e["score"]), reverse=True)
    return rows[:top_n]


def parse_releases_atom(xml, repo):
    """releases.atom 是标准 Atom:<entry> 里有 <title>(tag)、<updated>、<link href>。
    走它而不是 api.github.com/repos/.../releases/latest —— 同样被代理拦。
    只取最新一个 release:榜单块是「此刻的状态」,历史版本不是。"""
    entries = re.findall(r"<entry>(.*?)</entry>", xml or "", re.S)
    out = []
    for block in entries[:1]:
        tag = re.search(r"<title>(.*?)</title>", block, re.S)
        if not tag:
            continue
        when = re.search(r"<updated>(.*?)</updated>", block, re.S)
        link = re.search(r'<link[^>]*href="([^"]+)"', block)
        out.append({
            "title": f"{repo} {tag.group(1).strip()}",
            "url": link.group(1) if link else f"https://github.com/{repo}/releases",
            "published": when.group(1)[:10] if when else "",
            "note": "",
        })
    return out


def fetch_github_releases(get=None, repos=GH_WATCH_REPOS):
    doer = get or _default_get
    out = []
    for repo in repos:
        try:
            out.extend(parse_releases_atom(doer(GH_RELEASES.format(repo)), repo))
        except Exception as exc:  # noqa: BLE001 —— 一个仓库失败不连累另一个
            _warn(f"github release {repo} 失败,跳过: {exc}")
    return out


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
