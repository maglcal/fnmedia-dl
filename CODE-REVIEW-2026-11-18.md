# fnmedia-dl 全面代码审查报告（2026-11-18）

- **审查范围**：全仓约 1.87 万行——app 核心（main/downloader/taskqueue/updater/updatesrc/config/quota/links/filelock/gwtoken/serve/stats/webui_gateway/xunlei_docker）、backends/ 四通道、前端 app.js+index.html、发布链（build.sh/install.sh/bootstrap_deps.sh）。
- **方法**：安全与逻辑核心逐行亲读（13 个模块）；backends、前端、运维脚本三路并行专审后**逐条回读核验**再入报告（捏造/不实项已剔除并记录，见文末）。
- **基线**：`python -m unittest discover -s tests` → **537 通过 / 3 跳过**（本轮复跑 14.5s 全绿）。

分级：🔴 应尽快修｜🟠 建议近期修｜🟡 低优先级/防御性。

---

## 一、🔴 高优先级

### R1. 更新信息未转义进 innerHTML（持久 XSS sink）
- **位置**：`app/ui/app.js:1107-1108`、`1120`
- **问题**：`发现新版本 <b>v${r.latest}</b>（当前 v${r.current}）` 与 `✓ 已是最新版本（v${r.current}）` 直接拼入 `innerHTML`，未过 `esc()`。同函数 1114 行 `v${esc(r.latest)}` 有转义——不一致即漏网实证。
- **影响**：`r.latest` 来自远程清单（Gitee/GitHub 发行 tag、repobox manifest version/tag）。恶意镜像源或被投毒的发行标签可在管理页注入持久脚本（更新对话框每次「检查更新」重现）。
- **改法**：`v${esc(r.latest)}` / `v${esc(r.current)}`；顺带全局扫一遍模板串插值补齐 `esc()`。

### R2. 错误信息裸插 title 属性（属性注入 XSS sink）
- **位置**：`app/ui/app.js:115`
- **问题**：`title="${e.message}"` 未转义。`e.message` 可内嵌服务端响应片段（V8 的 JSON.parse 报错会带输入原文片段；main.py 全局异常处理器把 `exc` 原文回显进 detail，见 Y2）。
- **影响**：含引号的错误片段可破出属性上下文注入事件处理器；与 Y2 形成组合链。
- **改法**：`title="${esc(e.message)}"`，或改 `textContent`+`setAttribute`。

### R3. aria2 删除任务不清理结果记录（列表/内存膨胀）
- **位置**：`app/backends/aria2.py:124-128`
- **问题**：`delete()` 只调 `aria2.remove` / 失败转 `aria2.forceRemove`，不跟 `aria2.removeDownloadResult`。被删任务永久留在 aria2 的 stopped 结果列表（受 keep-download-result 上限约束，默认 5 条/保留下载结果开关开启时无限累积）。
- **影响**：长期运行后 `tellStopped` 结果堆积、统一任务视图反复合并/跳过已删项；「删除」语义与用户预期不符。
- **改法**：`remove`/`forceRemove` 成功后补调 `aria2.removeDownloadResult(task_id)`（幂等、失败忽略）。

---

## 二、🟠 中优先级

### O1. 种子上传先读全 body 再查 10MB，且无应用层全局请求体上限
- **位置**：`app/main.py:763-765`（`parse_torrent_upload`）；全应用无 ASGI 级 body 限制
- **问题**：`data = await request.body()` 一次性缓冲后才比对 `_TORRENT_MAX`。生产有 webui_gateway `MAX_BODY=64MB` 兜底，但 `--http` 开发通道无兜底；即便有 64MB×并发仍可造成内存压力。
- **改法**：`request.stream()` 增量读、超 10MB 立即 413 断流；或加 ASGI 中间件统一限 body（如 8MB，种子端点单独 10MB）。

### O2. 更新源全挂时，手动地址更新被连坐挡死
- **位置**：`app/main.py:361`（`update_download` 开头无条件 `info = update_check()`）；对照 `main.py:168-175`（`update_check` 失败抛 502）
- **问题**：手动粘贴下载地址（369-372 行的 manual 分支）根本不需要清单，但入口先跑多源检查；全部更新源不可用时用户拿到 502，明明手里有可用 fpk 地址却无法更新。
- **改法**：`req.url` 非空时 try/except 掉 update_check 的 502，直接走手动 asset 分支（保留地址形状校验与 `allow_unverified=manual` 语义）。

