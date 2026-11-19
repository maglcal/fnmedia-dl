# RSS 订阅支持 · 研究与设计草案（feat/rss-subscribe）

> 状态：研究定稿（2026-11-19），未实施。结论先行：**可行且成本可控**——
> 派发链（通道降级/配额熔断/历史查重/磁力真名）、持久化调度（taskqueue）
> 与链接解析（links）三块积木全部可复用，新增的只有「订阅源拉取 + 条目
> 解析 + 过滤去重」一层薄壳。

## 1. 目标形态

「RSS 订阅」= 订阅若干 RSS/Atom 发布源（典型：BT 站点的最新发布页），
按间隔轮询，新条目经**过滤规则**与**去重**后自动派发为下载任务（也可
人工确认后手动派发）。对齐下载工具通用预期（qBittorrent RSS 阅读器 /
下载规则、flexget、at-rss 一类），但入口收敛到现有管理页任务流里。

## 2. 外部调研结论（协议与成熟范式）

### 2.1 条目格式：BEP 36 是事实标准

BT 官方 BEP 36「Torrent RSS feeds」规定（优先级从高到低）：

1. **enclosure**：`<enclosure type="application/x-bittorrent" url="…"/>`
   ——MUST，匹配此规格的 enclosure **优先于其他一切标签**；
2. **title** = 种子名；**guid** 唯一标识，**SHOULD 直接用 info-hash**
   （跨订阅源去重的关键）；**ttl** = 客户端下次刷新间隔（秒）；
3. 扩展标签（EZTV/nyaa 风格，兼容解析）：
   - `<link>` 直接放 `magnet:?xt=urn:btih:…`（磁力订阅页常见）；
   - `<torrent><infoHash>…</infoHash><contentLength>…</contentLength></torrent>`
     ——infoHash 可直接 `links.magnet_from_infohash()` 组磁力，无需拉种子；
   - `<media:content url fileSize>` / `<media:hash>`（nyaa: 命名空间同款）；
   - `<description>` 里夹磁力/直链（兜底正则提取，at-rss 的 "trick mode"）。

Atom 支持面窄，**只做 RSS 2.0 优先、Atom 容错**即可。

### 2.2 过滤与去重：qBittorrent 模式

- **Must Contain / Must Not Contain**：关键词或正则（`(?i)` 忽略大小写；
  排除用负向断言 `^(?!.*CAM|TS).*`）。关键词模式对普通用户够用，正则做
  进阶开关。
- **剧集过滤**：`38x03-` 区间语法 / `S\d{2}E\d{2}` 正则；**Smart Episode
  Filter** 提取集数后跨条目去重，同集只下一次（PROPER/REPACK 例外）。
- **三轴过滤**：标题（包含/排除）、质量标签（`1080p|2160p`、编码）、
  大小（feed 提供 length/contentLength 时可做 min/max 阈值）。
- **去重三层**（at-rss / qBittorrent 通行做法）：
  ①条目级：guid/info-hash 已处理缓存（持久化，30 天滚动清理）；
  ②资源级：`links.identity()`——磁力按信息哈希归一，与全局下载历史查重
  （`downloader.history_check` 现成）；③派发级：dispatch 幂等 + 失败搁置。

## 3. 内部积木盘点（全部现成）

| 需要的能力 | 现成实现 | 复用方式 |
|---|---|---|
| 单条派发（通道降级/配额熔断/真名回填） | `downloader.dispatch()` | 直接调用 |
| 资源级去重 | `downloader.history_check()` + `links.identity()` | 直接调用 |
| 磁力/直链/种子解析 | `links.parse_line/parse_torrent_bytes/…` | 条目 URL 归一 |
| infohash → 磁力 | `links.magnet_from_infohash()` | torrent:infoHash 标签 |
| 定时调度 + 持久化队列 | `taskqueue.py`（文件锁/单飞/失败搁置/MAX_FAILS） | **仿写**一份 `rssfeed.py`（模型不同：轮询间隔 ≠ 每日定点） |
| 后台 ticker 线程 | `main.py` `queue-ticker` | 旁挂 `rss-ticker` 同款 |
| 配置/状态持久化 | `config.py` / state 目录约定 | 新增 rss.json / 订阅缓存 |

## 4. 设计草案

### 4.1 数据模型

