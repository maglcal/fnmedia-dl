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
- **在线更新**：管理页「关于」里一键更新到最新发布版（下载 fpk → 备份 → 替换 → 重启），
  更新过程显示进度；替换只动程序文件（app/deploy 等），`state/` 配置与任务记录
  永不触碰，失败自动回滚

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
bash packaging/fpk/build.sh          # 产出 dist/fnmedia-dl-main-v<版本>.fpk
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

## 更新源配置（国内镜像加速）

GitHub Releases 在国内访问慢/超时，插件支持**多更新源**：默认优先走自建
RepoBox 镜像（国内直链），失败自动回退 GitHub 官方源。未配置镜像时行为与
旧版完全一致（仅官方源）。

### 切换 / 配置更新源

在 `state/config.json` 的 `update.sources` 数组里配置，**按数组顺序依次尝试**，
第一个可用的源为准：

```json
"update": {
  "sources": [
    {"name": "repobox", "type": "repobox",
     "base_url": "https://repo.example.com",
     "repo": "fnmedia-dl", "token": "rbx_xxx"},
    {"name": "github", "type": "github", "repo": "maglcal/fnmedia-dl"}
  ],
  "timeout": 15,
  "retries": 2
}
```

- `type=repobox`：自建 RepoBox 镜像。`base_url` 留空自动跳过（即只用官方源）；
  `token` 用于读清单（raw 直链公开可读时可留空）。
- `type=github`：GitHub 官方 Releases 兜底。
- 想只用官方源：把 repobox 条目的 `base_url` 置空，或直接删掉该条目。
- 想把镜像换成别的地址：改 `base_url` 即可（需按 `latest.json` 清单协议提供）。
- `timeout`（单请求超时秒）/`retries`（单源请求重试次数）按需调整。

### 回退与重试策略

- **检查更新**：逐源按序尝试，每源内部重试 `retries` 次（间隔递增）；单源失败
  自动换下一个源，全部失败才报错（错误里含逐源原因）。
- **下载安装包**：下载候选按「镜像源 → 官方源」排序，每个候选重试 2 次
  （间隔递增）；候选内失败或**大小/SHA256 校验不符**自动换下一个候选，
  全部失败才报错（含逐候选原因）。
- **安装失败**：替换前整份备份到 `repo/.update-backup/`，任一步失败自动回滚
  到更新前版本，不会把系统留在半更新状态。

### 镜像自动同步（GitHub → RepoBox）

仓库自带 `.github/workflows/mirror-repobox.yml`：release 发布时立即同步，
每 6 小时兜底校验（幂等，已同步自动跳过），也可手动触发。配置 3 个
Repository Secrets 即可启用：

| Secret | 说明 |
|---|---|
| `REPOBOX_URL` | RepoBox 站点地址（如 `https://repo.example.com`） |
| `REPOBOX_TOKEN` | API 令牌 `rbx_xxx`（RepoBox 网页「令牌」页创建） |
| `REPOBOX_REPO` | 镜像仓库名（可省，默认 `fnmedia-dl`，不存在自动创建） |

同步内容：发行资产（`.fpk`/`.sha256`）按 `releases/{tag}/` 布局上传（逐文件
回读校验），并生成 `latest.json` 清单（version/tag/资产大小/SHA256），供插件
多源检查消费。手动同步：

```bash
REPOBOX_URL=... REPOBOX_TOKEN=... python3 tools/sync_repobox.py [--tag main-vX.Y.Z] [--force]
```

### 发布命名规则（main-vX.Y.Z）

从 `main-v0.1.35` 起，发布标识统一 **`main-vX.Y.Z`**（`main` 之后跟版本号，
如 `main-v0.1.35`）：

