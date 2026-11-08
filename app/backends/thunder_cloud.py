#!/usr/bin/env python3
"""迅雷云端远程通道（主通道）：以迅雷账号登录云端，向账号绑定的远程设备下发任务。

任务由远程设备执行，走迅雷 P2SP 加速；官方口径「迅雷 App 远程下载没有下载
次数限制」，因此不占套件端每日任务配额。

协议要点（与官方 Web 端一致）：
  v3 登录  POST https://xluser-ssl.xunlei.com/xluser.core.login/v3/login
  人机令牌 POST https://xluser-ssl.xunlei.com/v1/shield/captcha/init
  换令牌   POST https://xluser-ssl.xunlei.com/v1/auth/signin/token
  刷令牌   POST https://xluser-ssl.xunlei.com/v1/auth/token
  业务 API https://api-pan.xunlei.com/drive/v1/*（与本地引擎同构）
  设备专属 API GET /drive/v1/apps/INNER_API?space=<target> 发现中继域名，远程设备的目录表/建目录/设备配置走该域名（云端目录接口只服务云盘空间，设备空间走设备专属接口）
签名：captcha_sign/devicesign 按约定的摘要算法计算。
凭据仅保存在本机 state/config.json，不上传任何服务器。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from .base import Backend, BackendError, ParsedResource, TaskView
from .drive_core import (DriveCore, _agg_tries, _clip_timeout, _dbg,
                         task_to_view)

XLUSER_BASE = "https://xluser-ssl.xunlei.com"
XLUSER_API = XLUSER_BASE + "/v1"
DRIVE_API = "https://api-pan.xunlei.com/drive/v1"

CLIENT_ID = "Xp6vsxz_7IYVw2BB"
CLIENT_SECRET = "Xp6vsy4tN9toTVdMSpomVdXpRmES"
CLIENT_VERSION = "8.31.0.9726"
PACKAGE_NAME = "com.xunlei.downloadprovider"
APP_ID = "40"
APP_KEY = "34a062aaa22f906fca4fefe9fb3a3021"
SIGN_PROVIDER = "access_end_point_token"
REDIRECT_URI = "xlaccsdk01://xunlei.com/callback?state=harbor"

ALGORITHMS = [
    "9uJNVj/wLmdwKrJaVj/omlQ",
    "Oz64Lp0GigmChHMf/6TNfxx7O9PyopcczMsnf",
    "Eb+L7Ce+Ej48u",
    "jKY0",
    "ASr0zCl6v8W4aidjPK5KHd1Lq3t+vBFf41dqv5+fnOd",
    "wQlozdg6r1qxh0eRmt3QgNXOvSZO6q/GXK",
    "gmirk+ciAvIgA/cxUUCema47jr/YToixTT+Q6O",
    "5IiCoM9B1/788ntB",
    "P07JH0h6qoM6TSUAK2aL9T5s2QBVeY9JWvalf",
    "+oK0AN",
]

USER_AGENT = (
    "ANDROID-com.xunlei.downloadprovider/8.31.0.9726 netWorkType/5G appid/40 "
    "deviceName/Xiaomi_M2004j7ac deviceModel/M2004J7AC OSVersion/12 "
    "protocolVersion/301 platformVersion/10 sdkVersion/512000 "
    "Oauth2Client/0.9 (Linux 4_14_186-perf-gddfs8vbb238b) (JAVA 0)"
)
CORE_LOGIN_UA = "android-ok-http-client/xl-acc-sdk/version-5.0.12.512000"
# 短信验证链路使用浏览器同形 UA、不带 SDK 自定义头。
REVIEW_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

EMAIL_RE = re.compile(r"\w+([-+.]\w+)*@\w+([-.]\w+)*\.\w+([-.]\w+)*")

# 需要刷新令牌重试的错误码
TOKEN_ERROR_CODES = {4122, 4121, 10, 16}
CAPTCHA_ERROR_CODE = 9
# 设备端调用超时（社区同报案例）：设备端插件调用超时，返回
# {"error":"external_unknown_err","error_code":2,"error_description":"远程调用失败",
#  "error_details":[{"detail":"Get \"http://k8s.xsl/drive-reverse-proxy/...\":
#  net/http: request canceled (Client.Timeout exceeded while awaiting headers)"},…]}
# ——设备端插件响应慢或未运行（瞬态居多）。
DEVICE_TIMEOUT_CODE = 2


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _err_code(data: dict) -> int:
    # 登录接口用 errorCode（字符串），drive 接口用 error_code（整型），两者都认
    for key in ("error_code", "errorCode"):
        try:
            v = int(data.get(key) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


def _is_error(data: dict) -> bool:
    """error == "success" 视为成功；
    否则 error_code / error / error_description 任一非空即错误。"""
    if (data.get("error") or "").strip() == "success":
        return False
    return bool(_err_code(data) or (data.get("error") or "").strip()
                or (data.get("error_description") or "").strip()
                or (data.get("errorDesc") or "").strip())


def _pkg_version() -> str:
    """运行时版本（诊断用）：与 main.py 同源——向上找 VERSION 文件。

    错误文案带上它，一眼区分「装了新包但服务进程没重启」（文案版本旧）
    与「真跑着新代码」（文案版本新）。"""
    here = os.path.abspath(__file__)
    for _ in range(4):
        here = os.path.dirname(here)
        vf = os.path.join(here, "VERSION")
        if os.path.isfile(vf):
            try:
                with open(vf, encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                break
    return "0.0.0"


def _err_text(prefix: str, data: dict) -> str:
    # errorDesc 是中文人读文案（error_description 是英文），优先中文
    detail = (data.get("errorDesc") or data.get("error_description")
              or data.get("error") or "未知错误")
    code = data.get("error_code") or data.get("errorCode") or 0
    return f"{prefix}：{detail}（error_code={code} · v{_pkg_version()}）"


def _is_device_timeout(data: dict) -> bool:
    """设备端调用超时类错误（error_code=2 / external_unknown_err / 远程调用失败）：
    瞬态居多，GET 可安全重试。"""
    if _err_code(data) == DEVICE_TIMEOUT_CODE:
        return True
    if (data.get("error") or "").strip() == "external_unknown_err":
        return True
    return "远程调用失败" in (data.get("errorDesc") or ""
                             or data.get("error_description") or "")


def _masked_hint(data: dict) -> str:
    """error_details[].detail 证据摘要（脱敏）：含 uid/设备哈希/URL，
    长十六进制与长数字段掩掉，只留链路形态给人看。
    占位符用 [hash]/[id] 而非尖括号（0.1.34）——尖括号在 UI innerHTML
    里会被当标签吞掉（0.1.33 实测显示成 proxy///），文本语境也更清晰。"""
    for it in (data.get("error_details") or []):
        if isinstance(it, dict) and (it.get("detail") or "").strip():
            s = str(it["detail"])
            s = re.sub(r"[0-9a-fA-F]{16,}", "[hash]", s)
            s = re.sub(r"\d{6,}", "[id]", s)
            return s[:200]
    return ""


