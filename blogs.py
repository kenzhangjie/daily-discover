"""大厂博客。没有 RSS,只能直接访问 —— 三个站三条路,因为三个站给的东西不一样。

2026-09-14 实测:

| 站 | RSS | 文章页 | 能拿到的时间 |
|---|---|---|---|
| Anthropic | 无(/rss.xml、/news/rss.xml、/engineering/rss.xml 全 404) | 200 | **列表页**的 `publishedOn`,真发布时间 |
| OpenAI | 无 | **403** | 子 sitemap 的 lastmod |
| Paul Graham | 无(rss.html 是 HTML 页) | 200 | **没有**。无 Last-Modified、无 sitemap,正文只有「September 2026」 |

所以:
- Anthropic 走列表页里的 Next.js 流式载荷(`self.__next_f`),一次拿全
  publishedOn / slug / summary / title。文章页里**没有** publishedOn,只有列表页有。
- OpenAI 只能走 sitemap。文章页和列表页都 403,标题只能从 slug 反推。
  它的 lastmod 看着像发布时间(日期是散开的),但无从验证。
- Paul Graham **进不了时间线**:36h 截断需要真时间戳,他只有月粒度。
  放进榜单块(radar.py 消费 `fetch_pg_latest`),显示最新几篇带月份。

两个共同的防线:
- **每源封顶 SITE_CAP 条**。sitemap 的 lastmod 是「最后修改」不是「发布」——
  实测 Anthropic 的 building-effective-agents(2024 年的文章)标着 2026-08-10。
  站点整体重建会把所有旧文的 lastmod 刷成今天,不封顶就是 257 条旧文灌进当天。
- Anthropic 优先用 publishedOn,只有找不到时才退回 lastmod。
"""
import html as _html
import json
import re
import sys
import urllib.request

import models

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
TIMEOUT = 25
SITE_CAP = 5

ANTHROPIC_INDEXES = ("https://www.anthropic.com/news",
                     "https://www.anthropic.com/engineering")
ANTHROPIC_SITEMAP = "https://www.anthropic.com/sitemap.xml"
ANTHROPIC_SECTIONS = ("/news/", "/engineering/")
OPENAI_SITEMAPS = ("https://openai.com/sitemap.xml/company/",
                   "https://openai.com/sitemap.xml/engineering/")
PG_INDEX = "https://paulgraham.com/articles.html"
PG_TOP_N = 3

_NEXT_F = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)', re.S)
_PUBLISHED = re.compile(r'"publishedOn":"([^"]+)"')
_SLUG = re.compile(r'"current":"([^"]+)"')
_TITLE = re.compile(r'"title":"((?:[^"\\]|\\.)*)"')
_SUMMARY = re.compile(r'"summary":"((?:[^"\\]|\\.)*)"')
_LOCMOD = re.compile(r"<loc>(.*?)</loc>\s*<lastmod>(.*?)</lastmod>", re.S)
_MONTH = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(20\d\d)\b")


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _unescape_json_str(s):
    try:
        return json.loads('"' + s + '"')
    except ValueError:
        return s


def _iso(raw):
    """publishedOn 形如 2026-09-01T23:31:00.000Z,lastmod 形如 2026-09-11T02:45:01.000Z。
    统一成秒级 Z。"""
    s = (raw or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", s)
    if not m:
        raise ValueError(f"看不懂的时间: {raw!r}")
    return m.group(1) + "Z"


def _slug_title(slug):
    return (slug or "").replace("-", " ").strip().capitalize()


# ---------------------------------------------------------------- Anthropic

def parse_anthropic(page, source_url, cap=SITE_CAP):
    """列表页的 Next.js 流式载荷里,每条形如
    ..."publishedOn":"ISO","slug":{...,"current":"SLUG"},"subjects":[...],
       "summary":"...","title":"..."
    字段顺序偶有出入,所以从 publishedOn 往后一个窗口内找,不写死顺序。"""
    chunks = _NEXT_F.findall(page or "")
    if not chunks:
        raise ValueError("页面里没有 self.__next_f,Anthropic 可能改版了")
    blob = "".join(json.loads(c) for c in chunks)

    seen, out = {}, []
    for m in _PUBLISHED.finditer(blob):
        window = blob[m.end():m.end() + 3000]
        slug_m = _SLUG.search(window)
        if not slug_m:
            continue
        slug = slug_m.group(1)
        try:
            ts = _iso(m.group(1))
        except ValueError:
            continue
        # 同一 slug 在载荷里会重复出现,留时间最新的一份
        if slug in seen and seen[slug] >= ts:
            continue
        seen[slug] = ts
        title_m = _TITLE.search(window)
        summary_m = _SUMMARY.search(window)
        out.append({
            "slug": slug,
            "ts": ts,
            "title": _unescape_json_str(title_m.group(1)) if title_m else _slug_title(slug),
            "summary": _unescape_json_str(summary_m.group(1)) if summary_m else "",
            "url": f"{source_url.rstrip('/')}/{slug}",
        })
    # 去掉被更新的旧副本
    out = [e for e in out if seen.get(e["slug"]) == e["ts"]]
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:cap] if cap else out


