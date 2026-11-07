#!/usr/bin/env python3
"""GitHub Releases → 自建 RepoBox 镜像自动同步。

把 GitHub 发行资产（.fpk / .sha256）同步到自建 RepoBox 仓库站，并生成
latest.json 清单（格式见 app/updatesrc.py），供 fnmedia-dl 插件走国内
镜像检查与下载更新。仅用 Python 标准库（GitHub Actions / NAS 通用）。

环境变量：
  REPOBOX_URL     RepoBox 站点地址（必填，如 https://repo.example.com）
  REPOBOX_TOKEN   API 令牌 rbx_xxx（必填，网页「令牌」页创建）
  REPOBOX_REPO    镜像仓库名（默认 fnmedia-dl；不存在自动创建）
  GH_REPO         GitHub 仓库（默认 maglcal/fnmedia-dl）
  GITHUB_TOKEN    可选，GitHub API 限流保护
  GH_API          可选，GitHub API 地址（默认 https://api.github.com，
                  GitHub Enterprise / 本地联调可覆盖）

用法：
  python3 tools/sync_repobox.py             # 同步最新 release（幂等，已同步自动跳过）
  python3 tools/sync_repobox.py --tag v0.1.23
  python3 tools/sync_repobox.py --force     # 跳过幂等检查强制重传

退出码：0 成功（含已同步跳过）；1 失败。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

UA = "fnmedia-dl-sync/1.0"


# ------------------------------------------------------------ HTTP 小工具

def _request(url: str, *, method: str = "GET", headers: dict | None = None,
             data: bytes | None = None, timeout: int = 60) -> tuple[int, bytes]:
    """发请求，返回 (status, body)。HTTP 错误状态也返回 body 不抛。"""
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"user-agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise SystemExit(f"网络错误：{url} → {exc.reason}") from exc


def _api(base: str, path: str, token: str, *, method: str = "GET",
         headers: dict | None = None,
         data: bytes | None = None) -> tuple[int, bytes]:
    return _request(f"{base.rstrip('/')}{path}", method=method,
                    headers={"Authorization": f"Bearer {token}",
                             **(headers or {})}, data=data)


def _err_detail(body: bytes) -> str:
    try:
        return json.loads(body.decode("utf-8", "replace"))["error"]["message"]
    except Exception:  # noqa: BLE001 - 仅用于错误信息拼接
        return body.decode("utf-8", "replace")[:200]


def _quote_path(path: str) -> str:
    """仓库内文件路径按 / 逐段 URL 编码。"""
    return "/".join(urllib.parse.quote(seg) for seg in path.split("/"))


# ------------------------------------------------------------ GitHub 侧

def fetch_release(gh_repo: str, tag: str, gh_token: str) -> dict:
    """取 GitHub release（tag 空 = latest）。返回规范化信息。"""
    gh_api = os.environ.get("GH_API", "").strip() or "https://api.github.com"
    api = f"{gh_api.rstrip('/')}/repos/{gh_repo}"
    url = f"{api}/releases/tags/{urllib.parse.quote(tag)}" if tag \
        else f"{api}/releases/latest"
    headers = {"accept": "application/vnd.github+json"}
    if gh_token:
        headers["authorization"] = f"Bearer {gh_token}"
    status, body = _request(url, headers=headers)
    if status == 404:
        raise SystemExit(f"GitHub 上找不到 release：{gh_repo} {tag or 'latest'}")
    if status != 200:
        raise SystemExit(f"GitHub API 失败：HTTP {status} {_err_detail(body)}")
    rel = json.loads(body)
    assets = []
    for a in rel.get("assets") or []:
        name, dl = a.get("name") or "", a.get("browser_download_url") or ""
        if name and dl:
            assets.append({"name": name, "url": dl, "size": int(a.get("size") or 0)})
    return {"tag": rel.get("tag_name") or tag,
            "version": (rel.get("tag_name") or "").lstrip("vV"),
            "notes": rel.get("body") or "",
            "published_at": rel.get("published_at") or "",
            "html_url": rel.get("html_url") or "",
            "assets": assets}


def download(url: str) -> bytes:
    status, body = _request(url, headers={"accept": "application/octet-stream"})
    if status != 200:
        raise SystemExit(f"下载资产失败：HTTP {status} {url}")
    return body


# ------------------------------------------------------------ RepoBox 侧

def ensure_repo(base: str, token: str, repo: str) -> None:
    """幂等建仓：已存在（409）视为成功。"""
    payload = json.dumps({"name": repo,
                          "description": "fnmedia-dl 国内更新镜像（GitHub 自动同步）"
                          }).encode()
    status, body = _api(base, "/api/v1/repos", token, method="POST",
                        headers={"Content-Type": "application/json"}, data=payload)
    if status in (201, 409):
        return
    raise SystemExit(f"建仓失败：HTTP {status} {_err_detail(body)}")


def put_file(base: str, token: str, repo: str, path: str,
             content: bytes, message: str, *, as_json_text: bool = False) -> None:
    """写文件：文本/JSON 走 JSON 通道，二进制走 octet-stream 通道。200/201 均成功。"""
    url = (f"/api/v1/repos/{urllib.parse.quote(repo)}"
           f"/files/{_quote_path(path)}")
    if as_json_text:
        payload = json.dumps({"content": content.decode("utf-8"),
                              "message": message}).encode()
        status, body = _api(base, url, token, method="PUT",
                            headers={"Content-Type": "application/json"},
                            data=payload)
    else:
        url += f"?message={urllib.parse.quote(message)}"
        status, body = _api(base, url, token, method="PUT",
                            headers={"Content-Type": "application/octet-stream"},
                            data=content)
    if status not in (200, 201):
        raise SystemExit(f"上传 {path} 失败：HTTP {status} {_err_detail(body)}")


def read_file(base: str, token: str, repo: str, path: str) -> bytes:
    """回读文件原始字节（校验用）。"""
    url = (f"/api/v1/repos/{urllib.parse.quote(repo)}"
           f"/files/{_quote_path(path)}")
    status, body = _api(base, url, token,
                        headers={"Accept": "application/octet-stream"})
    if status != 200:
        raise SystemExit(f"回读 {path} 失败：HTTP {status} {_err_detail(body)}")
    return body


def fetch_manifest(base: str, repo: str) -> dict | None:
    """读镜像现有 latest.json（匿名 raw 直链）；无/坏返回 None。"""
    status, body = _request(f"{base.rstrip('/')}/raw/"
                            f"{urllib.parse.quote(repo)}/latest.json")
    if status != 200:
        return None
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


# ------------------------------------------------------------ 主流程

def main() -> int:
    parser = argparse.ArgumentParser(description="发布资产同步")
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""),
                        help="release 标签（默认取最新 release）")
    parser.add_argument("--force", action="store_true", help="跳过幂等检查强制重传")
    args = parser.parse_args()

    base = os.environ.get("REPOBOX_URL", "").strip()
    token = os.environ.get("REPOBOX_TOKEN", "").strip()
    repo = os.environ.get("REPOBOX_REPO", "").strip() or "fnmedia-dl"
    gh_repo = os.environ.get("GH_REPO", "").strip() or "maglcal/fnmedia-dl"
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not base or not token:
        print("缺少环境变量 REPOBOX_URL / REPOBOX_TOKEN", file=sys.stderr)
        return 1

    rel = fetch_release(gh_repo, args.tag, gh_token)
    tag = rel["tag"]
    print(f"[sync] GitHub {gh_repo} {tag} → RepoBox {repo}")

    # 同步范围：发行资产里的 .fpk 与 .sha256
    wanted = [a for a in rel["assets"]
              if a["name"].endswith((".fpk", ".fpk.sha256"))]
    if not any(a["name"].endswith(".fpk") for a in wanted):
        print("[sync] 该 release 没有 .fpk 资产，跳过", file=sys.stderr)
        return 1

    # 幂等检查：镜像已有同版本且资产齐全 → 跳过
    if not args.force:
        man = fetch_manifest(base, repo)
        if man and str(man.get("tag") or "") == tag:
            have = {a.get("name") for a in man.get("assets") or []}
            need = {a["name"] for a in wanted if a["name"].endswith(".fpk")}
            if need <= have:
                print(f"[sync] {tag} 已同步，跳过（--force 可强制重传）")
                return 0

    ensure_repo(base, token, repo)

    # 下载并上传资产，收集清单
    manifest_assets = []
    for a in wanted:
        data = download(a["url"])
        digest = hashlib.sha256(data).hexdigest()
        path = f"releases/{tag}/{a['name']}"
        is_text = a["name"].endswith(".sha256")
        put_file(base, token, repo, path, data,
                 f"sync {tag} {a['name']}", as_json_text=is_text)
        print(f"[sync] ↑ {path}  ({len(data)} 字节, sha256 {digest[:12]}…)")
        if a["name"].endswith(".fpk"):
            manifest_assets.append({"name": a["name"], "path": path,
                                    "size": len(data), "sha256": digest})
        # 回读校验
        back = read_file(base, token, repo, path)
        if hashlib.sha256(back).hexdigest() != digest:
            print(f"[sync] 回读校验不符：{path}", file=sys.stderr)
            return 1

    # 最后写清单（清单落盘 = 同步完成提交点）
    manifest = {"version": rel["version"], "tag": tag,
                "published_at": rel["published_at"], "notes": rel["notes"],
                "html_url": rel["html_url"], "assets": manifest_assets}
    put_file(base, token, repo, "latest.json",
             json.dumps(manifest, ensure_ascii=False, indent=1).encode(),
             f"sync {tag} latest.json", as_json_text=True)
    back = read_file(base, token, repo, "latest.json")
    if json.loads(back) != manifest:
        print("[sync] latest.json 回读校验不符", file=sys.stderr)
        return 1

    print(f"[sync] 完成：{tag} 共 {len(manifest_assets)} 个 fpk 资产 + latest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
