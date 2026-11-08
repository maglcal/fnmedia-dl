#!/usr/bin/env python3
"""更新源解析：多源检查 + 下载候选（自建 RepoBox 镜像优先，GitHub 兜底）。

背景：GitHub Releases 在国内访问慢/超时。发行资产由 tools/sync_repobox.py
自动同步到自建 RepoBox（raw 直链国内快），插件优先从镜像检查与下载，
失败按源自动重试、回退官方源。

config 的 update 段：
  sources   [{name, type, ...}] 按序尝试，第一个可用为准。
            type=repobox（base_url 留空自动跳过）| github。
  timeout   单次请求超时秒（默认 15）
  retries   单个源的请求尝试次数（默认 2，间隔递增）

RepoBox 清单（镜像仓库根部 latest.json，由同步脚本生成）：
  {"version","tag","published_at","notes","html_url",
   "assets":[{"name","path","size","sha256"}]}
"""
from __future__ import annotations

import time

import httpx

GITHUB_API = "https://api.github.com"


class SourceError(RuntimeError):
    """更新源不可用 / 清单异常（用户可见中文）。"""


# ------------------------------------------------------------ 工具

def resolve_sources(cfg: dict) -> list[dict]:
    """取有效更新源（按配置序）：未配置 / 未知类型的源自动跳过。"""
    out: list[dict] = []
    for s in (cfg.get("update") or {}).get("sources") or []:
        if not isinstance(s, dict):
            continue
        typ = s.get("type")
        if typ == "repobox":
            base = (s.get("base_url") or "").rstrip("/")
            if not base:
                continue                      # 未配域名 → 跳过（保持旧版行为）
            out.append({**s, "base_url": base})
        elif typ == "github":
            if s.get("repo"):
                out.append(dict(s))
    return out


def allowed_prefixes(cfg: dict) -> list[str]:
    """镜像资产下载地址白名单前缀（供 updater 放行校验用）。"""
    out = []
    for s in resolve_sources(cfg):
        if s.get("type") == "repobox":
            out.append(f"{s['base_url']}/raw/{s.get('repo') or 'fnmedia-dl'}/")
    return out


def strip_ver(s: str) -> str:
    """发布标识 → 纯版本号：剥 main-v / v 前缀（新格式 main-vX.Y.Z 与
    旧格式 vX.Y.Z 兼容），供版本比较与 UI 显示统一用 X.Y.Z。"""
    s = (s or "").strip()
    if s.lower().startswith("main-v"):
        return s[6:].strip()
    return s.lstrip("vV").strip()


def _ver_tuple(v: str) -> tuple[int, int, int]:
    import re
    nums: list[int] = []
    for piece in (v or "").split("."):
        m = re.search(r"\d+", piece)
        nums.append(int(m.group()) if m else 0)
        if len(nums) == 3:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _get(client: httpx.Client, url: str, *, headers: dict,
         timeout: float, retries: int,
         ok_status: tuple[int, ...] = (200,)) -> httpx.Response:
    """GET + 重试：网络类错误/非预期状态码按 attempts 重试，间隔递增。"""
    last = "请求失败"
    for attempt in range(max(1, int(retries))):
        try:
            resp = client.get(url, headers=headers, timeout=timeout)
            if resp.status_code not in ok_status:
                raise SourceError(f"HTTP {resp.status_code}")
            return resp
        except (httpx.HTTPError, SourceError) as exc:
            last = str(exc)
            if attempt < retries - 1:
                time.sleep(0.5 * (attempt + 1))
    raise SourceError(last)


# ------------------------------------------------------------ 源适配

def _fetch_repobox(src: dict, client: httpx.Client, *,
                   timeout: float, retries: int, current: str) -> dict:
    base, repo = src["base_url"], src.get("repo") or "fnmedia-dl"
    headers = {"user-agent": f"fnmedia-dl/{current}"}
    if src.get("token"):
        headers["Authorization"] = f"Bearer {src['token']}"
    resp = _get(client, f"{base}/raw/{repo}/latest.json",
                headers=headers, timeout=timeout, retries=retries)
    try:
        data = resp.json()
    except ValueError:
        raise SourceError("清单不是合法 JSON")
    if not isinstance(data, dict) or not data.get("version"):
        raise SourceError("清单格式异常（缺 version）")
    assets = []
    for a in data.get("assets") or []:
        if not isinstance(a, dict):
            continue
        path = a.get("path") or a.get("name") or ""
        if not path:
            continue
        assets.append({
            "name": a.get("name") or path.rsplit("/", 1)[-1],
            "url": f"{base}/raw/{repo}/{path}",
            "size": int(a.get("size") or 0),
            "sha256": str(a.get("sha256") or "").lower(),
        })
    return {"version": strip_ver(str(data.get("version") or "")),
            "tag": data.get("tag") or "",
            "notes": data.get("notes") or "",
            "html_url": data.get("html_url") or "",
            "assets": assets}