### O3. 队列派发持锁做网络调用，慢派发阻塞全部队列 API
- **位置**：`app/taskqueue.py:120-143`（`run_now` 在 `_FILE_LOCK` 内逐项调 `dispatch_fn`）
- **问题**：`dispatch_fn` 是同步网络派发（迅雷登录+建任务可达数十秒），期间 `snapshot/add/remove/settings/tick` 全部排队在同一把 threading 锁上；HTTP 请求线程被拖住直至整批派发完。
- **改法**：锁内只做「快照待发项+标记派发中」，网络派发放锁外逐项执行，结果回写再短锁入盘。

### O4. stats 上报线程 shutdown/kick 竞态 + 退避期不可唤醒
- **位置**：`app/stats.py:460-483`（`_ensure_worker`/`shutdown`）、`512`（`time.sleep`）
- **问题**：① 旧线程在长退避 `time.sleep` 中时 `shutdown()` join(5s) 超时后置 `_worker=None`，随后 `kick()` 清 `_stop` 新建线程——旧线程睡醒后 `_stop` 已被清空，**双线程并发 flush**（重复上报、SQLite 并发写）；② `time.sleep` 不响应 `_wake`，`on_config_changed` 承诺的「endpoint 变化立即尝试」在退避期失效（最长 6h）。
- **改法**：等待改 `_wake.wait(timeout)`（可被 shutdown/kick 唤醒）；线程身份用 generation/独立 Event 区分新旧，`shutdown` 置终态不复用。

### O5. thunder_cloud 状态提示与实际改道行为矛盾
- **位置**：`app/backends/thunder_cloud.py:1013-1015`（status 提示「任务将改发到 {online[0]}」）vs `965-984`（`_target_of` 明确「配置值优先、不擅自改道」，0.1.28 语义）
- **问题**：配置的目标设备离线时，status 文案说会自动改发到首个在线设备，实际派发仍发往配置值 → 派发失败。文案误导用户排障方向。
- **改法**：改文案为「目标设备已离线，派发将失败，请更新目标设备」（或实现改道——但与 0.1.28 修复意图相悖，不建议）。

### O6. drive_core 任务进度启发式换算可能错判 0-1 比率
- **位置**：`app/backends/drive_core.py:801-808`
- **问题**：`pct = pct if pct <= 100 else pct / 100.0` 无法区分 0-100 百分比与 0-1 比率：上游返回 0.5（50%）会被显示为 0.5%。千分比等异常值也只靠猜测。
- **改法**：先探明 drive/v1 `progress` 字段的真实语义并按文档定死换算；确需兼容多口径时按 `phase/status_size/file_size` 优先自算，progress 仅作兜底。

### O7. aria2 畸形响应时异常逃逸出 BackendError 体系
- **位置**：`app/backends/aria2.py:50`（`data = resp.json()` 裸调用）
- **问题**：`status()` 捕了 `ValueError`，但 `add()/list_tasks()` 只捕 `BackendError`——aria2 返回非 JSON（代理劫持/网关错误页）时 `JSONDecodeError` 逃逸 → 全局处理器 500，而非走 auto 降级链。
- **改法**：`_rpc` 内 try/except ValueError 转 `BackendError("aria2 响应不是合法 JSON")`。

---

## 三、🟡 低优先级 / 防御性

### 鉴权与配置
- **Y1** `main.py:548-553`（config_put）：`incoming.get("thunder_cloud", {})`/`aria2` 未 isinstance 防护，传字符串 → AttributeError 500 而非 400。→ 非 dict 一律 400。
- **Y2** `main.py:947` 全局异常处理器 `f"内部错误：{exc}"` 把异常原文回显客户端（路径/内部细节泄漏，且与 R2 组成注入链）。→ 统一「内部错误，请查看服务日志」，traceback 只进 stderr。
- **Y3** `main.py:940-941` 队列后台线程 `except Exception: pass` 静默吞错，持久故障（配置损坏/磁盘满）无任何感知。→ 至少 stderr 记一行。
- **Y4** `config.py:135-141` 配置 JSON 损坏回退默认后，下一次 save 会覆盖损坏现场，无法取证恢复。→ 回退前把原文件改名 `config.json.broken` 保留。
- **Y5** `config.py:177-189` redacted 未遮罩 `thunder_cloud.creditkey`（风控信任密钥）与 `device_id`。→ 加入遮罩清单。
- **Y6** `main._crumbs` 未 realpath 归一，配置根含软链时面包屑错位（仅外观）。