def anthropic_published_map(get=None):
    """{slug: 该条的完整信息(真发布时间 + 标题 + 摘要)}。两个列表页各有缺口,取并集。
    列表页只抓一次,标题摘要和发布时间一起带出来。"""
    doer = get or _default_get
    out = {}
    for url in ANTHROPIC_INDEXES:
        try:
            for e in parse_anthropic(doer(url), url, cap=None):
                out.setdefault(e["slug"], e)
        except Exception as exc:  # noqa: BLE001 —— 校正表拿不到就退回 lastmod
            _warn(f"anthropic 列表页 {url} 读不到,该页的发布时间校正失效: {exc}")
    return out


def fetch_anthropic(get=None):
    """sitemap 出候选(完整),列表页的 publishedOn 做校正(准确)。

    两边单用都不行:sitemap 的 lastmod 是「最后修改」,实测 2024 年的
    building-effective-agents 标着 2026-08-10,整站重建会把 257 篇旧文全刷成
    今天;而列表页漏文章,实测 /news/claude-agents 一类不在 CMS 列表里,
    只靠它会永远看不到那些文章。
    """
    doer = get or _default_get
    published = anthropic_published_map(get=get)
    try:
        xml = doer(ANTHROPIC_SITEMAP)
    except Exception as exc:  # noqa: BLE001
        _warn(f"anthropic sitemap 拿不到,退回只用列表页: {exc}")
        rows = list(published.values())
        rows.sort(key=lambda e: e["ts"], reverse=True)
        return rows[:SITE_CAP]

    rows = []
    for loc, mod in _LOCMOD.findall(xml or ""):
        loc = loc.strip()
        if not any(sec in loc for sec in ANTHROPIC_SECTIONS):
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        if slug in ("news", "engineering"):
            continue
        d = published.get(slug) or {}
        try:
            ts = d.get("ts") or _iso(mod)
        except ValueError:
            continue
        rows.append({"slug": slug, "ts": ts,
                     "title": d.get("title") or _slug_title(slug),
                     "summary": d.get("summary", ""), "url": loc})
    rows.sort(key=lambda e: e["ts"], reverse=True)
    return rows[:SITE_CAP]


# ---------------------------------------------------------------- OpenAI

def parse_openai_sitemap(xml):
    """只要 /index/<slug>/ 这种真文章页;/news/ 之类是栏目索引页,不是内容。"""
    out = []
    for loc, mod in _LOCMOD.findall(xml or ""):
        loc = loc.strip()
        if "/index/" not in loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        try:
            ts = _iso(mod)
        except ValueError:
            continue
        out.append({"slug": slug, "ts": ts, "title": _slug_title(slug),
                    "summary": "", "url": loc})
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:SITE_CAP]


def fetch_openai(get=None):
    doer = get or _default_get
    rows, seen = [], set()
    for url in OPENAI_SITEMAPS:
        try:
            got = parse_openai_sitemap(doer(url))
        except Exception as exc:  # noqa: BLE001
            _warn(f"openai {url} 抓取失败,跳过: {exc}")
            continue
        for e in got:
            if e["url"] in seen:
                continue
            seen.add(e["url"])
            rows.append(e)
    rows.sort(key=lambda e: e["ts"], reverse=True)
    return rows[:SITE_CAP]


# ---------------------------------------------------------------- 进时间线

SITES = {
    "anthropic": ("Anthropic", fetch_anthropic),
    "openai": ("OpenAI", fetch_openai),
}


def fetch_blog(client, person, get=None):
    """client 是 TikHub 客户端,这里用不到,保留只为与其他适配器同签名。
    整站失败往上抛给 run.collect 记 stats。"""
    key = str(person["id"])
    if key not in SITES:
        raise ValueError(f"不认识的博客源 {key!r},只支持 {sorted(SITES)}")
    label, fetch = SITES[key]
    out = []
    for e in fetch(get=get):
        text = f"{e['title']}\n\n{e['summary']}" if e["summary"] else e["title"]
        out.append(models.Post(
            id=f"blog:{key}:{e['slug']}",
            channel="blog",
            author_handle=key,
            author_name=person.get("name") or label,
            text=text,
            ts=e["ts"],
            url=e["url"],
        ))
    return out


# ---------------------------------------------------------------- 进榜单块

def fetch_pg_latest(get=None, top_n=PG_TOP_N):
    """Paul Graham 进不了时间线:没有任何可用时间戳(无 Last-Modified、无 sitemap、
    无 RSS),正文里只有「September 2026」这种月粒度。所以放榜单块,显示最新几篇
    带月份 —— 榜单块本来就是「此刻的状态」而不是「最近 36 小时发生了什么」。

    articles.html 按时间倒序排,取最前面几条;月份要进文章页才拿得到,所以只对
    这几条各打一次。
    """
    doer = get or _default_get
    index = doer(PG_INDEX)
    links = re.findall(r'<a href="([a-z0-9]+\.html)">([^<]{3,90})</a>', index)
    # 前面几条是 index/articles/books 这类导航,只认正文里成对出现的文章链接
    skip = {"index.html", "articles.html", "books.html", "rss.html", "bel.html",
            "arc.html", "faq.html"}
    out = []
    for href, title in links:
        if href in skip:
            continue
        url = f"https://paulgraham.com/{href}"
        month = ""
        try:
            body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", doer(url)))
            m = _MONTH.search(body)
            month = f"{m.group(1)[:3]} {m.group(2)}" if m else ""
        except Exception as exc:  # noqa: BLE001 —— 拿不到月份不影响这一条能用
            _warn(f"paulgraham {href} 取月份失败: {exc}")
        out.append({"title": _html.unescape(title).strip(), "url": url,
                    "note": month})
        if len(out) >= top_n:
            break
    return out