- **git tag / GitHub Release 标签**：`main-vX.Y.Z`；
- **fpk 资产文件名**：`fnmedia-dl-main-vX.Y.Z.fpk`（配套 `.fpk.sha256`）；
- **版本号本体**（VERSION 文件、manifest version 字段、更新比较）：`vX.Y.Z` /
  纯 `X.Y.Z`，**不带 `main-` 前缀**——fnOS 升级比较与插件内版本比较只认数字三段。

解析端（`app/updatesrc.py` 的 `strip_ver`，updater/同步脚本共用同一规则）剥
`main-v`/`v` 前缀取纯版本，旧格式 `v0.1.x` tag 兼容可解析；历史 tag 为旧命名，
不再新增。

版本号**延续主线往后递增**（不独立编号、不重置、不跳号）：小修小补递增第三位
（版本号 `v0.1.35` → `v0.1.36` → ……，发布标识对应 `main-v0.1.36`），
大内容更新才升第二位（`v0.2.0`），首个正式大版 `v1.0.0`。

## 通道配置

管理页「通道设置」里配置，要点：

1. **迅雷远程通道**：填迅雷账号（手机号）与密码。首次使用会走登录链（v3 登录 →
   人机令牌 → 换取访问令牌），令牌自动续期并持久化。若风控要求人工验证，会给出
   验证地址，浏览器完成验证后重试。`target` 留空时自动发现账号下第一台远程设备
   （需先在迅雷 App 中绑定远程下载设备）。
2. **迅雷下载引擎**：填引擎 web 面板地址（如 `http://127.0.0.1:2345`，Docker/套件版
   按实际端口）。没装 Docker 版迅雷也不用愁——安装向导可选「帮我自动部署」，
   或在管理页「迅雷下载引擎」卡片点「一键部署 Docker 版迅雷」，自动完成
   拉镜像、起容器、配面板地址（详见下一节）。注意此通道走套件端入口，
   任务数可能受每日配额限制。
   **支持范围**：仅「群晖套件系」面板（cnk3x/xunlei 的 Docker 版、群晖迅雷套件等）。
   **应用中心官方原生迅雷（"飞牛社区版"）不支持本地直连**——它没有这套面板接口；
   如需配合官方原生迅雷，请用**迅雷远程通道**（原生迅雷会把 NAS 注册为你迅雷账号的
   远程设备，远程通道可直接向它派发任务）。
3. **aria2**：需 aria2 开启 RPC（`--enable-rpc --rpc-listen-port=6800`），有 RPC 密钥则一并填写。

## 迅雷下载引擎（Docker 版）安装

### 一键部署（推荐）