### 更新链
- **Y7** `updater.py:427-436` `start()` 线程守卫 TOCTOU：`_thread` 赋值与 `start()` 在锁外，两并发请求可双双通过 `is_alive()` 检查各起一个更新线程。→ 线程创建+赋值+start 全部进锁。
- **Y8** `updater.py:110-116` `validate_asset_url` 未显式限制 scheme（依赖白名单前缀自带 https）。若配置注入非 http(s) 的 base_url 会放出宽松前缀。→ urlparse 显式断言 scheme∈{https}。
- **Y9** `updatesrc.py:172/205/244` `int(a.get("size") or 0)` 遇非数字 size 抛 ValueError 逃逸 SourceError 体系 → 500。→ safe_int 包装。
- **Y10** `updatesrc.py:189-197` GitHub fetcher 把 404 一律当「暂无发布版本」——仓库被删/改名（404）会被误报「无更新」；Gitee 版按「源不可用」处理是对的。→ 区分 repo-not-found 与 no-release。
- **Y11** `updatesrc.py:140-143` `.sha256` 配套文本未校验形状，HTML 错误页首词会被当哈希（fail-closed 兜底只浪费一次下载，轻微）。

### 队列 / 锁 / 记录
- **Y12** `taskqueue.py` 全模块只用 threading.Lock，queue.json 读改写无 filelock 跨进程互斥——与 config/quota/tasks.json 的 P0-2 加固不一致，升级双进程窗口互覆盖。→ 套 `filelock.locked`。
- **Y13** `taskqueue.py:122-123` 失败项无重试上限（failed 留在 PENDING 每日重派），永久坏链每天重试。→ 加 fail_count 上限或「连续 N 次失败自动搁置」。
- **Y14** `filelock.py:54-69` `_acquire` 对永久性 OSError（EBADF/ENOMEM 等）也重试满 10s 才报 TimeoutError，掩盖真因。→ 仅对锁冲突类错误（BlockingIOError / Windows EACCES）重试，其余立即抛。
- **Y15** `downloader.py:73-78` tasks.json 写盘未 chmod 600（URL 可能内嵌凭据），同目录 config.json 是 600。→ tmp 写后 `os.chmod(tmp, 0o600)` 对齐。

### 链接解析
- **Y16** `links.py:191` `torrent_infohash` 用 `data.index(b"4:info")` 重新定位 info 字节区间——种子里任何字符串值含 `4:info`（如文件名）都会定位错 → infohash 算错 → 磁力指向错误资源。→ `_bdecode` 返回值时记录字节区间，或从顶层 dict 解析位置直接切片。
- **Y17** `links.py:153-181` `_bdecode` 递归无深度上限，恶意深嵌套种子触发 RecursionError（不在捕获列表）→ 500。→ 深度上限（如 100）或捕 RecursionError 返回 None（报「种子损坏」）。

### 网关 / 统计
- **Y18** `webui_gateway.py:138-149` `_rewrite_request` 对恰 2 段的畸形请求行（`GET /x` 无版本）用 `parts[2]` → IndexError 线程裸崩，客户端收不到响应。→ `len(parts) >= 3` 才重写。
- **Y19** `stats.py:389-398` `_post` 走 urllib 会吃系统代理环境变量，与 downloader 的 `trust_env=False`（NAS 残留代理变量绕境外触发风控）策略不一致。→ `build_opener(ProxyHandler({}))` 直连。
- **Y20** `stats.py:217-227` outbox `ts` 带时区偏移做字符串比较，系统时区变更后新旧事件排序偏差。→ 统一存 UTC 或 epoch。

### 前端
- **Y21** `app.js:823-824` 「迅雷重新登录」靠 `setTimeout 300ms` 赌保存完成，慢保存时测试用旧配置。→ 直接 `await saveConfig()`。
- **Y22** `app.js:280/282` `t.state` 裸插 class 属性（`class="state ${t.state}"`）。状态目前来自服务端封闭集合（TaskView 映射），属防御性转义建议。

### backends
- **Y23** `aria2.py:71-78` `add()` 的 `name` 参数实质未用（仅决定 bt-save-metadata），前端「重命名后下载」对 aria2 无效。→ 用 aria2 `out` 选项实现或接口注释明示。
- **Y24** `drive_core.py:809/818` `(t.get("params") or {}).get("url", "")` 当 params 为非 dict（列表/字符串）时 AttributeError 逃逸。→ isinstance 防护。
- **Y25** `thunder_cloud.py:34` `XLUSER_API` 等官方域名硬编码，改域名/走代理需改源码（nit）。