def _device_timeout_text(data: dict) -> str:
    """设备端调用超时的专用错误文案：说清是谁的问题、影响面、怎么办。"""
    hint = _masked_hint(data)
    return (
        "迅雷接口错误：设备端响应超时（error_code=2 · v" + _pkg_version() + "）——"
        "迅雷服务端调用目标设备超时：设备端迅雷远程插件响应慢或未运行"
        "（设备高负载下载/网络链路差/插件未启用均会触发）。此错误只影响目录浏览与"
        "路径解析，稍后重试常自动恢复；若持续出现，请检查目标设备上的迅雷远程插件"
        "是否在运行，或在设置里换一台支持远程插件的设备"
        + (f"。证据：{hint}" if hint else ""))


def device_sign(device_id: str) -> str:
    base = f"{device_id}{PACKAGE_NAME}{APP_ID}{APP_KEY}"
    inner = hashlib.sha1(base.encode("utf-8")).hexdigest()
    return "div101." + device_id + _md5_hex(inner)


def captcha_sign(device_id: str) -> tuple[str, str]:
    timestamp = str(int(time.time() * 1000))
    s = f"{CLIENT_ID}{CLIENT_VERSION}{PACKAGE_NAME}{device_id}{timestamp}"
    for algo in ALGORITHMS:
        s = _md5_hex(s + algo)
    return timestamp, "1." + s