不需要手动写 compose：安装向导选「没装，请帮我自动部署」，或在管理页
「迅雷下载引擎」卡片点 **「🚀 一键部署 Docker 版迅雷」**。插件会自动
检测 Docker → 拉取 `cnk3x/xunlei` 镜像 → 启动容器 → 配好面板地址。
部署完成后打开面板 `http://NAS-IP:2345` 登录迅雷账号即可。首次登录后面板会
弹出**内测邀请码**输入框，填 `迅雷牛通`（注意是「通」字，官方内测码）激活；
若提示码已失效，可到[官方申请页](https://docs.qq.com/form/page/DQUJXZUdtRlNGWkxr#/fill)
重新领取。

**存放位置可选**：部署前可为「Docker 版配置文件存放位置」（账号/配置持久化）
和「下载文件存放位置」各自选择**用默认还是自己设置**（安装向导和管理页都有
此选项）。默认分别放在应用数据共享目录与应用下载共享目录（重装升级不丢
数据）；自己设置时填 NAS 绝对路径（如 `/vol1/1000/docker/xunlei/data`、
`/vol1/1000/downloads`）。

前提：NAS 已安装并启用 Docker（应用中心 → Docker）。部署只管理插件自建的
容器 `fnmedia-dl-xunlei`，不碰你自己已有的任何容器；重复点击幂等（已在跑
就直接复用）。若旧版本部署的容器起不来（日志报 `mount: permission denied`），
再点一次「一键部署」会自动清理重建修复。

### 手动部署

引擎直连通道配合 **cnk3x/xunlei Docker 版** 使用。host 网络最省心（UPnP 正常、
下载速度更好），面板默认 `http://NAS-IP:2345`：

```yaml
services:
  xunlei:
    container_name: xunlei
    image: cnk3x/xunlei:latest
    restart: unless-stopped
    network_mode: host        # bridge 则加 ports: ["2345:2345"] 并按需改外端口
    cap_add: [SYS_ADMIN, SYS_CHROOT]
    security_opt: ["apparmor=unconfined"]   # 不加会报 mount: permission denied
    environment:
      - XL_UID=1000           # 改成你自己的 UID/GID（通常 1000），避免下载文件属主是 root
      - XL_GID=1000
      - TZ=Asia/Shanghai
    volumes:
      - /vol1/1000/docker/xunlei/data:/xunlei/data        # 账号/配置，必须持久化
      - /vol1/1000/downloads:/xunlei/downloads            # 下载目录，按需改
```

要点：

- **挂载权限**：引擎启动时会在容器内 bind mount（构建套件运行环境），默认的
  AppArmor 策略会拦截，容器日志报 `… mount: permission denied` 后退出。
  上面的 `cap_add` + `security_opt: apparmor=unconfined` 即为解法
  （[cnk3x/xunlei#108](https://github.com/cnk3x/xunlei/issues/108)）；个别环境仍
  被拦时改用 `privileged: true`（并可加 `security_opt: ["seccomp=unconfined"]`）。
- 首次打开面板要登录迅雷账号并填内测邀请码（`迅雷牛通`），跟应用中心旧版一致。
- 面板里显示的下载路径是去掉 `/xunlei` 前缀的（如 `/downloads`）；本插件「保存目录」
  照抄面板里显示的路径即可，留空则用引擎默认目录。
- 有多个下载目录时用 `XL_DIR_DOWNLOAD`（冒号分隔，均以 `/xunlei` 开头）配合多条
  volumes 映射。
- 完整参数见 [cnk3x/xunlei](https://github.com/cnk3x/xunlei)。

## 迅雷接口说明（重要）

迅雷相关通道基于其 Web 端/客户端在用的接口实现（与公开协议分析一致），属于**非官方协议**，可能随官方改版失效或改变行为。架构上做了
隔离：任一迅雷通道失效时自动降级到 aria2，不影响基本可用性。ed2k 协议需要
amule 等重型组件，暂不支持派发。

**保存目录解析**：迅雷引擎建任务时必须给出下载根目录对应的
`parent_folder_id`（缺省任务只会停在「排队中」不会开始下载）。插件会按
`/device/config` 的下载路径 → 目录表 realPath 匹配 → 顶层唯一目录 → 首个
顶层目录的顺序自动识别下载根。目录表查询按参数形状阶梯自动适配不同网关
（云端 api-pan 只认标准形状，本地引擎认 扩展/空间形状，逐形状重试）；
「保存目录」填根下的相对路径（如 `movies/2025`）即可，缺级目录会自动创建。
目录解析或建目录失败时（目录在下载根之外、引擎账号对该目录无读写权等）
不会报错，按阶梯降级保留下载根 id 并把目录路径原样交给引擎落盘；本地引擎
在目录表彻底查不到时最后兜底回退直传形状（引擎按路径自行落盘）。诊断信息
写入 `state/dispatch.log`。

## 测试

```bash
python -m unittest discover -s tests -v    # 244 个用例，全程无网络
```

覆盖：链接解析（magnet/thunder/ed2k/种子）、drive/v1 请求形状、云端登录链与
签名链、令牌自动续期、下载目录/parent_folder 解析、aria2 JSON-RPC、派发降级、
统一任务视图、API 层。

## License

MIT · Copyright (c) 2026 许诺

---

很庆幸曾经遇见你，遗憾的是只是遇见你——许诺