### 发布链 / 运维脚本
- **Y26** `deploy/bootstrap_deps.sh:121/125/127` `log "…$idx"` 把 pip 源 URL 原样落 `/tmp/fnmedia-dl.log` 与 trace 日志——用户经 `FNDL_PIP_INDEX` 配内联凭据源（`https://user:pass@host`）时凭据落盘可被同机读取。→ log 前剥掉 URL userinfo 段。
- **Y27** `deploy/install.sh:33` `sed "s#@ROOT@#$ROOT#g"` 渲染 systemd 单元——安装路径含 `#`/`&`/反斜杠时表达式断裂或注入。→ envsubst / python 渲染。
- **Y28** `packaging/fpk/build.sh:22` `rm -rf "${STAGE}"` 配 `--stage-only DIR` 可删任意目录（dev 工具误用风险）。→ 限定 stage 路径前缀或加显式确认。
- **Y29** `build.sh:71` `cmd/` 目录文件跳过 UTF-8 判定直接做换行改写——二进制误入 cmd/ 会被字节级破坏（潜伏坑，当前 cmd/ 均为 shell 无实际触发）。→ cmd/ 同样先过 UTF-8 判定。
- **Y30** `build.sh:158-163` fnpack 自动下载（官方 CDN HTTPS）无 SHA256 校验即 chmod +x 用于出包（供应链信任）。→ 钉死哈希校验。
- **Y31** `build.sh:110` changelog 标题正则不支持 `## [0.2.0] - …`（keep-a-changelog）与含冒号版本号——自家格式目前命中，改格式时会静默回退「首段当变更」。→ 正则放宽并断言提取命中。

---

## 四、已检查未见问题的重点（结论）

| 模块 | 已检查 | 结论 |
|---|---|---|
| gwtoken.py | O_EXCL 并发创建收敛、hmac 常量时间比对、fail-closed | 未见问题 |
| serve.py | UDS 权限收紧、`--http` 仅回环自动信任、socket 0600 | 未见问题 |
| main.py 鉴权面 | `_admin_guard` 写操作 fail-closed、读写分流、401 门 | 未见问题 |
| updater.py 更新链 | 逐跳重定向≤5 跳+主机白名单、512MB 上限、SHA256 fail-closed、`_swap_in` 原子顶替、失败回滚、`_mask_secrets` | 加固到位（仅 Y7/Y8） |
| downloader.py | 降级链派发、配额感知换通道、统一视图跨通道去重、DISPATCH_BUDGET、records 读改写文件锁原子区间 | 未见问题 |
| webui_gateway.py | 单请求单连接契约、X-Fndl-Auth 顶替防护、hop-by-hop 剥离、Expect:100 处理、chunked 解析 MAX_BODY 上限、超时显式 JSON 错误 | 基本到位（仅 Y18） |
| stats.py | 双闸（采集/上报）、machine_hash 组包现算不落盘、字段白名单、outbox 限额/退避/400-413 丢批语义、shutdown 停线程根治 WinError 32 | 基本到位（仅 O4/Y19/Y20） |
| quota.py | 跨天自动重置、filelock 原子区间、tmp+replace | 未见问题 |
| config.py | 读改写整段锁、ConfigError fail-loud（0.1.41.2 教训内化）、原子替换+0600、sources_v 迁移幂等 | 基本到位（仅 Y4/Y5） |
| links.py | thunder 套壳解码、magnet 校验、split_links 协议感知防误拆、identity 归一查重 | 基本到位（仅 Y16/Y17） |
| taskqueue.py | tick 先跑后记 last_run、should_run 边界（23:59/次日） | 基本到位（仅 O3/Y12/Y13） |
| 前端转义面 | 任务表格/提示/解析结果普遍走 `esc()`、text 节点用 textContent | 总体到位，2 处漏网（R1/R2） |
| 测试 | 全量红线复跑 | 537/3 全绿 |

---

## 五、审查过程说明（agent 发现的核验与剔除）

三路并行专审的报告均经逐条回读核验，**属实项**已收录上文并改由我方亲验行号；**不实项**剔除如下（避免误报入账）：

- 前端专审：18 项中 13 项不实/无法证实——`xdInput` 过滤竞态、`flashEl` aria-live、`input type="url"`、`linkInput autofocus`、`btnResumeAll` 双击、`torrentTaskName` 注入、heartbeat O(N×M)、`raw.taskName` 未转义（实际 275 行已 `esc()`）、queued 状态缺 STATE_TEXT（服务端状态集无 queued）、history 遮蔽提示（实际用 textContent）等，相关标识符/行号在代码中不存在。
- backends 专审：12 项中 4 项不实——`drive_core.download_file` 无上限下载（函数不存在）、`_api_l3` URL 兜底 task_id（不存在）、thunder_engine status「import socket 手工 HTTP」（实际走 core 登录）、map_engine_dir「路径逃逸」（文档语义为 mounts 缺失时不校验，未证实非法路径回退）。
- 运维专审：实质成立但行号漂移（bootstrap_deps.sh 路径误写 packaging/fpk/、行号 71/102/111 → 实际 121/125/127），已修正引用后收录（Y26）。