class _CloudDrive(DriveCore):
    """带错误码检测与令牌/人机令牌自动续期的 drive/v1 客户端。

    远程设备的目录表/建目录走设备专属 API。"""

    # 设备专属 API 发现缓存 TTL（
    # 取 300s 减少发现请求；失效时自动重发现，见 _folders 的 refresh 轮）
    _RELAY_TTL = 300

    def __init__(self, client: httpx.Client, backend: "ThunderCloud"):
        super().__init__(client, DRIVE_API, backend._auth_headers)
        self.backend = backend
        self._device_apis: dict[str, dict] = {}
        # 设备端超时 GET 重试次数（0.1.33 可治理）：默认 2（共 3 次尝试）保持
        # 运行态瞬态自愈；诊断探针期设 0 让真实错误透出——0.1.32 实测 3 次
        # 重试链（~3s×3+退避）撑爆诊断窗口，真实错误被「超时」文案掩盖。
        self._device_timeout_retries = 2

    def _request(self, method, path, *, params=None, json_body=None,
                 timeout=30, deadline=None):
        # 自行发请求（不走 DriveCore._request）：迅雷错误多以 200/4xx + error_code
        # 返回，必须先按错误码分流（令牌续期），不能只看 HTTP 状态码。
        # path 以 http 开头时视为完整 URL（如 /device/config 同级接口）。
        url = path if path.startswith("http") else self.base_url + path
        # 人机令牌刷新的 action 是完整 URL 路径（含 /drive/v1 前缀）
        action = method + ":" + urlsplit(url).path
        for attempt in range(3):
            try:
                resp = self.client.request(method, url, params=params,
                                           json=json_body,
                                           headers=self._headers(),
                                           timeout=timeout)
            except httpx.HTTPError as exc:
                raise BackendError(f"网络错误（请检查 NAS 出网/DNS/代理设置）：{exc}") from exc
            try:
                data = resp.json()
            except ValueError:
                raise BackendError(f"迅雷接口响应异常（HTTP {resp.status_code}）："
                                   f"{resp.text[:120]}")
            if not isinstance(data, dict):
                raise BackendError(f"迅雷接口响应异常（HTTP {resp.status_code}）")
            if not _is_error(data):
                return data
            err_code = _err_code(data)
            if attempt < 2 and err_code in TOKEN_ERROR_CODES:
                self.backend._refresh_access_token()
                continue
            if attempt < 2 and err_code == CAPTCHA_ERROR_CODE:
                # 登录后刷新：user_id + captcha_sign 签名 meta；user_id 为空也不改 meta 形态
                self.backend._refresh_captcha_at_login(
                    action, self.backend.conf.get("user_id", ""))
                continue
            if (attempt < self._device_timeout_retries and method == "GET"
                    and _is_device_timeout(data)
                    and (deadline is None or time.monotonic() < deadline)):
                # 设备端超时瞬态居多，GET 幂等可重试。
                # 1s 退避给设备端插件喘息；POST 不重试（建目录非幂等）。
                # deadline（0.1.33）：预算内才重试——重试链不得超出解析
                # 总预算，否则撑爆诊断窗/响应窗（0.1.32）。
                time.sleep(1)
                continue
            if err_code == CAPTCHA_ERROR_CODE:
                uid = self.backend.conf.get("user_id", "")
                raise BackendError(
                    "迅雷接口错误：人机令牌校验失败（error_code=9）——"
                    "这不是短信验证码输错；请点「重新登录」重试，"
                    "若仍失败请重新完成一次短信验证"
                    f"（v{_pkg_version()}，user_id={'已取到' if uid else '空'}）")
            if _is_device_timeout(data):
                raise BackendError(_device_timeout_text(data))
            raise BackendError(_err_text("迅雷接口错误", data))
        raise BackendError("迅雷接口错误：重试后仍失败")

    # ------------------------------------------------ 设备专属 API（0.1.30）

    def _relay_api(self, space: str, refresh: bool = False,
                   deadline: float | None = None) -> dict:
        """发现远程设备的专属 API 网关。

        链路：
          GET https://api-pan.xunlei.com/drive/v1/apps/INNER_API?space=<target>
          （仅 Bearer 认证）→ {"link": "<url>", "links": [{"url": "<url>"}]}
          url 形如 https://<relay-host>/<path>/?plugin_app_id=X&plugin_app_token=Y
        ApiDomain = 去掉 query、保留路径、补尾斜杠，
        后续请求 <ApiDomain>drive/v1/files?...&plugin_app_id=X&plugin_app_token=Y。"""
        now = time.time()
        ent = self._device_apis.get(space)
        if ent and not refresh and now - ent.get("at", 0) < self._RELAY_TTL:
            return ent
        data = self._request("GET", DRIVE_API + "/apps/INNER_API",
                             params={"space": space},
                             timeout=_clip_timeout(deadline, 10),
                             deadline=deadline)
        url = (data.get("link") or "").strip()
        if not url:
            for it in (data.get("links") or []):
                if isinstance(it, dict) and (it.get("url") or "").strip():
                    url = it["url"].strip()
                    break
        if not url:
            raise BackendError(
                "设备专属 API 发现失败：INNER_API 响应缺少 link/links[].url")
        parts = urlsplit(url)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if not base.endswith("/"):
            base += "/"        # ApiDomain+相对路径 是字符串拼接，必须带尾斜杠
        q = parse_qs(parts.query)
        ent = {"base": base,
               "plugin_app_id": (q.get("plugin_app_id") or [""])[0],
               "plugin_app_token": (q.get("plugin_app_token") or [""])[0],
               "at": now}
        self._device_apis[space] = ent
        _dbg(f"设备API发现: space={space[:12]} relay={parts.netloc}{parts.path} "
             f"plugin凭据={'有' if ent['plugin_app_token'] else '无'}")
        return ent

    def _relay_params(self, api: dict, space: str) -> dict:
        """设备 API 请求公共参数：space= 设备空间、
        device_space= 空、plugin_app_id/plugin_app_token 发现凭据。"""
        p = {"device_space": "", "space": space}
        if api.get("plugin_app_id"):
            p["plugin_app_id"] = api["plugin_app_id"]
        if api.get("plugin_app_token"):
            p["plugin_app_token"] = api["plugin_app_token"]
        return p

    def _relay_folder_shapes(self, api: dict, space: str,
                             parent_id: str) -> list[tuple[str, dict]]:
        """设备 API 目录表参数形状：
        filters 三条件（trashed/phase/kind）、with_audit=true、order=TYPE_DESC、
        page_token= 空、with=withCategoryDiskMountPath&withCategoryHistoryDownloadPath
        （注意是 HistoryDownloadPath，扩展引擎形状的 withCategoryDownloadPath
        不适用于设备 API）。第二形状 kind-only 兜底防扩展参数被严格校验。"""
        filters_full = json.dumps({
            "trashed": {"eq": False},
            "phase": {"eq": "PHASE_TYPE_COMPLETE"},
            "kind": {"eq": "drive#folder"}}, separators=(",", ":"))
        filters_kind = json.dumps({"kind": {"eq": "drive#folder"}},
                                  separators=(",", ":"))
        base = self._relay_params(api, space)
        full = dict(base, parent_id=parent_id or "", limit="200",
                    with_audit="true", filters=filters_full, page_token="",
                    order="TYPE_DESC",
                    **{"with": ["withCategoryDiskMountPath",
                                "withCategoryHistoryDownloadPath"]})
        kind_only = dict(base, parent_id=parent_id or "", limit="200",
                         filters=filters_kind)
        return [("设备API完整形状", full), ("设备API极简形状", kind_only)]

    def _folders(self, space: str, parent_id: str = "",
                 deadline: float | None = None) -> list[dict]:
        """目录表走设备专属 API（0.1.30 修复）。

        0.1.31 回退策略收紧：只有**设备 API 发现失败**（INNER_API 不可达/
        缺 link）才回退基类阶梯（api-pan 标准/极简形状，兼容万一 api-pan
        开放设备空间的未来变化）；发现成功但 relay 形状全挂时直接抛聚合
        错误——通用接口对设备空间不可用，回退阶梯只会浪费响应预算。deadline：解析总预算，超时即止损。"""
        tries: list[str] = []
        discovered = False
        for refresh in (False, True):     # 第二轮强制重发现（凭据过期自愈）
            if deadline is not None and time.monotonic() >= deadline:
                tries.append("预算耗尽未及尝试")
                break
            try:
                api = self._relay_api(space, refresh=refresh, deadline=deadline)
            except BackendError as exc:
                tries.append(f"设备API发现：{str(exc)[:100]}")
                break
            discovered = True
            saw_ok = False
            for label, params in self._relay_folder_shapes(api, space, parent_id):
                if deadline is not None and time.monotonic() >= deadline:
                    tries.append("预算耗尽未及尝试")
                    break
                try:
                    data = self._request("GET", api["base"] + "drive/v1/files",
                                         params=params,
                                         timeout=_clip_timeout(deadline, 8),
                                         deadline=deadline)
                except BackendError as exc:
                    tries.append(f"{label}：{str(exc)[:100]}")
                    continue
                saw_ok = True
                files = [f for f in (data.get("files") or [])
                         if isinstance(f, dict)
                         and f.get("kind") in (None, "", "drive#folder")]
                if files:
                    return files
                tries.append(f"{label}：空表")
            if saw_ok:                    # 设备 API 可达但目录为空：如实返回
                return []
        if discovered:                    # relay 全挂 ≠ api-pan 能救，别空转
            raise BackendError(
                "云端目录表查询失败（设备API 各形状均失败，未回退 api-pan"
                "——设备空间的 api-pan /files 只会 502）：" + _agg_tries(tries))
        _dbg(f"设备API发现失败回退api-pan: {space[:12]} {str(tries)[:80]}")
        try:
            return super()._folders(space, parent_id, deadline=deadline)
        except BackendError as exc:
            # 0.1.34：基类异常带 .tries 明细时跨层合并同因（同一条错误只
            # 展开一次、标签全保留，长指引不再重复刷屏）；拿不到明细才
            # 退回截断文本。
            inner = getattr(exc, "tries", None)
            merged = (tries + list(inner) if inner else
                      tries + ["api-pan 阶梯：" + str(exc)[:200]])
            raise BackendError(
                "云端目录表查询失败（设备API 与 api-pan 均失败）："
                + _agg_tries(merged)) from exc

    def _make_folder(self, space: str, parent_id: str, name: str,
                     deadline: float | None = None) -> dict:
        """建子目录走设备专属 API（带 plugin 凭据）。
        0.1.31：仅发现失败才回退基类（api-pan）；发现成功但 relay POST
        失败直接抛错（api-pan 对设备空间建目录同样 502，回退无意义）。"""
        try:
            api = self._relay_api(space, deadline=deadline)
        except BackendError:
            return super()._make_folder(space, parent_id, name,
                                        deadline=deadline)
        data = self._request("POST", api["base"] + "drive/v1/files",
                             params=self._relay_params(api, space),
                             json_body={"parent_id": parent_id, "name": name,
                                        "space": space, "kind": "drive#folder"},
                             timeout=_clip_timeout(deadline, 10),
                             deadline=deadline)
        fid = (data.get("file") or {}).get("id") or ""
        if not fid:
            raise BackendError(
                f"创建下载子目录 {name} 失败（设备API）：响应缺少 file.id")
        return {"id": fid, "name": name, "params": {"RealPath": ""}}

    def _device_config(self, space: str = "",
                       deadline: float | None = None) -> dict:
        """设备配置（download_paths 下载根提示）优先走设备专属 API
        （<ApiDomain>device/config，与 device/info/watch 同级），
        失败回退基类（api-pan /device/config 通常不可达 → {} 静默）。"""
        if space:
            try:
                api = self._relay_api(space, deadline=deadline)
                return self._request("GET", api["base"] + "device/config",
                                     params=self._relay_params(api, space),
                                     timeout=_clip_timeout(deadline, 10),
                                     deadline=deadline)
            except BackendError:
                pass
        return super()._device_config(space, deadline=deadline)

    def _folder_shapes(self, space: str, parent_id: str) -> list[tuple[str, dict]]:
        """api-pan 回退阶梯的形状顺序（0.1.31 起仅在设备 API 发现失败时用）：
        裁剪为标准/极简 2 形状——扩展/空间引擎形状（with/device_space）
        对 api-pan 直接 502，多试纯属浪费响应预算。"""
        shapes = dict(super()._folder_shapes(space, parent_id))
        order = ["标准形状", "极简形状"]
        return [(k, shapes[k]) for k in order if k in shapes]


