# daily discover · 抓取端

每天把我关注的人的最新帖子抓下来,去重归一化后写进 R2。页面在
[discover.ken.solar](https://discover.ken.solar)(密码门),站点源码在 `discover-ken-solar`。

取代 `daily-insight` 的信息流部分。定位是「**只订阅我关注的人的阅读器**」——
全量、不评分、不翻译,judgment 交回给读者。

## 文件

| 文件 | 职责 |
|---|---|
| `sources.yaml` | **关注名单。日常只改这个文件。** |
| `tikhub.py` | Twitter / 小红书 / 公众号 三个渠道,走 TikHub |
| `xueqiu.py` | 雪球。**不走 TikHub**(它没有雪球),直连 `api.xueqiu.com` |
| `models.py` | 统一的 `Post` 模型 + 名单读取 |
| `dedup.py` | 转发去重(正文哈希,保留最早的一条) |
| `market.py` | Polymarket + 打新/财报日历 |
| `publish.py` | 写分片 + 重写 index + 上传 R2 |
| `run.py` | 主入口 |

## 跑

```bash
TIKHUB_API_KEY=<key> R2_ACCOUNT_ID=<id> \
R2_ACCESS_KEY_ID=<k> R2_SECRET_ACCESS_KEY=<s> python3 run.py
```

测试:`python3 -m pytest tests/ -q`(不打真实网络)

## 跨仓库依赖:`fetch_market.py`

`market.py` 的打新/财报日历部分 `import fetch_market`,那个文件在
**`kenzhangjie/claude` 仓库的 `ipo-earnings/`** 下,不在这里。

为什么不复制过来:`ipo-earnings` 还有「打新&财报」那条 routine 在用,复制一份
就是两个真源,改一边另一边静默过期。

云 routine 按 raw URL 逐个下文件,所以要多下一个:

```
https://raw.githubusercontent.com/kenzhangjie/claude/main/ipo-earnings/fetch_market.py
  → 放到 ../ipo-earnings/fetch_market.py
https://raw.githubusercontent.com/kenzhangjie/claude/main/ipo-earnings/watchlist.yaml
  → 同目录
```

下不到也不会挂:`run.load_market` 包了 try/except,市场数据是补充品,失败降级。
帖子流才是主交付 —— R2 上传失败必须抛错,两个方向是刻意相反的。

## 踩过的坑

- **默认取数器必须带 User-Agent。** TikHub 和 Polymarket 的 WAF 都对
  `Python-urllib/3.x` 回 403。注入 `get` 的单测碰不到默认实现那一层,所以各配了
  一条断言 UA 非空的回归测试。
- **雪球预热要打服务端渲染页。** `xueqiu.com/` 只下发阿里云 WAF 的 `acw_tc`,
  API 回 400016;`/u/<id>` 是 SPA 空壳。要打 `/about` 或 `/hq`。
- **雪球的 `created_at` 是毫秒**,按秒解会算到 58000 年,再被 36h 截断悄悄丢光。
- **`sources.yaml` 里 name/note 一律加双引号。** 旧的 daily-insight 名单里
  `note: Founder - Altimeter, Invest America` 没加引号,YAML 遇逗号截断,
  后半截被静默吃掉,几个月没人发现。
- **stats 要区分 `count:0, ok:true`(今天真没人发)与 `ok:false`(接口挂了)。**
  缺了这个字段页面就分不清「没有」和「没抓到」。
- **36h 窗口 + 按天分片** ⇒ 同一条帖子会出现在相邻两天的分片里,页面必须按
  `post.id` 跨天去重。

## 缺口

- **LinkedIn**:TikHub 有端点,但 2026-09-13 实测全线 400(响应明示不扣费),
  证据在 `tests/fixtures/linkedin_400_blocked.json`。上游修好后照 `xueqiu.py`
  的结构补一个适配器即可。
- **小红书 / 公众号名单目前是占位数据**,见 `sources.yaml` 里的 ⚠️ 注释。
