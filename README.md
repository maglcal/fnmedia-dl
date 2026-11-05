# fnmedia-dl · 飞牛下载助手

NAS 多通道下载管理工具（fnOS 第三方应用）。批量解析磁力 / thunder / 直链 / 种子链接，
统一任务面板管理下载进度，三通道下载引擎自动选择与失败降级：

| 通道 | 定位 | 特点 |
| --- | --- | --- |
| **迅雷远程通道** | 主通道 | 任务下发到账号绑定的远程设备执行，走 P2SP 加速；官方口径远程下载不占套件端每日任务配额 |
| **迅雷下载引擎** | 备选 | 直连 NAS 上的迅雷引擎面板；走套件端入口，任务数可能受每日配额限制 |
| **aria2** | 兜底 | 通用下载，无任务数限制，速度取决于 BT 网络 |

> **免责声明**：本工具仅供学习交流与个人合法下载自有或已获授权内容使用。
> 工具本身不提供、不存储、不分发任何内容资源；下载内容来自你自行提供的链接与
> 第三方网络，版权归原权利人所有。请遵守所在地法律法规，勿用于商业用途。

## 功能

- **链接解析**：magnet（hex/base32 信息哈希、dn、tracker）、thunder://（AA/ZZ 套壳解码）、
  http/ftp 直链、.torrent URL 与本地种子文件（bencode 解析 info 哈希，归一化为 magnet）；
  ed2k 解析结构供展示、暂不派发
- **批量派发**：一次粘贴多条链接混排，逐条解析、逐条派发，结果逐条回报
- **自动降级**：auto 模式按「迅雷远程 → 迅雷引擎 → aria2」顺序尝试，通道不可用或创建
  失败自动落下一个通道
- **统一任务面板**：各通道任务合并展示（进度/速度/状态），支持暂停/继续/删除，任务记录持久化
- **配置管理**：通道优先级、各引擎连接参数与默认保存目录，管理页内测试连通性；
  迅雷凭据仅保存在本机 `state/config.json`（chmod 600），不上传任何服务器

## 目录结构

```
fnmedia-dl/
├── app/
│   ├── serve.py            # 服务入口：uvicorn(UDS) + 桌面网关线程
│   ├── webui_gateway.py    # 飞牛桌面 iframe 网关（/app/fnmedia-dl → app.sock）
│   ├── main.py             # FastAPI：管理页 + JSON API
│   ├── links.py            # 下载链接解析
│   ├── downloader.py       # 派发调度（降级链 / 统一任务视图 / 记录）
│   ├── config.py           # 配置存取（state/config.json）
│   ├── backends/           # 三通道适配器
│   │   ├── drive_core.py   # 迅雷 drive/v1 共用客户端（引擎/云端同构）
│   │   ├── thunder_cloud.py# 迅雷云端远程（主）
│   │   ├── thunder_engine.py # 迅雷下载引擎（备）
│   │   └── aria2.py        # aria2 JSON-RPC（兜底）
│   └── ui/                 # 管理页（原生 HTML/CSS/JS）
├── deploy/                 # systemd 安装/卸载/依赖引导
├── packaging/fpk/          # FPK 打包（manifest/cmd/wizard/build.sh）
├── wheelhouse/             # 离线依赖 wheel（安装免联网）
└── tests/                  # 单元测试（stdlib unittest + httpx MockTransport）
```

## 安装

### 方式一：FPK 应用包（推荐）

```bash
# 在 Linux（fnOS 或构建机）上打包
bash packaging/fpk/build.sh          # 产出 dist/fnmedia-dl-<版本>.fpk
```

把 `.fpk` 上传到 fnOS 应用中心安装；安装向导需勾选同意免责声明。
桌面/应用中心会出现「飞牛下载助手」入口（iframe 管理页）。

### 方式二：手动部署

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 或 bash deploy/bootstrap_deps.sh
sudo bash deploy/install.sh                 # 渲染并启动 systemd 服务
# 卸载：sudo bash deploy/uninstall.sh [--purge]
```

### 开发模式（任意平台）

```bash
cd app && python serve.py --http 127.0.0.1:8787
# 打开 http://127.0.0.1:8787
```

## 通道配置

管理页「通道设置」里配置，要点：

1. **迅雷远程通道**：填迅雷账号（手机号）与密码。首次使用会走登录链（v3 登录 →
   人机令牌 → 换取访问令牌），令牌自动续期并持久化。若风控要求人工验证，会给出
   验证地址，浏览器完成验证后重试。`target` 留空时自动发现账号下第一台远程设备
   （需先在迅雷 App 中绑定远程下载设备）。
2. **迅雷下载引擎**：填引擎 web 面板地址（如 `http://127.0.0.1:2345`，Docker/套件版
   按实际端口）。注意此通道走套件端入口，任务数可能受每日配额限制。
3. **aria2**：需 aria2 开启 RPC（`--enable-rpc --rpc-listen-port=6800`），有 RPC 密钥则一并填写。

## 迅雷接口说明（重要）

迅雷相关通道基于其 Web 端/客户端在用的接口实现（与公开协议分析一致），属于**非官方协议**，可能随官方改版失效或改变行为。架构上做了
隔离：任一迅雷通道失效时自动降级到 aria2，不影响基本可用性。ed2k 协议需要
amule 等重型组件，暂不支持派发。

## 测试

```bash
python -m unittest discover -s tests -v    # 45 个用例，全程无网络
```

覆盖：链接解析（magnet/thunder/ed2k/种子）、drive/v1 请求形状、云端登录链与
签名链、令牌自动续期、aria2 JSON-RPC、派发降级、统一任务视图、API 层。

## License

MIT