class ThunderCloud(Backend):
    name = "thunder_cloud"
    label = "迅雷远程通道（P2SP 加速，推荐）"

    def __init__(self, client: httpx.Client, conf: dict,
                 persist: Callable[[dict], None] | None = None):
        self.client = client
        self.conf = conf            # 直接引用 config 的 thunder_cloud 段，登录态更新即持久化
        self.persist = persist
        self.core = _CloudDrive(client, self)
        self._target = ""

    # ------------------------------------------------------------ 配置

    def _save(self) -> None:
        if self.persist:
            self.persist(self.conf)

    def _device_id(self) -> str:
        did = (self.conf.get("device_id") or "").strip()
        if len(did) != 32:
            did = _md5_hex(str(time.time_ns()) + CLIENT_ID)
            self.conf["device_id"] = did
            self._save()
        return did

    # ------------------------------------------------------------ 请求头

    def _base_headers(self, ua: str = USER_AGENT) -> dict:
        return {
            "user-agent": ua,
            "accept": "application/json;charset=UTF-8",
            "x-device-id": self._device_id(),
            "x-client-id": CLIENT_ID,
            "x-client-version": CLIENT_VERSION,
        }

    def _auth_headers(self) -> dict:
        h = self._base_headers()
        token = self.conf.get("access_token") or ""
        if token:
            h["authorization"] = f"{self.conf.get('token_type') or 'Bearer'} {token}"
        captcha = self.conf.get("captcha_token") or ""
        if captcha:
            h["x-captcha-token"] = captcha
        return h

    # ------------------------------------------------------------ 登录链

    def _request_json(self, method: str, url: str, **kw) -> dict:
        """带网络错误包装的 JSON 请求（登录链专用）。"""
        try:
            resp = self.client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            raise BackendError(f"网络错误（请检查 NAS 出网/DNS/代理设置）：{exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            raise BackendError(f"迅雷接口响应异常（HTTP {resp.status_code}）："
                               f"{resp.text[:120]}")
        if not isinstance(data, dict):
            raise BackendError(f"迅雷接口响应异常（HTTP {resp.status_code}）")
        return data

    def _review_message(self, data: dict) -> str:
        """review_panel 风控：给出验证指引与完整验证数据。

        注意：reviewurl 是迅雷 App 的内嵌验证页，独立浏览器打开白屏——主路径是本页卡片内的「短信验证」框
        （直调 sendsms/checksms），链接仅作 App 内备用。"""
        ds = device_sign(self._device_id())
        url = (data.get("reviewurl") or "").strip()
        if url:
            url = url + "&deviceid=" + ds
        ck = (data.get("creditkey") or "").strip()
        if ck:
            self.conf["creditkey"] = ck
        self.conf["review_url"] = url
        # 新验证会话：运行态密钥重置为验证链接里的值，
        # 旧短信令牌随旧会话失效
        self.conf["review_key"] = (
            (parse_qs(urlsplit(url).query).get("creditkey") or [""])[0] or ck)
        self.conf["review_token"] = ""
        self._save()
        lines = ["本次登录触发迅雷风控验证（review_panel），需短信验证一次："]
        if url:
            lines += [
                "1) 在本页「迅雷远程通道」卡的「短信验证」框点「获取验证码」，"
                "输入短信 6 位码提交；",
                "2) 验证通过后点「重新登录」。",
            ]
        else:
            lines.append("但本次响应缺少 reviewurl，无法在面板内验证——"
                         "请稍后重点「重新登录」重试获取。")
        if ck:
            lines.append(f"验证数据 creditkey={ck} deviceid={ds}（已自动填入信任密钥）")
        if url:
            lines.append(f"备用验证链接（仅迅雷 App 内置浏览器可打开，普通浏览器白屏）：{url}")
        return "\n".join(lines)

    def _core_login(self, username: str, password: str) -> str:
        body = {
            "protocolVersion": "301", "sequenceNo": "1000012",
            "platformVersion": "10", "isCompressed": "0",
            "appid": APP_ID, "clientVersion": CLIENT_VERSION,
            "peerID": "0" * 32, "appName": "ANDROID-com.xunlei.downloadprovider",
            "sdkVersion": "512000", "devicesign": device_sign(self._device_id()),
            "netWorkType": "WIFI", "providerName": "NONE",
            "deviceModel": "M2004J7AC", "deviceName": "Xiaomi_M2004j7ac",
            "OSVersion": "12", "creditkey": self.conf.get("creditkey", ""),
            "hl": "zh-CN", "userName": username, "passWord": password,
            "verifyKey": "", "verifyCode": "", "isMd5Pwd": "0",
        }
        data = self._request_json(
            "POST", XLUSER_BASE + "/xluser.core.login/v3/login",
            json=body, headers=self._base_headers(CORE_LOGIN_UA), timeout=30)
        if _is_error(data):
            if (data.get("error") or "").strip() == "review_panel":
                raise BackendError(self._review_message(data))
            raise BackendError(_err_text("迅雷登录失败", data))
        session_id = data.get("sessionID") or ""
        if not session_id:
            raise BackendError("迅雷登录失败：未返回 sessionID")
        self.conf["creditkey"] = data.get("creditkey", "")
        # 登录响应 userID：登录后刷人机令牌 meta 的首要来源；
        # 键名防御（userID/userid/user_id 都收）
        uid = data.get("userID") or data.get("userid") or data.get("user_id")
        if uid:
            self.conf["user_id"] = uid
        return session_id

    def _refresh_captcha_in_login(self, action: str, username: str) -> str:
        """登录时刷新人机令牌：
        meta 用账号形态（email / phone_number / username 三选一）。"""
        if EMAIL_RE.match(username):
            meta = {"email": username}
        elif 11 <= len(username) <= 18:
            meta = {"phone_number": username}
        else:
            meta = {"username": username}
        return self._captcha_init(action, meta)

    def _refresh_captcha_at_login(self, action: str, user_id: str) -> str:
        """登录后刷新人机令牌：
        meta 固定签名形态。注意 user_id 即使为空也必须带键——服务端按
        meta 形态区分签发场景，缺 user_id/captcha_sign 键会被当成「登录时」
        场景签发，令牌对 drive/v1 无效（error_code=9 循环的原因）。"""
        ts, sign = captcha_sign(self._device_id())
        meta = {"client_version": CLIENT_VERSION, "package_name": PACKAGE_NAME,
                "user_id": user_id or "", "timestamp": ts, "captcha_sign": sign}
        return self._captcha_init(action, meta)

    def _captcha_init(self, action: str, meta: dict) -> str:
        body = {
            "action": action,
            "captcha_token": self.conf.get("captcha_token", ""),
            "client_id": CLIENT_ID,
            "device_id": self._device_id(),
            "meta": meta,
            "redirect_uri": REDIRECT_URI,
        }
        data = self._request_json("POST", XLUSER_API + "/shield/captcha/init",
                                  json=body, headers=self._base_headers(),
                                  timeout=30)
        if data.get("url"):
            raise BackendError(f"需要人工验证：请打开 {data['url']} 完成验证后重试")
        token = data.get("captcha_token") or ""
        if not token:
            raise BackendError(_err_text("获取人机令牌失败", data))
        self.conf["captcha_token"] = token
        self._save()
        return token

    # ------------------------------------------------------------ 短信验证（review_panel）

    def review_state(self) -> dict:
        """当前是否处于待短信验证状态（供 UI 显示验证框并同步信任密钥）。"""
        url = (self.conf.get("review_url") or "").strip()
        mobile = ""
        if url:
            mobile = (parse_qs(urlsplit(url).query).get("mobile") or [""])[0]
        return {"active": bool(url), "mobile": mobile,
                "creditkey": self.conf.get("creditkey", "")}

    def _review_payload(self, creditkey: str | None = None) -> dict:
        """sendsms/checksms 参数 = 验证链接查询串 ∪ 页面基础参数兜底值。

        URL 的 deviceid 改名 devicesign、smsEvent 强制 34；creditkey 默认取运行态信任密钥。"""
        q = {k: v[0] for k, v in
             parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()}
        params = {
            "provideNname": "NONE", "netWorkType": "NONE", "providerName": "NONE",
            "protocolVersion": "301", "devicesign": device_sign(self._device_id()),
            "platformVersion": "1", "fromPlatformVersion": "1",
            "timestamp": str(int(time.time() * 1000)), "sdkVersion": "1.0",
            "deviceModel": "M2004J7AC", "deviceName": "Xiaomi_M2004j7ac",
            "OSVersion": "12",
        }
        params.update(q)          # URL 参数（appid/appName/deviceid/userID/mobile…）
        if params.get("deviceid"):
            # 页面把 URL 的 deviceid（即 device_sign 值）改名为 devicesign
            params["devicesign"] = params.pop("deviceid")
        params["smsEvent"] = "34"
        params["creditkey"] = (creditkey if creditkey is not None
                               else self._review_creditkey())
        return params

    def _review_creditkey(self) -> str:
        """验证链路的运行态信任密钥：响应续期值 > 验证链接里的 > 顶层 creditkey。

        （验证响应可能续期信任密钥，此后沿用续期值；顶层 creditkey 是登录重试用的，两者可能不是同一个值。）"""
        return ((self.conf.get("review_key") or "").strip()
                or (parse_qs(urlsplit(self.conf.get("review_url") or "").query)
                    .get("creditkey") or [""])[0]
                or (self.conf.get("creditkey") or ""))

    def _set_review_key(self, key: str) -> None:
        """记住续期后的信任密钥。"""
        self.conf["review_key"] = key.strip()
        self._save()

    # ------------------------------------------------ 验证链路传输层（与页面同形）

    def _review_base(self) -> str:
        """验证接口域名：URL 的 mainHost 参数优先。"""
        q = {k: v[0] for k, v in
             parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()}
        host = (q.get("mainHost") or "").strip().split(",")[0]
        host = host.replace("https://", "").replace("http://", "").strip("/")
        return ("https://" + host) if host else XLUSER_BASE

    def _review_cookies(self) -> dict:
        return dict(self.conf.get("review_cookies") or {})

    def _review_http(self, method: str, url: str, **kw) -> httpx.Response:
        """验证链路专用请求：浏览器同形 UA + cookie 罐（cookie 贯穿短信收发/校验/上报）。"""
        headers = {"user-agent": REVIEW_UA}
        jar = self._review_cookies()
        if jar:
            headers["cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        try:
            resp = self.client.request(method, url, headers=headers,
                                       timeout=30, **kw)
        except httpx.HTTPError as exc:
            raise BackendError(f"网络错误（请检查 NAS 出网/DNS/代理设置）：{exc}") from exc
        changed = False
        for name, value in resp.cookies.items():
            if value == "":
                if jar.pop(name, None) is not None:
                    changed = True        # 删除型 Set-Cookie
            elif jar.get(name) != value:
                jar[name] = value
                changed = True
        if changed:
            self.conf["review_cookies"] = jar
            self._save()
        return resp

    @staticmethod
    def _review_jsonp(text: str) -> dict:
        """JSONP 响应兜底解析：callback({...}) → dict（无 callback 时是纯 JSON）。"""
        m = re.search(r"\((\{.*\})\)\s*;?\s*$", text or "", re.S)
        if not m:
            return {}
        try:
            data = json.loads(m.group(1))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _merge_review_result(self, resp: httpx.Response, *,
                             cookies_first: bool) -> dict:
        """结果 = JSON 体 ∪ Set-Cookie 双通道合并。

        （blogresult/securitytoken/token/creditkey/error 全在 Set-Cookie）。"""
        data: dict = {}
        try:
            body = resp.json()
            if isinstance(body, dict):
                data = body
        except ValueError:
            data = self._review_jsonp(resp.text)
        for name, value in resp.cookies.items():
            if cookies_first and value != "":
                data[name] = value            # 页面以 cookie 为准
            else:
                data.setdefault(name, value)
        # 结果型 cookie，不留在罐里回带给下次请求
        jar = self.conf.get("review_cookies") or {}
        dropped = False
        for stale in ("blogresult", "error"):
            if jar.pop(stale, None) is not None:
                dropped = True
        if dropped:
            self._save()
        return data

    def _review_get(self, path: str, params: dict) -> dict:
        """GET+JSONP：参数拼查询串，附 csrf_token。"""
        q = dict(params)
        seed = (self._review_cookies().get("deviceid") or self._device_id())[:32]
        q.setdefault("csrf_token", _md5_hex(seed))
        resp = self._review_http("GET", self._review_base() + path, params=q)
        return self._merge_review_result(resp, cookies_first=False)

    def _review_post_form(self, path: str, params: dict) -> dict:
        """multipart 表单 POST（每个参数一个表单域，不带 csrf_token）。"""
        files = {k: (None, str(v)) for k, v in params.items()}
        resp = self._review_http("POST", self._review_base() + path, files=files)
        return self._merge_review_result(resp, cookies_first=True)

    # ------------------------------------------------ 会话失效（errorCode=13）自动恢复

    @staticmethod
    def _err13(data: dict) -> bool:
        """会话失效：errorCode=13 或错误名 userinfo_expired（校验结果在 cookie 里，error cookie 给的是错误名而不是数字码）。"""
        code = str(data.get("errorCode") or data.get("error_code") or "")
        return (code == "13"
                or (data.get("error") or "").strip() == "userinfo_expired")

    def _review_with_retry(self, kind: str, extra: dict | None = None, *,
                           retry_refresh: bool = True) -> dict:
        """一次点击内自动恢复会话失效（errorCode=13 / userinfo_expired）：

        ① 响应续期了 creditkey → 换新键原地重试（响应的 creditkey 会续期，用户再点一次就用新键）；
        ② 仍 13 → 重登刷新验证会话后重试（send）/ 提示重新获取短信（check）；
        ③ 仍 13 → 明确报「刷新后的新会话仍被拒」。"""
        renewed = refreshed = False
        for _ in range(3):
            if kind == "send":
                data = self._review_get("/xluser.core.login/v3/sendsms",
                                        self._review_payload())
            else:
                data = self._review_post_form(
                    "/xluser.core.login/v3/checksms",
                    {**self._review_payload(), **(extra or {})})
            if not self._err13(data):
                return data
            new_key = (data.get("creditkey") or "").strip()
            if new_key and new_key != self._review_creditkey() and not renewed:
                renewed = True
                self._set_review_key(new_key)
                continue
            if not refreshed:
                refreshed = True
                self._refresh_review_session()
                if not retry_refresh:
                    raise BackendError(
                        "验证码提交失败：验证会话已失效，已自动刷新——"
                        "原短信验证码随旧会话失效，请重新点「获取验证码」")
                continue
            tail = ("请点「重新登录」获取新的验证会话后重试，或用迅雷 App "
                    "内置浏览器打开备用验证链接完成验证" if kind == "send"
                    else "请重新点「获取验证码」获取新短信")
            raise BackendError("验证会话刷新后仍被迅雷拒绝"
                               f"（error_code=13，身份信息已失效）——{tail}")
        raise BackendError("验证请求失败：自动重试次数超限")

    def _refresh_review_session(self) -> None:
        """验证会话失效（errorCode=13）时重登一次，刷新 review_url/creditkey。

        重登会再次触发 review_panel，_review_message 顺带更新验证会话；
        若重登直接成功（其实已验证过），登录态也就绪了。"""
        username = (self.conf.get("login_name") or "").strip()
        password = self.conf.get("password") or ""
        if not username or not password:
            raise BackendError("验证会话已失效，且未配置迅雷账号密码，无法刷新——"
                               "请先在上方填写账号密码")
        try:
            self._core_login(username, password)
        except BackendError as exc:
            if "review_panel" in str(exc):
                return  # 已刷新为新验证会话
            raise

    def review_send_sms(self) -> dict:
        """获取短信验证码（GET+JSONP）。"""
        state = self.review_state()
        if not state["active"]:
            raise BackendError("当前没有待完成的短信验证（请先点「重新登录」触发）")
        data = self._review_with_retry("send")
        code = str(data.get("errorCode") or data.get("error_code") or "0")
        if code in ("1007", "6"):
            raise BackendError("该次验证需图形验证，短信通道不可用——"
                               "请用迅雷 App 内置浏览器打开备用验证链接完成")
        if code != "0":
            raise BackendError(_err_text("获取验证码失败", data))
        token = (data.get("token") or "").strip()
        if not token:
            raise BackendError("获取验证码失败：响应缺少 token")
        # （每次响应都可能续期，后续请求换新键）
        if (data.get("creditkey") or "").strip():
            self._set_review_key(data["creditkey"].strip())
        self.conf["review_token"] = token
        self._save()
        return {"ok": True, "mobile": state["mobile"]}

    def _review_report(self, data: dict) -> str:
        """checksms 成功后上报（GET+JSONP）。

        响应 code==200 时回传续期信任密钥；上报失败不影响验证结果。"""
        q = {k: v[0] for k, v in
             parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()}
        jar = self._review_cookies()
        params = {
            "type": "sms", "scene": "34", "userid": q.get("userID", ""),
            "data": data.get("securitytoken", ""),
            "result": data.get("blogresult", ""), "appid": q.get("appid", ""),
            "deviceid": jar.get("deviceid") or self._device_id(),
            "creditkey": jar.get("creditkey") or self._review_creditkey(),
        }
        try:
            r = self._review_get("/credit/v1/report", params)
        except BackendError:
            return ""
        if str(r.get("code") or "") == "200":
            return (r.get("creditkey") or "").strip()
        return ""

    def review_check_sms(self, sms_code: str) -> dict:
        """提交短信验证码（multipart 表单 POST，
        结果在 Set-Cookie；成功后走 /credit/v1/report 续期信任密钥）。"""
        state = self.review_state()
        if not state["active"]:
            raise BackendError("当前没有待完成的短信验证（请先点「重新登录」触发）")
        token = (self.conf.get("review_token") or "").strip()
        code = (sms_code or "").strip()
        if not token:
            raise BackendError("请先点「获取验证码」")
        if not re.fullmatch(r"\d{6}", code):
            raise BackendError("请输入 6 位数字验证码")
        data = self._review_with_retry("check", {"token": token, "smsCode": code},
                                       retry_refresh=False)
        if str(data.get("blogresult") or "") != "0":
            raise BackendError(_err_text("验证码提交失败", data))
        # 验证通过：优先用上报接口的续期密钥，其次响应里的
        renewed = self._review_report(data)
        if renewed:
            self.conf["creditkey"] = renewed
        elif (data.get("creditkey") or "").strip():
            self.conf["creditkey"] = data["creditkey"].strip()
        else:
            # 两处都没有续期值：沿用本轮验证会话的密钥重登（页面场景宿主
            # 拿不到续期值时同样带原 key 继续），空凭据重登等于没验证
            self.conf["creditkey"] = self._review_creditkey()
        self.conf["review_url"] = ""
        self.conf["review_token"] = ""
        self.conf["review_key"] = ""
        self._save()
        # 回传续期后的信任密钥：UI 先同步到表单再触发重新登录，
        # 避免登录链的“先保存表单”把续期值又冲掉。
        return {"ok": True, "detail": "验证通过，正在重新登录…",
                "creditkey": self.conf.get("creditkey", "")}

    def login(self) -> None:
        username = (self.conf.get("login_name") or "").strip()
        password = self.conf.get("password") or ""
        if not username or not password:
            raise BackendError("未配置迅雷账号（手机号/密码），无法使用远程通道")
        session_id = self._core_login(username, password)
        self._refresh_captcha_in_login("POST:/v1/auth/signin/token", username)
        body = {
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "provider": SIGN_PROVIDER, "signin_token": session_id,
        }
        data = self._request_json("POST", XLUSER_API + "/auth/signin/token",
                                  json=body, headers=self._base_headers(),
                                  timeout=30)
        if not data.get("access_token"):
            if _is_error(data):
                raise BackendError(_err_text("换取令牌失败", data))
            raise BackendError("换取令牌失败：响应无 access_token")
        self._apply_token(data)
        # 登录成功，信任密钥已消费（清空避免复用过期值）
        self.conf["creditkey"] = ""
        self.conf["review_url"] = ""
        self.conf["review_token"] = ""
        self.conf["review_key"] = ""
        self.conf["review_cookies"] = {}
        self._save()

    def _apply_token(self, data: dict) -> None:
        self.conf["access_token"] = data.get("access_token", "")
        self.conf["token_type"] = data.get("token_type") or "Bearer"
        self.conf["refresh_token"] = data.get("refresh_token") or self.conf.get("refresh_token", "")
        # user_id 多来源：signin/token 的 user_id > sub > v3 登录已存的 userID
        self.conf["user_id"] = (data.get("user_id") or data.get("sub")
                                or self.conf.get("user_id", ""))

    def _refresh_access_token(self) -> None:
        refresh = self.conf.get("refresh_token") or ""
        if not refresh:
            self.login()
            return
        body = {"grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
        data = self._request_json("POST", XLUSER_API + "/auth/token",
                                  json=body, headers=self._base_headers(),
                                  timeout=30)
        if not data.get("access_token"):
            self.login()   # 刷新失败回退账号密码登录
            return
        self._apply_token(data)
        self._save()

    # ------------------------------------------------------------ 目标设备

    def _target_of(self) -> str:
        """目标设备空间：配置值优先（0.1.28 回退老行为）——0.1.26 加的
        「不在在线列表就自动改发到 online[0]」在多设备/发现口径不一致的场景
        会把任务发去别的设备空间（用户实测「派发成功但迅雷里面没有」）。
        配置值疑似失效时 status() 会提示，但不擅自改道。未配置时才用
        发现到的首个在线设备。"""
        conf_t = (self.conf.get("target") or "").strip()
        if conf_t:
            return conf_t
        if self._target:
            return self._target
        try:
            online = [t["target"] for t in self.core.device_targets()]
        except BackendError:
            online = []
        if online:
            self._target = online[0]
            return self._target
        raise BackendError("当前迅雷账号下未发现远程下载设备（请先在迅雷 App 中绑定远程设备，"
                           "或在设置里填写「目标设备」）")

    # ------------------------------------------------------------ 接口

    def _require_token(self) -> None:
        if not self.conf.get("access_token"):
            self.login()

    def status(self) -> dict:
        conf_ok = bool((self.conf.get("login_name") or "").strip() and
                       (self.conf.get("password") or "")) or bool(self.conf.get("refresh_token"))
        if not conf_ok:
            return {"configured": False, "reachable": False,
                    "detail": "未配置迅雷账号（需手机号与密码，或已保存的登录态）"}
        try:
            self._require_token()
            data = self._request_json("GET", XLUSER_API + "/user/me",
                                      headers=self._auth_headers(), timeout=15)
            if _err_code(data) in TOKEN_ERROR_CODES:
                self._refresh_access_token()
                data = self._request_json("GET", XLUSER_API + "/user/me",
                                          headers=self._auth_headers(), timeout=15)
            if _is_error(data):
                return {"configured": True, "reachable": False,
                        "detail": _err_text("登录态校验失败", data)}
            targets = self.core.device_targets()
            detail = f"已登录，远程设备 {len(targets)} 台"
            conf_t = (self.conf.get("target") or "").strip()
            online = [t["target"] for t in targets]
            if conf_t and online and conf_t not in online:
                detail += (f"；⚠ 配置的目标设备 {conf_t[:12]}… 已不在线，"
                           f"任务将改发到 {online[0][:12]}…（可在设置里更新目标设备）")
            return {"configured": True, "reachable": bool(targets), "detail": detail}
        except BackendError as exc:
            return {"configured": True, "reachable": False, "detail": str(exc)}

    def parse(self, url: str) -> ParsedResource:
        self._require_token()
        return self.core.parse_resource(url)

    def add(self, url: str, *, name: str | None = None,
            directory: str | None = None) -> str:
        self._require_token()
        target = self._target_of()
        resource = self.core.parse_resource(url)
        if name:
            resource.name = name
        return self.core.create_task(url, resource, target,
                                     directory=directory or (self.conf.get("dir") or ""))

    def list_tasks(self) -> list[TaskView]:
        self._require_token()
        target = self._target_of()
        return [task_to_view(self.name, t) for t in self.core.list_tasks(target)]

    def pause(self, task_id: str) -> None:
        self._require_token()
        self.core.set_phase(self._target_of(), "user#download-url", task_id, "pause")

    def resume(self, task_id: str) -> None:
        self._require_token()
        self.core.set_phase(self._target_of(), "user#download-url", task_id, "running")

    def delete(self, task_id: str) -> None:
        self._require_token()
        self.core.delete_task(self._target_of(), task_id)
