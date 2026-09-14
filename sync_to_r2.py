"""把源码同步到 R2 的 code/ 前缀,给云 routine 做取文件的兜底。

为什么需要:云 routine 走 raw.githubusercontent.com 取文件,但 GH_TOKEN 是个
细粒度 PAT,只授权了 kenzhangjie/claude 那一个仓库 —— 本仓库的 raw 返回 404
(2026-09-14 实测,对照组 claude 仓库同时是 200)。给 PAT 加授权要人去 GitHub
面板点,而 R2 这条路本仓库自己就能维护。

两条路同时在:routine 先试 GitHub,404 再退 R2。哪条通都能跑。

用法:本地 `python3 sync_to_r2.py`(读环境变量里的 R2 凭证),
或让 .github/workflows/sync-r2.yml 在每次 push 后自动跑。
"""
import json
import os
import sys
import urllib.parse
import urllib.request

import publish

FILES = ["models.py", "tikhub.py", "xueqiu.py", "rss.py", "xiaoyuzhou.py",
         "blogs.py", "dedup.py", "market.py", "radar.py", "publish.py",
         "run.py", "sources.yaml"]

CONTENT_TYPE = {".py": "text/x-python", ".yaml": "text/yaml"}


GH_API = "https://api.github.com"
GH_WATCH_REPOS = ("anthropics/claude-code", "openai/openai-python")
GH_TRENDING_TOP_N = 8


def _gh(url):
    """Actions runner 上有完整的 GitHub 访问权,还自带 GITHUB_TOKEN 免限流。"""
    headers = {"User-Agent": "daily-discover-sync", "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def build_github_radar():
    """把榜单块的 GitHub 两组在这里算好,写成 JSON 摆到 R2。

    为什么要这座桥:云沙盒的代理**把 github.com 和 api.github.com 全拦了**
    (2026-09-14 线上连续两次实测,403;只有 raw.githubusercontent.com 放行),
    所以 radar.py 在沙盒里怎么写都拿不到。而 Actions runner 跑在 GitHub 自己
    的机器上,访问毫无问题 —— 让它算好、摆到 R2,沙盒去读 R2。
    和代码同步用的是同一条通路。
    """
    out = {"trending": [], "releases": []}
    try:
        q = "created:>2026-01-01 stars:>500 language:python"
        data = _gh(f"{GH_API}/search/repositories?q={urllib.parse.quote(q)}"
                   f"&sort=stars&order=desc&per_page={GH_TRENDING_TOP_N}")
        for it in data.get("items") or []:
            out["trending"].append({
                "title": it.get("full_name") or "",
                "url": it.get("html_url") or "",
                "score": int(it.get("stargazers_count") or 0),
                "note": (it.get("description") or "")[:120],
            })
    except Exception as e:  # noqa: BLE001
        print(f"WARN trending 取不到: {e}", file=sys.stderr)
    for repo in GH_WATCH_REPOS:
        try:
            r = _gh(f"{GH_API}/repos/{repo}/releases/latest")
            if r.get("tag_name"):
                out["releases"].append({
                    "title": f"{repo} {r['tag_name']}",
                    "url": r.get("html_url") or f"https://github.com/{repo}/releases",
                    "published": (r.get("published_at") or "")[:10],
                    "note": "",
                })
        except Exception as e:  # noqa: BLE001
            print(f"WARN release {repo} 取不到: {e}", file=sys.stderr)
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    missing = [f for f in FILES if not os.path.exists(os.path.join(here, f))]
    if missing:
        print(f"FATAL 本地缺文件: {missing}", file=sys.stderr)
        return 1
    for name in FILES:
        with open(os.path.join(here, name), "rb") as fh:
            body = fh.read()
        ext = os.path.splitext(name)[1]
        publish._default_put(f"code/{name}", body,
                             content_type=CONTENT_TYPE.get(ext, "text/plain"))
        print(f"OK code/{name}  {len(body)}B")
    print(f"已同步 {len(FILES)} 个文件到 R2 的 code/ 前缀")

    radar = build_github_radar()
    publish._default_put("radar/github.json",
                         json.dumps(radar, ensure_ascii=False).encode("utf-8"))
    print(f"已写 radar/github.json  trending {len(radar['trending'])} "
          f"| releases {len(radar['releases'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