**建议修复顺序**：R1→R2（XSS 两处，改动极小）→ R3 → O1/O2（用户体验直击）→ O4（并发正确性）→ O3 → 其余按版本节奏。

---

## 六、修复记录（2026-11-18，「全部修复」指令）

全部 35 项（R1-R3、O1-O7、Y1-Y31）已修复完毕，分批落改、每批过测试红线，
最终全量 **548 通过 / 3 跳过**（较修复前 537/3 新增 11 条钉住测试）。

| 批次 | 范围 | 红线 |
|---|---|---|
| 1 | 前端 app.js：R2 toast 转义、R1 更新文案插值转义、Y21 saveConfig 直调、Y22 状态字典安全查表 | 537/3 ✅ |
| 2 | main.py：O1 种子流式限长 413、O2 手动更新免清单连坐、Y1 配置段 isinstance 400、Y2 顶层异常 traceback+中性报错、Y3 定时派发异常收口 | 537/3 ✅ |
| 3 | backends：R3 delete 补 removeDownloadResult、O7 _rpc JSON/形状异常并入 BackendError、Y23 直链 out 选项、O5 status 文案改「派发将失败」与 _target_of 一致、Y25 XLUSER_BASE/DRIVE_API env 覆盖、O6 进度 size 优先 progress 兜底、Y24 params isinstance（同款横扫 +2 处） | 537/3 ✅ |
| 4 | taskqueue：O3 双短锁+派发单飞（撞车 409）、Y12 套 filelock.locked、Y13 fail_count≥5 自动搁置 suspended（手动派发可捞回）+ 前端徽标 | 541/3 ✅（+4 测试） |
| 5 | 核心小件：Y4 损坏配置备份 config.json.broken、Y5 redacted 遮罩 creditkey/device_id、Y6 面包屑边界+realpath 兜底、Y7 线程 TOCTOU 全锁内、Y8 scheme 显式断言、Y9 safe_int、Y10 GitHub 404 区分仓库缺失/无 release、Y11 sha256 形状校验、Y14 锁冲突才重试、Y15 tasks.json 0600、Y16 infohash 按解析区间切片、Y17 bdecode 深度上限、Y18 请求行 3 段守卫 | 548/3 ✅（+6 测试，含 Y10 语义修正 1 例） |
| 6 | stats：O4 每代独立终止事件+_wake.wait 可唤醒、Y19 ProxyHandler({}) 直连、Y20 时间戳统一 UTC（2 处断言随语义更新） | 548/3 ✅ |
| 7 | 发布链：Y26 日志/pip 输出脱敏 userinfo、Y27 sed 替换串转义、Y28 stage 限定 .build/ 前缀、Y29 cmd/ 同过 UTF-8 判定、Y30 fnpack SHA256 钉死、Y31 changelog 正则放宽+提取断言 | 548/3 ✅（脚本 bash -n + LF 字节级验收过） |

**修复口径备注**（与报告建议的有意偏差）：
- **Y8**：scheme 断言取 {https, http}——LAN 自建镜像 http 前缀是合法场景，堵的是 file:/ftp: 等非 http(s) 协议。
- **Y26**：不止脱敏脚本自身 log，pip 输出（会回显 index URL）同步过 sed 脱敏；`--trusted-host` 提取剥 userinfo。pipefail 保持退出语义。
- **Y30**：amd64 产物钉死 `54b97fa7b70968c4d05c79840f5daeff508957d0bb2062fdb0376d00d9615c93`（下载自官方 CDN 并核 ELF）；arm64 上游无产物（原自动下载路径本就 404），未钉死 → fail-closed 拒绝自动下载、要求 --fnpack 显式指定。缓存产物复用前同样验哈希。
- **Y13**：落地为「连续 5 次失败自动搁置」方案；搁置项仅手动「立即派发」重试（retry_suspended），定时派发不再每日重试。
- **Y10**：原 `test_github_404_empty_manifest` 钉住的正是错误语义，拆为「无 release 空清单」与「仓库缺失报错」两用例。
- **Y31**：断言取「CHANGELOG 非空但零版本段解析出来 → fail-loud」；「当前版本未命中沿用首段」保留为正常兜底。