def _fetch_github(src: dict, client: httpx.Client, *,
                  timeout: float, retries: int, current: str) -> dict:
    repo = src["repo"]
    headers = {"accept": "application/vnd.github+json",
               "user-agent": f"fnmedia-dl/{current}"}
    if src.get("token"):
        headers["authorization"] = f"Bearer {src['token']}"
    resp = _get(client, f"{GITHUB_API}/repos/{repo}/releases/latest",
                headers=headers, timeout=timeout, retries=retries,
                ok_status=(200, 404))
    if resp.status_code == 404:
        # 仓库尚无 release：不是错误，给出「暂无发布版本」的空清单
        return {"version": "", "tag": "",
                "notes": "仓库暂无发布版本",
                "html_url": f"https://github.com/{repo}/releases",
                "assets": []}
    try:
        data = resp.json()
    except ValueError:
        raise SourceError("响应不是合法 JSON")
    assets = [{"name": a.get("name") or "",
               "url": a.get("browser_download_url") or "",
               "size": int(a.get("size") or 0),
               "sha256": ""}
              for a in (data.get("assets") or []) if isinstance(a, dict)]
    # 发行配套 .sha256 资产：读哈希供下载后校验（失败不阻断）
    for asset in assets:
        if asset["name"].endswith(".fpk"):
            sib = next((x for x in assets
                        if x["name"] == asset["name"] + ".sha256"), None)
            if sib and sib["url"]:
                try:
                    text = _get(client, sib["url"], headers={},
                                timeout=timeout, retries=1).text
                    asset["sha256"] = (text.strip().split() or [""])[0].lower()
                except SourceError:
                    pass
    tag = str(data.get("tag_name") or "").strip()
    return {"version": strip_ver(tag), "tag": tag,
            "notes": data.get("body") or "",
            "html_url": data.get("html_url")
            or f"https://github.com/{repo}/releases",
            "assets": assets}


_FETCHERS = {"repobox": _fetch_repobox, "github": _fetch_github}


# ------------------------------------------------------------ 归一化

def _syntactic_url(src: dict, tag: str, name: str) -> str:
    """按约定推导某资产在该源上的直链（用作下载回退候选）。
    布局约定：RepoBox 同步脚本存 releases/{tag}/{name}（tools/sync_repobox.py），
    GitHub 为 releases/download/{tag}/{name}。"""
    if not name or ".." in name or not tag:
        return ""
    if src.get("type") == "repobox":
        return (f"{src['base_url']}/raw/{src.get('repo') or 'fnmedia-dl'}"
                f"/releases/{tag}/{name}")
    if src.get("type") == "github":
        return (f"https://github.com/{src['repo']}/releases/"
                f"download/{tag}/{name}")
    return ""


def _normalize(current: str, src: dict, info: dict, cfg: dict,
               notice: str = "") -> dict:
    ver = info.get("version") or ""
    tag = info.get("tag") or f"main-v{ver}"
    # 跨源回退候选按新旧两种 tag 命名各合成一份（main-vX.Y.Z / 旧 vX.Y.Z），
    # 某源目录布局与 tag 命名对不上时另一条仍可命中
    tags = [tag]
    if ver:
        for t in (f"main-v{ver}", f"v{ver}"):
            if t not in tags:
                tags.append(t)
    assets = []
    for a in info.get("assets") or []:
        if not a.get("url"):
            continue
        fallbacks: list[str] = []
        for other in resolve_sources(cfg):
            for t in tags:
                u = _syntactic_url(other, t, a["name"])
                if u and u != a["url"] and u not in fallbacks:
                    fallbacks.append(u)
        assets.append({**a, "fallback_urls": fallbacks})
    latest = info.get("version") or ""
    return {"ok": True, "current": current, "latest": latest or current,
            "has_update": bool(latest) and _ver_tuple(latest) > _ver_tuple(current),
            "url": info.get("html_url") or "",
            "notes": (info.get("notes") or "")[:2000],
            "source": src.get("name") or src.get("type") or "",
            "notice": notice,
            "assets": assets,
            "fetched_at": int(time.time())}


# ------------------------------------------------------------ 入口

def check(cfg: dict, current: str,
          client: httpx.Client | None = None) -> dict:
    """多源检查更新：按序尝试源，第一个成功者为准；
    每个源内部重试 retries 次；全部失败抛 SourceError（含逐源原因）。"""
    own = client is None
    client = client or httpx.Client(trust_env=True)   # 官方源可走系统代理
    try:
        sources = resolve_sources(cfg)
        if not sources:
            raise SourceError("未配置任何更新源（update.sources）")
        upd = cfg.get("update") or {}
        timeout = float(upd.get("timeout") or 15)
        retries = int(upd.get("retries") or 2)
        errors: list[str] = []
        for src in sources:
            fetch = _FETCHERS.get(src.get("type") or "")
            if fetch is None:
                continue
            try:
                info = fetch(src, client, timeout=timeout, retries=retries,
                             current=current)
                # 回退透明提示：前面的源失败才走到这里 → 告知已切换备用源及原因
                notice = ""
                if errors:
                    used = src.get("name") or src.get("type") or ""
                    notice = (f"{'；'.join(errors)}——已自动切换到备用源"
                              f"「{used}」完成检查，更新流程不受影响")
                return _normalize(current, src, info, cfg, notice=notice)
            except SourceError as exc:
                errors.append(f"源「{src.get('name') or src.get('type')}」不可用（{exc}）")
        raise SourceError("全部更新源不可用：" + "；".join(errors))
    finally:
        if own:
            client.close()
