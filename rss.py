"""通用 RSS / Atom 适配器。

覆盖任何自带 feed 的「人」—— Substack、个人博客。不覆盖榜单类的源,那些走
radar.py,在页面上也是分开呈现的(时间线只放我关注的人)。

自己解析而不用 feedparser:沙盒里每多一个 pip 依赖就多一个失败点,而 RSS 里
我们只要 title / link / pubDate 三个字段,正则足够。代价是遇到畸形 XML 会漏条,
所以逐条容错 —— 漏一条比整源失败好。

已知拿不到 feed 的(别再试):
- paulgraham.com/rss.html 是 HTML 页不是 feed;社区镜像 aaronsw.com 的条目
  **没有日期字段**,最新一条还停在 2023 年,无法参与 36h 截断
- anthropic.com / claude.com 的博客没有任何 RSS 端点(试过 /rss.xml、
  /news/rss.xml、/engineering/rss.xml,全 404)
"""
import html
import re
import sys
import urllib.request
from datetime import datetime, timezone

import models

USER_AGENT = "daily-discover/1.0"
TIMEOUT = 25

_ITEM_RE = re.compile(r"<(?:item|entry)\b[^>]*>(.*?)</(?:item|entry)>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

# RSS 的 pubDate 和 Atom 的 updated/published 格式各异,逐个试
_DATE_FMTS = (
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
)


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


def _default_get(url):
    """必须带 User-Agent:不少 feed 主机对默认的 Python-urllib 直接 403,
    而适配器的容错是"失败就跳过",线上只会表现为这个源天天是空的。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _field(block, *tags):
    for tag in tags:
        m = re.search(r"<{0}\b[^>]*>(.*?)</{0}>".format(tag), block, re.S | re.I)
        if not m:
            continue
        val = m.group(1).strip()
        cd = re.match(r"<!\[CDATA\[(.*?)\]\]>", val, re.S)
        if cd:
            val = cd.group(1).strip()
        if val:
            return val
    return ""


def _link(block):
    # Atom 把链接放在属性里:<link rel="alternate" href="..."/>
    plain = _field(block, "link")
    if plain and not plain.startswith("<"):
        return plain
    m = re.search(r"<link\b[^>]*href=[\"']([^\"']+)[\"']", block, re.I)
    return m.group(1) if m else ""


def _plain(raw, limit=600):
    """摘要是 HTML。页面按纯文本渲染,标记留着会以字面量显示出来。
    截断是因为有的 feed 把全文塞进 description,一条能顶满整屏。"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>|</p>", "\n", raw, flags=re.I)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _ts(raw):
    s = (raw or "").strip().replace("GMT", "+0000")
    for fmt in _DATE_FMTS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(f"看不懂的时间格式: {raw!r}")


def parse_rss(xml, person, channel="rss"):
    """逐条容错。缺时间戳的条目直接丢弃 —— 绝不用 now() 顶替,那会让一篇
    三年前的旧文每天都显示成"刚发布"。"""
    out = []
    for block in _ITEM_RE.findall(xml or ""):
        try:
            title = _plain(_field(block, "title"), limit=300)
            link = _link(block)
            ts = _ts(_field(block, "pubDate", "updated", "published", "dc:date"))
            body = _plain(_field(block, "description", "summary", "content:encoded"))
            text = f"{title}\n\n{body}" if body and not body.startswith(title) else (title or body)
            out.append(models.Post(
                id=f"{channel}:{link or title}",
                channel=channel,
                author_handle=str(person["id"]),
                author_name=person.get("name") or str(person["id"]),
                text=text,
                ts=ts,
                url=link,
            ))
        except Exception as exc:  # noqa: BLE001
            _warn(f"{channel} {person.get('id')} 有一条解析失败,跳过: {exc}")
    return out


def fetch_rss(client, person, get=None):
    """client 是 TikHub 客户端,这里用不到,保留只为与其他适配器同签名。

    整源失败往上抛,由 run.collect 记进 stats —— 吞掉的话渠道会被误判成 ok,
    页面上就分不清「没有」和「没抓到」。
    """
    doer = get or _default_get
    feed = person.get("feed") or person["id"]
    return parse_rss(doer(feed), person)
