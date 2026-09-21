"""LinkedIn 适配器。走 Asklear 的异步采集 REST,不走 TikHub ——
TikHub 的 LinkedIn 2026-09-13 实测全线 400(证据在
tests/fixtures/linkedin_400_blocked.json),供给断了。

## REST 路径是探出来的,不是文档给的

Asklear 公开文档只给了 CLI 封装(`asklear collection ...`),没有 REST 路径,
也没有公开 OpenAPI(/openapi.json 等五个位置全 404)。下面三个端点是
2026-09-21 用「401 = 存在但要鉴权 / 404 = 不存在」探出来、再实跑验证的:

    POST /v1/collections/estimate     → quote_token + upper_bound_credits
    POST /v1/collections/jobs         → job_id        ← 注意不是 /start,那个 404
    GET  /v1/collections/jobs/<id>    → status + result

`/v1/collection/*`(单数)全是 404,复数才对。

## 贵,所以不能每天全员拉

linkedin_user_posts_v1 固定 **84 积分/次**,是本项目最贵的渠道。同一家的
Reddit / X 拉用户帖子只要 2 积分、抖音公众号 3、小红书 8。分界线是登录态:
LinkedIn 所有列表接口都在登录墙后面,而同平台的 post_detail(匿名可访问)
只要 2 积分。

所以 run.py 按 sources.yaml 里的 slot 四天轮一圈,每天只拉一组。

**不翻页**。2026-09-21 实测 chamath:一次返回 50 条,跨度 2026-05-25 ~ 09-18,
将近四个月。36 小时窗口下绝大部分会被 run.collect 截掉,再翻页纯属浪费钱。

## 实测纠正了 describe 文档的两处

describe_collection_task 的 limitations 说过滤要看 `poster_linkedin_url`,
**实际返回里没有这个字段**,能用的是 `author_url`。另外它说
「同一端点也返回评论过/点赞过的帖」—— 50 条样本里没出现这种,6 条非本人的
全部是 `is_reshared: true` 的转发。两个判据(author_url 指向谁、is_reshared)
在样本里完全一致。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import models

BASE = os.environ.get("ASKLEAR_BASE_URL", "https://api.asklear.cn")
TASK_CODE = "linkedin_user_posts_v1"
CREDITS_PER_CALL = 84
TIMEOUT = 60            # 服务端单次 HTTP 上限 120s,这里留一半给重试
POLL_MAX_SECONDS = 180  # 官方估时 2-30s,给六倍余量
HTTP_RETRIES = 2

# 转发保留还是丢掉。保留 —— 转发是本人主动放到时间线上的内容,跟「评论过的帖」
# 不是一回事;LinkedIn 本来就发得稀(chamath 四个月 50 条),再砍掉 12% 这个
# 渠道会更空。转发会带上 repost_of,页面上分得出来。
# 要改成只留原创:把这里改 False,parse 会把 is_reshared 的全丢掉。
KEEP_RESHARES = True


def _warn(msg):
    print(f"WARN {msg}", file=sys.stderr)


class AsklearError(RuntimeError):
    pass


def _slug(url):
    """从 LinkedIn 链接里取 vanity name,用来判断这条是不是本人发的。

    必须归一化:实测 profile 接口对 deykhan 返回的是
    `https://ua.linkedin.com/in/Deykhan` —— 域名带国家前缀、大小写也不一样。
    只取 path 末段并转小写,两个维度一起解决。
    """
    if not url:
        return ""
    return urllib.parse.urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].lower()


class Asklear:
    """Asklear 异步采集客户端。估价 → 启动 → 轮询。

    并发:服务端默认每租户同时最多 10 个活跃任务(排队+运行合计),超了回 429
    `collection_queue_full`,且**服务端不会自动重试**。run.collect 用
    ThreadPoolExecutor(4),4 < 10,安全;真要提高并发先改这个数。
    """

    def __init__(self, api_key=None, base=BASE, opener=None):
        self.api_key = api_key or os.environ.get("ASKLEAR_API_KEY")
        self.base = base.rstrip("/")
        self._opener = opener or urllib.request.build_opener()

    def _call(self, method, path, body=None):
        if not self.api_key:
            raise AsklearError("缺 ASKLEAR_API_KEY")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"})
        last = None
        for attempt in range(HTTP_RETRIES + 1):
            try:
                with self._opener.open(req, timeout=TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                # 4xx 是请求本身的问题,重试只是重复犯错 —— 唯独 429 例外
                if e.code != 429 and 400 <= e.code < 500:
                    raise AsklearError(f"{method} {path} → HTTP {e.code}: {detail}") from e
                last = AsklearError(f"{method} {path} → HTTP {e.code}: {detail}")
            except Exception as e:  # noqa: BLE001
                last = AsklearError(f"{method} {path} 失败: {e}")
            if attempt < HTTP_RETRIES:
                time.sleep(2 ** attempt)
        raise last

    def estimate(self, task_code, payload):
        # 不发 task_query。MCP 收这个字段,**REST 不收** —— 2026-09-21 实测
        # estimate 回 400 extra_forbidden。官方文档那句「REST calls may omit it
        # and remain unattributed」读起来像可选,实际是这个端点压根不接受。
        # 代价:REST 发起的任务在用量明细里没有任务归因,只能靠 idempotency_key
        # 认出是谁发的。
        return self._call("POST", "/v1/collections/estimate",
                          {"task_code": task_code, "input": payload})

    def start(self, task_code, payload, quote_token, idempotency_key,
              max_credits=None):
        body = {"task_code": task_code, "input": payload,
                "quote_token": quote_token, "idempotency_key": idempotency_key}
        if max_credits is not None:
            body["max_credits"] = max_credits
        return self._call("POST", "/v1/collections/jobs", body)   # 同上,不发 task_query

    def wait(self, job_id, max_seconds=POLL_MAX_SECONDS, sleep=time.sleep):
        """轮询到终态。按服务端给的 poll_after_seconds 退避。

        超时抛错而不是返回空 —— 上层 run.collect 靠异常把这个人记进 stats,
        静默返回空会被页面读成「他今天没发」。
        """
        waited = 0.0
        while waited < max_seconds:
            got = self._call("GET", f"/v1/collections/jobs/{job_id}")
            status = got.get("status")
            if status == "succeeded":
                return got
            if status in ("failed", "cancelled", "expired"):
                raise AsklearError(f"任务 {job_id} {status}: {got.get('error')}")
            nap = float(got.get("poll_after_seconds") or 3)
            sleep(nap)
            waited += nap
        raise AsklearError(f"任务 {job_id} 超过 {max_seconds}s 仍未完成")

    def collect(self, task_code, payload, idempotency_key, max_credits=None,
                sleep=time.sleep):
        """估价 → 启动 → 等结果,一步到位。

        估价是免费的,但**必须先估**:start 要带 quote_token。
        """
        quote = self.estimate(task_code, payload)
        token = quote.get("quote_token")
        if not token:
            raise AsklearError(f"估价没返回 quote_token: {quote}")
        started = self.start(task_code, payload, token, idempotency_key,
                             max_credits=max_credits)
        job_id = started.get("job_id")
        if not job_id:
            raise AsklearError(f"启动没返回 job_id: {started}")
        return self.wait(job_id, sleep=sleep)


_client = None


def _default_client():
    global _client
    if _client is None:
        _client = Asklear()
    return _client


def _entry(item, person):
    pid = item.get("post_id")
    if pid in (None, ""):
        raise ValueError("缺 post_id")

    text = (item.get("text") or "").strip()
    # Pulse 长文:text 是本人写的导语,article_title 是被链接文章的标题。
    # 导语为空时用标题兜底,不做拼接 —— 拼出来的格式是我发明的,不是数据里的。
    if not text:
        text = (item.get("article_title") or "").strip()
    if not text:
        raise ValueError(f"{pid} 正文和 article_title 都是空")

    # published_at 是 unix **秒**(实测 1789742981 → 2026-09-18)。
    # 当毫秒解会算到公元五万年,36h 截断会把整批悄悄丢光 —— 不报错,
    # 只表现为「LinkedIn 今天没人发」。雪球那个适配器踩过反过来的同一个坑。
    ts = models.to_utc_iso(int(item["published_at"]))

    eng = {}
    for key, field in (("likes", "like_count"), ("replies", "comment_count"),
                       ("reposts", "repost_count")):
        try:
            n = int(item.get(field) or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            eng[key] = n

    # author_name 实测 50/50 全是 null,显示名只能取 sources.yaml 里的 name
    author_slug = _slug(item.get("author_url"))
    repost_of = author_slug if (item.get("is_reshared")
                                and author_slug != str(person["id"]).lower()) else None

    return models.Post(
        id=f"linkedin:{pid}",
        channel="linkedin",
        author_handle=str(person["id"]),
        author_name=person.get("name") or str(person["id"]),
        text=text,
        ts=ts,
        url=item.get("url") or f"https://www.linkedin.com/in/{person['id']}",
        engagement=eng,
        media=[],
        repost_of=repost_of,
    )


def parse_linkedin(payload, person, keep_reshares=KEEP_RESHARES):
    """把 data.items 归一化成 Post,并滤掉不属于这个人的条目。

    过滤判据是 author_url 的 vanity name。上游这个端点混着返回本人发的、
    本人转发的,文档还说可能混进「评论过/点赞过」的帖(50 条样本里没见到)。
    既不是本人发的、也不是本人转发的,一律丢 —— 那种只可能是污染。

    逐条容错:一条坏数据不能让整个人当天消失。
    """
    me = str(person["id"]).lower()
    out = []
    for item in (payload or {}).get("items") or []:
        try:
            mine = _slug(item.get("author_url")) == me
            reshared = bool(item.get("is_reshared"))
            if not mine and not reshared:
                continue          # 既不是他发的也不是他转的 → 污染
            if not mine and not keep_reshares:
                continue
            out.append(_entry(item, person))
        except Exception as exc:  # noqa: BLE001
            _warn(f"LinkedIn {person.get('id')} 有一条解析失败,跳过: {exc}")
    return out


def fetch_linkedin(client, person, asklear=None, sleep=time.sleep):
    """client 是 TikHub 客户端,LinkedIn 用不到,保留只为与其他适配器同签名
    (run.collect 统一按 fetch(client, person) 调),跟 xueqiu.py 一样。

    整人失败要往上抛,由 run.collect 记进 stats —— 在这里吞掉会让渠道被误判
    成 ok,页面上就分不清「没有」和「没抓到」了。
    """
    api = asklear or _default_client()
    url = f"https://www.linkedin.com/in/{person['id']}"
    # 幂等键带日期:同一天重跑复用同一个 job(不重复扣 84 积分),
    # 换一天就是新任务。
    day = time.strftime("%Y%m%d", time.gmtime())
    key = f"dd-linkedin-{person['id']}-{day}"
    got = api.collect(TASK_CODE, {"url": url}, key,
                      max_credits=CREDITS_PER_CALL, sleep=sleep)
    return parse_linkedin((got.get("result") or {}).get("data"), person)
