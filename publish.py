"""分片与索引的组装。同日重跑覆盖不追加 —— merge_index 按 date 替换。"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

R2_PUBLIC = "https://pub-42a2b5c1ec984024833f48ca358f4571.r2.dev"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_shard(date, posts, stats, market, radar=None):
    """radar 默认给空壳而不是省略这个键 —— 页面按键存在与否决定要不要渲染那一块,
    键时有时无会让旧分片和新分片走两条不同的渲染路径。"""
    ordered = sorted(posts, key=lambda p: p.ts, reverse=True)
    return {
        "date": date,
        "generated_at": _now_iso(),
        "stats": stats,
        "posts": [p.to_dict() for p in ordered],
        "market": market,
        "radar": radar or {"hn": [], "github_trending": [], "github_releases": []},
    }


def build_index(days, authors):
    return {
        "updated_at": _now_iso(),
        "days": sorted(days, key=lambda d: d["date"], reverse=True),
        "authors": authors,
    }


def merge_index(existing, day_entry):
    days = [d for d in (existing.get("days") or []) if d["date"] != day_entry["date"]]
    days.append(day_entry)
    return build_index(days, existing.get("authors") or [])


class IndexFetchFailed(RuntimeError):
    pass


def fetch_index(get=None):
    """拉线上现有 index。只有「文件确实不存在」(HTTPError 404,或 R2 public
    桶对不存在的 key 也可能返回的 403,一并当作"不存在")才返回空骨架。

    其他任何失败 —— 网络错误、超时、5xx、JSON 解析失败 —— 都必须抛出
    IndexFetchFailed,不能吞掉:index 是唯一的目录,一旦在网络抖动时把它
    静默替换成空骨架,历史日期就会从索引里永久消失(分片还在 R2,但页面
    再也找不到),宁可今天不更新,也不能清空历史。
    """
    doer = get or _default_get
    try:
        raw = doer(f"{R2_PUBLIC}/discover/index.json")
    except urllib.error.HTTPError as e:
        if e.code in (404, 403):
            return {"days": [], "authors": []}
        raise IndexFetchFailed(f"index.json 拉取失败(HTTP {e.code}): {e}") from e
    except Exception as e:
        raise IndexFetchFailed(f"index.json 拉取失败: {e}") from e
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise IndexFetchFailed(f"index.json 解析失败: {e}") from e


def _default_get(url):
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read()


class UploadFailed(RuntimeError):
    pass


def upload_r2(key, payload, put=None, sleep=time.sleep, retries=3):
    """上传一个 JSON 到 R2。失败重试 3 次后抛错 —— 绝不能静默失败,
    否则页面会停更而没人知道。

    put(key, body_bytes) 由调用方注入,默认实现读环境变量里的 R2 凭证。
    """
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    doer = put or _default_put
    last = None
    for attempt in range(retries):
        try:
            doer(key, body)
            return
        except Exception as exc:
            last = exc
            if attempt < retries - 1:
                sleep(2 ** attempt)
    raise UploadFailed(f"R2 上传失败 {key}: {last}")


def _default_put(key, body, content_type="application/json"):
    """纯 Python SigV4 PUT。沙盒里没有 rclone/boto3,所以自己签。

    这段是从 daily-insight/fetch_all.py 的 _r2_put 复制来的 —— 没有 import
    复用,因为云沙盒是按 raw URL 逐个文件下载的,跨项目 import 会逼着它
    多下一个 32KB 的无关文件。35 行的重复换部署独立性,值。
    """
    import hashlib
    import hmac
    import urllib.parse
    import urllib.request
    from datetime import datetime, timezone

    acct = os.environ["R2_ACCOUNT_ID"]
    akey = os.environ["R2_ACCESS_KEY_ID"]
    secret = os.environ["R2_SECRET_ACCESS_KEY"]
    bucket = os.environ.get("R2_BUCKET", "ldoce-audio")
    host = f"{acct}.r2.cloudflarestorage.com"
    region, service = "auto", "s3"
    now = datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_uri = "/" + bucket + "/" + urllib.parse.quote(key, safe="/")
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amzdate}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        ["PUT", canonical_uri, "", canonical_headers, signed_headers, payload_hash])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amzdate, scope,
         hashlib.sha256(canonical_request.encode()).hexdigest()])

    def _sign(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    ksigning = _sign(_sign(_sign(_sign(("AWS4" + secret).encode(), datestamp),
                                 region), service), "aws4_request")
    signature = hmac.new(ksigning, string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={akey}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")
    req = urllib.request.Request(
        f"https://{host}{canonical_uri}", data=body, method="PUT",
        headers={"Host": host, "x-amz-date": amzdate,
                 "x-amz-content-sha256": payload_hash,
                 "Authorization": auth, "Content-Type": content_type})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status not in (200, 204):
            raise OSError(f"R2 PUT {key} -> HTTP {resp.status}")