```python
# 订阅（rss.json，文件锁短临界区同 taskqueue 口径）
Subscription = {
    "id": "sub-1", "name": "示例订阅", "url": "https://…/rss",
    "enabled": True, "interval_min": 30,          # 轮询间隔，下限 10 分钟
    "auto_download": False,                        # False=仅入库待确认（默认安全）
    "directory": None,                             # 每订阅保存目录，空=全局默认
    "filter": {
        "must": ["1080p"],                         # 关键词，任一命中（OR）
        "must_not": ["CAM", "TS"],                 # 排除优先
        "regex": False,                            # True 时 must/must_not 按正则编译
        "size_min_mb": None, "size_max_mb": None,  # feed 提供大小时生效
    },
    "etag": "", "last_modified": "",               # 条件 GET
    "last_check": "", "last_error": "", "fail_count": 0,
}

# 条目（seen 缓存，30 天滚动清理；只存指纹不存正文）
Item = {
    "guid": "…",           # 或 infohash 兜底
    "identity": "…",       # links.identity 归一结果
    "title": "…", "url": "…",
    "state": "new|matched|dispatched|skipped|failed",
    "first_seen": "...", "last_result": "…",
}
```

### 4.2 轮询与派发流程

```
rss-ticker（后台线程，60s 醒一次）
  └─ 到点订阅（enabled 且距 last_check ≥ interval_min）
       ├─ GET feed（If-None-Match/If-Modified-Since；超时 15s；体积极限 2MB；
       │           item 数极顶 500；304/失败 → fail_count++，5 次连续失败搁置并报错）
       ├─ 解析条目（BEP 36 优先级链 → links.LinkInfo）
       ├─ 去重：guid/identity ∈ seen → 跳过；history_check 命中 → skipped(重复)
       ├─ 过滤：must_not 一票否决 → must 任一命中（空=全过）→ 大小阈值
       └─ auto_download=True → dispatch()（失败按 taskqueue 口径 fail_count 搁置）
          auto_download=False → state=matched 入库，UI 手动「派发」按钮
```

### 4.3 API / UI（管理页新增「RSS 订阅」区，任务面板旁）

- `GET/POST/PUT/DELETE /api/rss/subs`（CRUD）；
- `POST /api/rss/subs/{id}/check`（立即拉取，返回新条目命中情况）；
- `GET /api/rss/subs/{id}/items`（条目预览：title/大小/命中状态/手动派发）；
- `POST /api/rss/items/{id}/dispatch`（手动派发单条）；
- UI：订阅卡片（启停开关/上次检查/错误提示）+ 条目表（复用任务表样式），
  过滤规则先做「关键词行 + 正则开关」两个输入框，不做规则构建器。

### 4.4 风险与对策

| 风险 | 对策 |
|---|---|
| 恶意/超大 feed | 体积极限 2MB + item 数上限 + 超时；解析异常单源隔离不拖垮轮询 |
| 正则 ReDoS | regex 模式限长（512 字符）+ 用户自填自担；关键词模式为默认 |
| 重复派发 | 三层去重（guid → identity → 派发幂等）；seen 缓存先写后派（宁漏勿重） |
| 条目标题含敏感内容 | UI 只显 title；日志/文案沿用脱敏词表（内容接口/第三方数据源） |
| 隐私 | feed 拉取走服务端（NAS），不经第三方；seen 缓存只存指纹与标题 |

## 5. 实施分期

- **P1 手动闭环**（先验证解析质量）：订阅 CRUD + 「立即检查」+ 条目预览
  + 手动派发 + guid/identity 去重缓存。**不做定时轮询**。
- **P2 自动化**：rss-ticker 定时轮询 + auto_download 开关 + 关键词/正则
  过滤 + 失败搁置与错误提示（对齐 taskqueue 语义）。
- **P3 打磨**：剧集级防重复（Smart-episode 式，同集只下一次）、大小过滤、
  nyaa/EZTV 扩展标签深解析、每订阅目录、条目历史清理策略。

## 6. 开放问题（实施前需拍板）

1. **订阅源形态**：主要订阅哪类源？BT 站发布页（BEP 36/nyaa 风格）为主，
   还是也要支持普通网页/播客 enclosure（HTTP 直链）？——影响解析优先级
   与 P1 验收样本。
2. **自动下载默认值**：建议默认关（入库待确认），确认无误再开自动——
   同意吗？
3. **过滤层级**：关键词 + 正则开关够吗？剧集智能去重（P3）优先级如何？
4. **下载目录**：全局默认目录即可，还是每订阅必须指定（分类存放）？

## 7. 参考

- BEP 36 Torrent RSS feeds：https://www.bittorrent.org/beps/bep_0036.html
- qBittorrent RSS 下载规则 / Smart Episode Filter（正则三轴过滤范式）
- at-rss（aria2/Transmission RSS 监视器）：include/exclude 关键词组、
  trick-mode 磁力提取、processed-item 持久缓存
- nyaa 风格 `?page=rss` 扩展（nyaa:infoHash/media:hash 等）
