# daily discover · 抓取端

每天把我关注的人的最新帖子抓下来,去重归一化后写进 R2。页面在
[discover.ken.solar](https://discover.ken.solar)(密码门),站点源码在 `discover-ken-solar`。

取代 `daily-insight` 的信息流部分。定位是「**只订阅我关注的人的阅读器**」——
全量、不评分、不翻译,judgment 交回给读者。

## 每天怎么跑起来的

```
09:00 北京    云 routine(Anthropic 沙盒)
              └─ raw.githubusercontent.com 拉本仓库的脚本 + sources.yaml
              └─ python3 run.py
                   ├─ 七个渠道各自抓 → 36h 截断 → 去重
                   ├─ 市场块(Polymarket / 打新 / 财报)+ 榜单块
                   └─ 写 R2:discover/<北京日期>.json 与 discover/index.json
08:40 北京    GitHub Actions(提前 20 分钟)
              └─ 算 GitHub 新星 / 新版本 → R2:radar/github.json
随时          页面直接读 R2,无需部署
```

**本仓库是公开的**,所以 routine 用 `raw.githubusercontent.com` 直接拉,
**没有 R2 副本兜底**——在 GitHub 上改完 `sources.yaml`,下一次运行立刻生效。
兜底是故意不留的:一份可能过期的副本会在主路径坏掉时安静地拿旧代码把今天跑完,
而报告照样全绿(见下面「索引每天被清空」那条)。

## 文件

| 文件 | 职责 |
|---|---|
| `sources.yaml` | **关注名单 + Polymarket 设置。日常只改这个文件。** |
| `tikhub.py` | Twitter / 小红书 / 公众号,走 TikHub |
| `xueqiu.py` | 雪球。**不走 TikHub**(它没有雪球),直连 `api.xueqiu.com` |
| `rss.py` | 自带 RSS/Atom 的源(也被播客渠道复用) |
| `xiaoyuzhou.py` | 小宇宙播客(它没有公开 RSS,只能抓 `__NEXT_DATA__`) |
| `blogs.py` | Anthropic / OpenAI 博客(都没有 RSS),外加榜单块里的 Paul Graham |
| `models.py` | 统一的 `Post` 模型 + 名单读取 |
| `dedup.py` | 转发去重(正文哈希,保留最早的一条) |
| `market.py` | Polymarket + 打新日历 + 财报日历(三块分开产出) |
| `radar.py` | 榜单块:Hacker News + GitHub 两组 + Paul Graham |
| `publish.py` | 写分片 + 合并 index + 上传 R2 |
| `run.py` | 主入口 |
| `sync_to_r2.py` | **只**给 Actions 用:算 GitHub 榜单写进 R2(沙盒够不着 github.com) |
| `routine_prompt.md` | 云 routine 的 prompt 真源。改完要同步到 routine 面板 |
| `tools/podfind.py` | 按名字查播客,打印一行能直接贴进 `sources.yaml` 的条目 |

## 加一个播客

```bash
python3 tools/podfind.py "Dwarkesh"        # 第一个候选
python3 tools/podfind.py "a16z" --all      # 全部候选
```

它走 Apple 的公开目录接口(免 key),打印作者、托管商、集数、**最新一集日期**,
外加一行可以直接贴进 `sources.yaml` 的 `podcast:` 条目。

最新一集日期是最该看的一行:节目在 Apple 目录里好端端挂着、feed 却几个月没更新,
是常态(BG2 就是,共 44 集,最新一集停在 2026-06-11)。

Apple 目录里搜不到的(纯 YouTube 频道)才走另一条路 —— 取频道页 HTML 里的
`"externalId":"UC..."`(**不是 `"channelId"`**,那个会命中推荐位的别家频道),
拼成 `https://www.youtube.com/feeds/videos.xml?channel_id=UC...`。注意 YouTube
频道 feed 给的是那个频道的**所有视频**,含切片 —— 2026-09-15 实测 20VC 的频道
近 15 条里 VC 正片 0 条,全是足球和板球。有播客 feed 就别用 YouTube。

## 跑

```bash
TIKHUB_API_KEY=<key> R2_ACCOUNT_ID=<id> \
R2_ACCESS_KEY_ID=<k> R2_SECRET_ACCESS_KEY=<s> python3 run.py
```

测试:`python3 -m pytest tests/ -q`(不打真实网络)

## R2 上有什么

| key | 谁写 | 干什么 |
|---|---|---|
| `discover/index.json` | `run.py` | 目录:有哪些天、各天多少条、关注名单 |
| `discover/<日期>.json` | `run.py` | 那天的分片:帖子 + stats + 市场 + 榜单 |
| `radar/github.json` | Actions | GitHub 新星 / 新版本 |

`discover/` 里是**他人写的原文加我的关注名单**,所以站点挂了密码门,
桶里的数据也只通过站点的同源 rewrite 取(R2 没有 CORS,浏览器直连必被挡)。

## 跨仓库依赖:`fetch_market.py`

`market.py` 的打新/财报日历部分 `import fetch_market`,那个文件在
**`kenzhangjie/claude` 仓库的 `ipo-earnings/`** 下,不在这里。那个仓库是**私有的**,
所以 routine 拉这两个文件时仍要带 `GH_TOKEN`。

为什么不复制过来:`ipo-earnings` 是「打新&财报」那条 routine 的真源,复制一份
就是两个真源,改一边另一边静默过期。

下不到也不会挂:`run.load_market` 包了 try/except,市场数据是补充品,失败降级。
帖子流才是主交付 —— R2 上传失败必须抛错,两个方向是刻意相反的。

## 沙盒里 GitHub 能到哪一层

2026-09-15 在真沙盒里逐条实测:

| 探测 | 结果 |
|---|---|
| `raw.githubusercontent.com` 公开仓库、不带 token | **200** |
| `raw.../<私有仓库>` 不带 token | 404(带 `GH_TOKEN` 则 200) |
| `github.com/<repo>` 网页 / `codeload` / `releases.atom` | **403** |
| `api.github.com` | **403** |
| `git clone --depth 1 https://github.com/<公开仓库>` | **成功** |

被拦的是 github.com / api.github.com 的**普通 HTTP GET**,不是 raw,也不是 git。
所以代码可以直接从 GitHub 拉,而 trending / releases 这类只能从 github.com 或 api
拿的数据,仍要靠 Actions 算好摆到 R2。

## 踩过的坑

- **默认取数器必须带 User-Agent。** TikHub、Polymarket、**R2 的 `pub-*.r2.dev`**
  都对 `Python-urllib/3.x` 回 403。注入 `get` 的单测碰不到默认实现那一层,
  所以每个注入点都配了一条**直接检查默认取数器**的回归测试。
- **不要把 403 归进「文件不存在」。** 上面那条 UA 缺失,配上 `fetch_index` 里
  `if e.code in (404, 403): return 空骨架`,后果是**每次运行都把 index 重写成只剩当天**——
  分片还在桶里,索引不再指向它,页面上唯一的症状是「加载更早一天」永远不出现。
  两天没人发现,因为核对用的是自带 UA 的 `curl`,而 routine 的自检只问
  「最新一天对不对」。R2 对真正缺失的 key 回 404,403 是权限或 WAF。
- **会覆盖历史的写操作,日志要打数量。** `run.py` 现在打印索引天数并在变少时 WARN;
  routine 的自检也加了「天数不能比昨天少」。只查「最新一条对不对」看不出历史在被削。
- **TikHub 给的正文是 HTML 转义过的。** 推文里的 `&` 到手是 `&amp;`、`->` 是 `-&gt;`。
  页面刻意用 `textContent` 渲染(正文是他人写的,不走 `innerHTML`),所以实体会原样
  显示给读者。`rss` / `xueqiu` / `xiaoyuzhou` 早就 `html.unescape` 了,`tikhub.py` 漏了。
- **雪球预热要打服务端渲染页。** `xueqiu.com/` 只下发阿里云 WAF 的 `acw_tc`,
  API 回 400016;`/u/<id>` 是 SPA 空壳。要打 `/about` 或 `/hq`。
- **雪球的 `created_at` 是毫秒**,按秒解会算到 58000 年,再被 36h 截断悄悄丢光。
  另外要带 `type=0`(原发布),否则 65–80% 是「回复@」噪音。
- **Anthropic sitemap 的 `lastmod` 是最后修改时间不是发布时间。** 2024 年的文章标着
  今年的日期,整站重建会把几百篇旧文按今天灌进时间线。要用列表页的 `publishedOn`
  校正,并且每源封顶。
- **`sources.yaml` 里 name/note 一律加双引号。** 旧的 daily-insight 名单里
  `note: Founder - Altimeter, Invest America` 没加引号,YAML 遇逗号截断,
  后半截被静默吃掉,几个月没人发现。
- **stats 要区分 `count:0, ok:true`(今天真没人发)与 `ok:false`(接口挂了)。**
  缺了这个字段页面就分不清「没有」和「没抓到」。
- **播客 / 订阅 / 博客要用更长的窗口。** 它们是周更级的,36 小时意味着一周里有六天
  这个渠道整个是空的 —— 2026-09-15 实测两个小宇宙播客最新一集是 12 天和 17 天前,
  `stats` 记 `0, ok:true`,如实,但页面上等于这个渠道不存在。`run.SLOW_CHANNELS`
  给它们 7 天。侧栏的渠道名单也要写死,空了灰着,否则连「我订了播客」都看不出来。
- **36h 窗口 + 按天分片** ⇒ 同一条帖子会出现在相邻两天的分片里,页面必须按
  `post.id` 跨天去重,日期档也要按帖子自己的时间戳算而不是分片文件名。

## 缺口

- **港股财报没有源。** `fetch_market.py` 只有 Nasdaq 的美股日历。2026-09-15 探过
  东财 datacenter 的几个 reportName,要么不存在,要么(`RPT_HKF10_FN_MAININDICATOR`)
  是已发布财报的历史财务指标,不是前瞻日历。watchlist 里的中概(BABA/PDD/TCOM)
  是美股 ADR 已被覆盖;缺的是纯港股(腾讯 0700 一类)。
- **LinkedIn**:TikHub 有端点,但 2026-09-13 实测全线 400(响应明示不扣费),
  证据在 `tests/fixtures/linkedin_400_blocked.json`。上游修好后照 `xueqiu.py`
  的结构补一个适配器即可。
- **Reddit**:`www/old.reddit.com` 的 `.json` 现在一律 403(换浏览器 UA 也一样),
  要走 TikHub 的 `/api/v1/reddit/app/fetch_subreddit_feed`。没接是因为没拿到返回结构。
- **没有跨天的硬闸。** 条数腰斩、索引天数变少、某渠道从有到零,目前只靠 routine
  用自然语言判断「正常吗」——而它判断错过一次(见上面 403 那条)。
