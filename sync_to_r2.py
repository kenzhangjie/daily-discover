"""把源码同步到 R2 的 code/ 前缀,给云 routine 做取文件的兜底。

为什么需要:云 routine 走 raw.githubusercontent.com 取文件,但 GH_TOKEN 是个
细粒度 PAT,只授权了 kenzhangjie/claude 那一个仓库 —— 本仓库的 raw 返回 404
(2026-09-14 实测,对照组 claude 仓库同时是 200)。给 PAT 加授权要人去 GitHub
面板点,而 R2 这条路本仓库自己就能维护。

两条路同时在:routine 先试 GitHub,404 再退 R2。哪条通都能跑。

用法:本地 `python3 sync_to_r2.py`(读环境变量里的 R2 凭证),
或让 .github/workflows/sync-r2.yml 在每次 push 后自动跑。
"""
import os
import sys

import publish

FILES = ["models.py", "tikhub.py", "xueqiu.py", "rss.py", "xiaoyuzhou.py",
         "blogs.py", "dedup.py", "market.py", "radar.py", "publish.py",
         "run.py", "sources.yaml"]

CONTENT_TYPE = {".py": "text/x-python", ".yaml": "text/yaml"}


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
