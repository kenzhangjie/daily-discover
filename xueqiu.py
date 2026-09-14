"""雪球适配器。不走 TikHub —— TikHub 的 27 个平台里没有雪球(2026-09-14 查
openapi.json 全表确认,xueqiu/snowball 零命中)。

也不走 RSSHub:RSSHub 的 /xueqiu/user/:id 底下打的正是下面这个接口,它用
Chromium 只是为了采一个 token cookie。而那个 cookie 纯 stdlib 就能拿到:

    GET https://xueqiu.com/        → 只下发 acw_tc(阿里云 WAF),API 回 400016
    GET https://xueqiu.com/about   → 下发 xq_a_token/xqat/u/... → API 正常返回
    GET https://xueqiu.com/u/<id>  → SPA 空壳,只有 acw_tc,不行

所以预热必须打服务端渲染页(/about),不能打用户主页。省掉了一台 RSSHub 服务
和一个 Chromium 镜像。
"""
import html
import http.cookiejar
import json
import re
import sys
import threading
import urllib.request

import models

XQ_WARM = "https://xueqiu.com/about"
XQ_API = "https://api.xueqiu.com/v4/statuses/user_timeline.json"
XQ_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
PAGES = 1                 # 每人一页 20 条,够覆盖 36h 窗口
TIMEOUT = 25

# type=0 是「原发布」。默认的 type=10(全部)里有 65-80% 是「回复@某某」的
# 一句话评论 —— 2026-09-14 实测:段永平 13/20、刘成岗 13/21、管我财 16/21。
# 那些淹进时间线会把整个页面变成评论区,而 type=0 拿到的条数并不少(还是 20 条),
# 等于白拿更多原创。
XQ_TYPE_ORIGINAL = 0

_TAG_RE = re.compile(r"<[^>]+>")


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


class Session:
    """持 cookie 的会话。预热一次即可,之后所有人共用同一份 token。

    run.collect 用 ThreadPoolExecutor(4) 并发跑同一渠道的人,所以预热要加锁 ——
    不加的话四个线程会各打一次 /about,多三次无谓请求,还可能互相覆盖 cookie。
    """

    def __init__(self, opener=None):
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._lock = threading.Lock()
        self._warm = False

    def warm(self):
        with self._lock:
            if self._warm:
                return
            req = urllib.request.Request(XQ_WARM, headers={"User-Agent": XQ_UA})
            self._opener.open(req, timeout=TIMEOUT).read()
            self._warm = True

    def timeline(self, uid, page=1):
        url = f"{XQ_API}?user_id={uid}&page={page}&type={XQ_TYPE_ORIGINAL}"
        req = urllib.request.Request(url, headers={
            "User-Agent": XQ_UA,
            "Referer": f"https://xueqiu.com/u/{uid}",
            "Accept": "application/json, text/plain, */*",
        })
        with self._opener.open(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


_session = None
_session_lock = threading.Lock()


def _default_session():
    global _session
    with _session_lock:
        if _session is None:
            _session = Session()
        return _session


def _plain(raw):
    """description 是 HTML(<br/>、<a>、实体)。页面按纯文本渲染,标记留着只会
    以字面量的形式显示出来。"""
    if not raw:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _entry(item, person):
    sid = item.get("id")
    if sid in (None, ""):
        raise ValueError("缺 id")

    title = (item.get("title") or "").strip()
    body = _plain(item.get("description"))
    # 有的条目正文本身就以标题开头,拼起来标题会显示两遍(小红书那个适配器
    # 踩过同一个坑)
    if title and body.startswith(title):
        text = body
    elif title and body:
        text = f"{title}\n\n{body}"
    else:
        text = title or body

    target = item.get("target") or ""
    url = f"https://xueqiu.com{target}" if target.startswith("/") else (
        target or f"https://xueqiu.com/u/{person['id']}")

    # created_at 是**毫秒**。按秒解会算到 58000 年,而 36h 截断会把整批悄悄
    # 丢光 —— 不报错,只表现为"雪球今天没人发"。
    ms = item.get("created_at")
    ts = models.to_utc_iso(int(ms) // 1000)

    # pic 是 'url1,url2' 的逗号分隔字符串,不是列表。当列表遍历会得到一串单字符。
    media = [u for u in (item.get("pic") or "").split(",") if u.startswith("http")]

    eng = {}
    for key, field in (("likes", "like_count"), ("replies", "reply_count"),
                       ("views", "view_count"), ("collected", "fav_count")):
        n = _int(item.get(field))
        if n:
            eng[key] = n

    rt = item.get("retweeted_status") or None
    repost_of = (rt.get("user") or {}).get("screen_name") if isinstance(rt, dict) else None

    user = item.get("user") or {}
    return models.Post(
        id=f"xueqiu:{sid}",
        channel="xueqiu",
        author_handle=str(person["id"]),
        author_name=person.get("name") or user.get("screen_name") or str(person["id"]),
        text=text,
        ts=ts,
        url=url,
        engagement=eng,
        media=media,
        repost_of=repost_of,
    )


def parse_xueqiu(payload, person):
    """逐条容错:一条坏数据不能让整个人当天消失(Task 10 的判例)。
    绝不用 now() 顶替缺失的时间戳 —— 那是编造数据。"""
    out = []
    for item in (payload or {}).get("statuses") or []:
        try:
            out.append(_entry(item, person))
        except Exception as exc:  # noqa: BLE001
            _warn(f"雪球 {person.get('id')} 有一条解析失败,跳过: {exc}")
    return out


def fetch_xueqiu(client, person, pages=PAGES, session=None):
    """client 是 TikHub 客户端,雪球用不到,保留只为与其他适配器同签名
    (run.collect 统一按 fetch(client, person) 调)。

    整人请求失败要往上抛,由 run.collect 记进 stats —— 在这里吞掉会让渠道被
    误判成 ok,页面上就分不清"没有"和"没抓到"了。
    """
    sess = session or _default_session()
    sess.warm()
    out = []
    for page in range(1, pages + 1):
        payload = sess.timeline(str(person["id"]), page)
        got = parse_xueqiu(payload, person)
        out.extend(got)
        if not (payload or {}).get("statuses"):
            break
    return out
