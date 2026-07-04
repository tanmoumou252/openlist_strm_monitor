from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import struct
import os
import time
from urllib.parse import unquote
from pathlib import Path
from typing import Any
import requests
from lxml import etree

from openlist_login_shared import parse_login_error

log = logging.getLogger(__name__)


def _normalize_host(host: str) -> str:
    """规范化 host URL：移除末尾斜杠和 /dav 后缀。"""
    host = host.rstrip("/")
    if host.endswith("/dav"):
        host = host[:-4]
    return host


def _generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    """生成 6 位 TOTP 码（RFC 6238）

    Raises:
        ValueError: secret 为空，或既不是合法的 base32 也不是合法的 base64。
    """
    if not secret:
        raise ValueError("TOTP Secret 不能为空")
    timestamp = int(time.time() // interval)
    timestamp_bytes = timestamp.to_bytes(8, byteorder="big")

    secret_bytes: bytes | None = None
    # 尝试 base32（TOTP 标准编码）
    try:
        padded = secret.upper() + "=" * ((8 - len(secret) % 8) % 8)
        secret_bytes = base64.b32decode(padded)
    except (binascii.Error, ValueError):
        pass

    # 回退到 base64
    if secret_bytes is None:
        try:
            secret_bytes = base64.b64decode(secret, validate=True)
        except (binascii.Error, ValueError):
            secret_bytes = None

    if secret_bytes is None:
        raise ValueError(
            "TOTP Secret 编码无效：既不是合法的 base32 也不是合法的 base64，请检查配置"
        )

    hmac_hash = hmac.new(secret_bytes, timestamp_bytes, hashlib.sha1).digest()
    offset = hmac_hash[-1] & 0x0F
    truncated_hash = hmac_hash[offset : offset + 4]
    otp_int = int.from_bytes(truncated_hash, byteorder="big") & 0x7FFFFFFF
    otp = otp_int % (10**digits)
    return f"{otp:0{digits}d}"


class OpenListAdminClient:
    """OpenList Admin API 客户端（JWT 复用增强版）"""

    def __init__(self, host: str, user: str = "", password: str = "", totp_secret: str = ""):
        self.host = _normalize_host(host)
        self.user = user
        self.password = password
        self.totp_secret = totp_secret
        self.token: str | None = None
        self.session = requests.Session()
        self._fs_list_logged: set[str] = set()
        self._fs_list_logged_time: float = 0.0  # 上次清理时间

        # 最近一次登录的错误详情（调用方可通过属性访问）
        self.last_error_message: str | None = None
        self.last_error_type: str | None = None

        # Token 缓存文件路径 (存放在脚本同目录)
        self.token_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".admin_token.json")
        self._load_token_from_cache()

    # ================= 内部辅助方法 =================

    def _load_token_from_cache(self) -> None:
        """从文件加载缓存的 Token"""
        if os.path.exists(self.token_cache_path):
            try:
                with open(self.token_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.token = data.get("token")
                    log.debug("已从本地缓存加载 JWT Token")
            except Exception:
                self.token = None

    def _save_token_to_cache(self, token: str) -> None:
        """将 Token 保存到本地文件"""
        self.token = token
        try:
            with open(self.token_cache_path, "w", encoding="utf-8") as f:
                json.dump({"token": token, "ts": time.time()}, f)
        except Exception as e:
            log.warning(f"无法保存 Token 缓存: {e}")

    def login(self, force: bool = False) -> bool:
        """登录获取 JWT。force=True 会无视缓存强制联网登录。
        
        返回: bool（True 成功，False 失败）
        错误详情通过属性访问:
          - self.last_error_type: "wrong_password", "wrong_2fa", "account_not_found",
                                   "network_error", "token_missing", "unknown", None(成功)
          - self.last_error_message: 具体错误消息字符串，成功时为 None
        """
        if not force and self.token:
            self.last_error_message = None
            self.last_error_type = None
            return True

        try:
            otp_code = _generate_totp(self.totp_secret) if self.totp_secret else None
        except ValueError as e:
            log.error("TOTP 生成失败，请检查 TOTP Secret 配置: %s", e)
            self.last_error_message = f"TOTP Secret 无效: {e}"
            self.last_error_type = "invalid_totp"
            return False

        url = f"{self.host}/api/auth/login"
        payload = {"username": self.user, "password": self.password, "otp_code": otp_code}

        try:
            # 登录是唯一不走 _do_request 的方法，避免死循环
            res = self.session.post(url, json=payload, timeout=10)
            
            # 检查 HTTP 状态码
            if res.status_code != 200:
                # 尝试解析错误消息
                try:
                    error_data = res.json()
                    error_msg = error_data.get("message", "")
                    error_type = self._parse_login_error(error_msg)
                    log.error("登录失败 (HTTP %s): %s", res.status_code, error_msg)
                    self.last_error_message = error_msg
                    self.last_error_type = error_type
                    return False
                except Exception:
                    log.error("登录失败 (HTTP %s)，无法解析错误信息", res.status_code)
                    self.last_error_message = f"HTTP {res.status_code}"
                    self.last_error_type = "unknown"
                    return False
            
            # HTTP 200，检查业务响应
            data = res.json()
            token = data.get("data", {}).get("token")
            if not token:
                token = data.get("token")
            if not token:
                # 检查是否有业务错误码
                error_msg = data.get("message", "")
                error_type = self._parse_login_error(error_msg) if error_msg else "unknown"
                log.error("登录响应中未获取到有效 Token: %s", data)
                self.last_error_message = error_msg or "无法提取 token"
                self.last_error_type = error_type
                return False
            
            self._save_token_to_cache(token)
            self.last_error_message = None
            self.last_error_type = None
            log.info("OpenList 登录成功，Token 已更新")
            return True
        except requests.exceptions.RequestException as e:
            log.error(f"登录网络请求失败: {e}")
            self.last_error_message = str(e)
            self.last_error_type = "network_error"
            return False
        except Exception as e:
            log.error(f"登录请求失败: {e}")
            self.last_error_message = str(e)
            self.last_error_type = "unknown"
            return False
    
    def _parse_login_error(self, message: str) -> str:
        """解析 OpenList 登录错误消息，返回错误类型。
        
        常见错误消息:
        - "username or password is wrong" → wrong_password
        - "otp code is wrong" → wrong_2fa
        - "user not found" → account_not_found
        """
        return parse_login_error(message)

    def _do_request(self, method: str, url: str, **kwargs) -> requests.Response | None:
        """统一请求包装器：注入 Token、自动处理 401 过期重试"""
        if not self.token:
            if not self.login():
                return None

        # 注入 Header
        headers = kwargs.get("headers", {})
        # OpenList 的 JWT 认证不需要 "Bearer" 前缀，直接使用 token
        headers["Authorization"] = self.token
        headers["Content-Type"] = "application/json"
        kwargs["headers"] = headers

        try:
            # 提取请求对象摘要（json payload 或 params），方便定位是哪个文件/路径
            req_summary = ""
            json_payload = kwargs.get("json")
            if json_payload and isinstance(json_payload, dict):
                # 取 path/dir/src_dir/names 等关键字段
                keys_of_interest = ("path", "dir", "src_dir", "dst_dir", "names")
                summary_parts = [f"{k}={json_payload[k]}" for k in keys_of_interest if k in json_payload]
                if summary_parts:
                    req_summary = " | " + ", ".join(summary_parts)
            params = kwargs.get("params")
            if params and isinstance(params, dict):
                req_summary = " | params=" + str(params)
            # /api/fs/list 日志精简：只输出一次路径扫描
            _is_fs_list = url.endswith("/api/fs/list")
            _fs_path = ""
            if _is_fs_list and isinstance(json_payload, dict):
                _fs_path = str(json_payload.get("path", "")).strip()

            if _is_fs_list and _fs_path:
                # 每 10 分钟清理一次日志缓存，防止无限增长
                now = time.time()
                if now - self._fs_list_logged_time > 600:  # 10 minutes
                    self._fs_list_logged.clear()
                    self._fs_list_logged_time = now
                
                if _fs_path not in self._fs_list_logged:
                    self._fs_list_logged.add(_fs_path)
                    log.debug("[STRM] 扫描 %s", _fs_path)
            else:
                log.debug("[API请求] %s %s%s", method, url, req_summary)

            res = self.session.request(method, url, **kwargs)

            if not _is_fs_list:
                log.debug("[API响应] 状态码=%s", res.status_code)

            # 检查是否过期：HTTP 401 或业务 JSON code 401
            should_retry = res.status_code == 401
            if not should_retry:
                try:
                    if res.json().get("code") == 401:
                        should_retry = True
                except (ValueError, KeyError, AttributeError):
                    pass

            if should_retry:
                log.warning("Token 已过期，尝试自动重新登录...")
                if self.login(force=True):
                    kwargs["headers"]["Authorization"] = self.token
                    res = self.session.request(method, url, **kwargs)
                    if not _is_fs_list:
                        log.debug("[API重试] 重新登录后状态码=%s", res.status_code)
                else:
                    log.error("[API重试] 重新登录失败: %s", self.last_error_message or "未知错误")
                    return res  # 登录失败，直接返回 401 结果

            # 记录响应摘要（避免记录大响应体）
            try:
                if res.status_code == 200:
                    response_json = res.json()
                    code = response_json.get('code', 'N/A')
                    message = response_json.get('message', '')
                    if not _is_fs_list:
                        log.debug("[API结果] 业务码=%s, 消息=%s", code, message)
            except (ValueError, KeyError, AttributeError):
                if not _is_fs_list:
                    log.debug("[API结果] 响应非JSON格式")

            return res
        except Exception as e:
            log.error(f"网络请求异常 ({url}): {e}")
            return None

    # ================= 业务方法 (全量补全) =================

    # 1. 获取存储列表 (Admin API)
    def list_storages(self, page: int = 1, per_page: int = 1000) -> dict[str, Any] | None:
        """获取全部存储列表（自动分页聚合）。

        返回结构与 OpenList API 一致：
            {"code": 200, "data": {"content": [...], "total": N}}
        如果任意分页请求失败，返回 None。
        """
        url = f"{self.host}/api/admin/storage/list"
        aggregated: list[dict[str, Any]] = []
        total: int | None = None
        current_page = page

        while True:
            res = self._do_request(
                "GET", url,
                params={"page": current_page, "per_page": per_page},
                timeout=10,
            )
            if not res or res.status_code != 200:
                return None
            try:
                payload = res.json()
            except ValueError:
                log.error("list_storages: 响应非 JSON 格式")
                return None

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            content = data.get("content", []) if isinstance(data, dict) else []
            if not isinstance(content, list):
                content = []

            aggregated.extend(content)

            # 计算总数与已拉取数量，决定是否继续翻页
            if total is None:
                try:
                    total = int(data.get("total", len(content)))
                except (TypeError, ValueError):
                    total = len(content)

            if len(aggregated) >= total or len(content) < per_page:
                # 构造聚合后的返回结构，保持与单次调用一致
                return {
                    "code": payload.get("code", 200),
                    "message": payload.get("message", ""),
                    "data": {"content": aggregated, "total": total},
                }

            current_page += 1
            if current_page > 100:  # 安全阀：防止异常响应导致死循环
                log.error("list_storages: 分页超过 100 页，强制终止")
                return {
                    "code": payload.get("code", 200),
                    "message": payload.get("message", ""),
                    "data": {"content": aggregated, "total": total},
                }

    # 2. 获取存储详情 (Admin API) - 【补全】
    def get_storage_info(self, storage_id: int) -> dict[str, Any] | None:
        url = f"{self.host}/api/admin/storage/get"
        res = self._do_request("GET", url, params={"id": storage_id}, timeout=10)
        if res and res.status_code == 200:
            try:
                return res.json()
            except (ValueError, KeyError, AttributeError):
                log.error("get_storage_info: 响应非 JSON 格式")
        return None

    # 3. 列出目录 (FS API)
    def list_directory(self, path: str = "/", **kwargs) -> dict[str, Any] | None:
        url = f"{self.host}/api/fs/list"
        payload = {
            "path": path,
            "password": kwargs.get("password", ""),
            "refresh": kwargs.get("refresh", False),
            "page": kwargs.get("page", 1),
            "per_page": kwargs.get("per_page", 30),
        }
        res = self._do_request("POST", url, json=payload, timeout=15)
        if res and res.status_code == 200:
            try:
                return res.json()
            except (ValueError, KeyError, AttributeError):
                log.error("list_directory: 响应非 JSON 格式 (path=%s)", path)
        return None

    # 4. 创建目录 (FS API)
    def mkdir(self, path: str) -> bool:
        """创建目录。
        
        Returns:
            True: 目录创建成功，或目录已存在（幂等）。
            False: 请求失败或真正的创建失败。
        """
        url = f"{self.host}/api/fs/mkdir"
        log.debug("[建目录] path=%s", path)
        res = self._do_request("POST", url, json={"path": path}, timeout=15)
        if res is None:
            log.debug("[建目录] 请求失败: res=None")
            return False
        if res.status_code != 200:
            log.warning("[建目录] 请求非200: status=%s", res.status_code)
            return False
        try:
            data = res.json()
        except (ValueError, KeyError, AttributeError):
            log.error("[建目录] 响应非 JSON: %s", getattr(res, "text", "")[:500])
            return False
        code = data.get("code")
        if self._is_success_code(code):
            log.debug("[建目录] 创建成功: %s", path)
            return True
        # 目录已存在视为成功（幂等），但其他业务错误视为失败
        message = str(data.get("message", "")).lower()
        if "exist" in message or "already" in message or code == 403:
            log.debug("[建目录] 目录已存在，视为成功: %s", path)
            return True
        log.warning("[建目录] 业务失败: code=%s message=%s (path=%s)", code, data.get("message", ""), path)
        return False

    def _is_success_code(self, code: int) -> bool:
        """判断业务 code 是否为成功值"""
        return code in (0, 200)

    # 5. 移动文件 (FS API)
    def move(self, src: str, dst: str) -> bool:
        url = f"{self.host}/api/fs/move"
        payload = {
            "src_dir": os.path.dirname(src),
            "dst_dir": os.path.dirname(dst),
            "names": [os.path.basename(src)],
        }
        log.debug("[移动] %s -> %s", src, dst)
        res = self._do_request("POST", url, json=payload, timeout=30)
        if res is None:
            log.debug("[移动] 请求失败: res=None")
            return False
        log.debug("[移动] 响应: status=%s body=%s", res.status_code, getattr(res, "text", "")[:500])
        if res.status_code != 200:
            log.warning("[移动] 请求非200: status=%s", res.status_code)
            return False
        try:
            data = res.json()
        except Exception:
            log.error("[移动] 响应非 JSON: %s", getattr(res, "text", "")[:500])
            return False
        code = data.get("code")
        if not self._is_success_code(code):
            log.error("[移动] 业务失败: code=%s message=%s", code, data.get("message", ""))
            return False
        log.debug("[移动] 成功")
        return True

    # 6. 删除文件 (FS API)
    def remove(self, path: str) -> bool:
        url = f'{self.host}/api/fs/remove'
        payload = {
            'dir': os.path.dirname(path),
            'names': [os.path.basename(path)],
        }
        log.debug('[删除] %s', path)
        res = self._do_request('POST', url, json=payload, timeout=30)
        if res is None:
            log.debug('[删除] 请求失败: res=None')
            return False
        log.debug('[删除] 响应: status=%s body=%s', res.status_code, getattr(res, 'text', '')[:500])
        if res.status_code != 200:
            log.warning('[删除] 请求非200: status=%s', res.status_code)
            return False
        try:
            data = res.json()
        except Exception:
            log.error('[删除] 响应非 JSON: %s', getattr(res, 'text', '')[:500])
            return False
        code = data.get('code')
        if not self._is_success_code(code):
            log.error('[删除] 业务失败: code=%s message=%s', code, data.get('message', ''))
            return False
        log.debug('[删除] 成功')
        return True

    # 7. 检查路径是否存在 (逻辑方法)
    def check_exists(self, path: str) -> bool:
        """检查文件或目录是否存在。
        
        支持大目录（>1000 项）的分页搜索。
        """
        if not path or path == "/":
            res = self.list_directory("/", per_page=1)
            return res is not None and res.get("code") in (0, 200)

        path = path.rstrip("/")
        parts = path.split("/")
        parent_path = "/".join(parts[:-1]) or "/"
        target_name = parts[-1]

        try:
            page = 1
            per_page = 1000
            max_pages = 100  # 安全阀：防止异常响应导致死循环
            
            while page <= max_pages:
                result = self.list_directory(parent_path, page=page, per_page=per_page)
                if not result or result.get("code") not in (0, 200):
                    return False

                data = result.get("data", {})
                content = data.get("content", []) if isinstance(data, dict) else []
                
                # 检查当前页是否包含目标
                for item in content:
                    if isinstance(item, dict) and item.get("name") == target_name:
                        return True
                
                # 如果当前页不满，说明已无更多数据
                if len(content) < per_page:
                    return False
                
                # 检查总数，如果已遍历完所有项
                try:
                    total = int(data.get("total", 0))
                    if page * per_page >= total:
                        return False
                except (TypeError, ValueError):
                    pass
                
                page += 1
            
            log.warning("check_exists: 分页搜索超过 %d 页，强制终止: %s", max_pages, path)
            return False
        except Exception as e:
            log.error("check_exists 异常: %s - %s", path, e)
            return False

    # 8. 获取兼容格式的内容列表 (逻辑方法)
    def list_contents(self, path: str) -> dict[str, list[dict[str, Any]]] | None:
        """列出目录内容，返回兼容格式。
        
        Returns:
            dict: {"folders": [...], "files": [...]} 成功时返回。
            None: 目录不存在或请求失败时返回。
        """
        result = self.list_directory(path)
        if result is None or result.get("code") not in (0, 200):
            return None
        data = result.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []
        if content is None:
            content = []
        folders, files = [], []
        for item in content:
            if isinstance(item, dict):
                name = item.get("name", "")
                if item.get("is_dir", False):
                    folders.append({"name": name, "is_dir": True, "size": 0})
                else:
                    files.append({"name": name, "is_dir": False, "size": item.get("size", 0)})
        return {"folders": folders, "files": files}

    # 9. 触发索引更新 (逻辑方法)
    def trigger_refresh_via_fs_list(self, paths: list[str]) -> bool:
        for path in paths:
            result = self.list_directory(path, refresh=True)
            if result is None:
                return False
        return True


class OpenlistWebDAV:
    DEFAULT_TIMEOUT = 30

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        totp_secret: str = "",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.host = host.rstrip("/")
        if self.host.endswith("/dav"):
            self.host = self.host[:-4]
        self.user = user
        self.password = password
        self.totp_secret = totp_secret
        self.timeout = timeout
        self.session = requests.Session()

    def _generate_totp(self) -> str:
        return _generate_totp(self.totp_secret)

    def _request(
        self,
        method: str,
        path: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        # 确保 path 以 / 开头，避免拼接错误
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self.host}/dav{path}"
        auth = (self.user, self.password)
        all_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*",
        }
        if headers:
            all_headers.update(headers)
        if self.totp_secret:
            all_headers["X-TOTP-Code"] = self._generate_totp()
        res = self.session.request(
            method,
            url,
            data=data,
            headers=all_headers,
            auth=auth,
            timeout=self.timeout,
            stream=stream,
        )
        res.raise_for_status()
        return res

    def check_exists(self, path: str) -> bool:
        try:
            res = self._request("HEAD", path)
            return res.status_code == 200
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return False
            raise

    def list_contents(self, path: str) -> dict | str:
        try:
            res = self._request(
                "PROPFIND",
                path,
                headers={"Depth": "1", "Content-Type": "text/xml; charset=utf-8"},
            )
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return {"code": 404, "message": "路径不存在"}
            raise
        if res.status_code == 207:
            root = etree.fromstring(res.content)
            ns = {"d": "DAV:"}
            items = []
            for resp in root.findall(".//d:response", ns):
                href = resp.findtext("d:href", "", ns)
                prop = resp.find(".//d:propstat/d:prop", ns)
                if prop is not None:
                    is_dir = prop.find("d:resourcetype/d:collection", ns) is not None
                    size = prop.findtext("d:getcontentlength", "0", ns)
                    items.append(
                        {
                            "name": unquote(Path(href).name),  # ← 解码 URL 编码
                            "is_dir": is_dir,
                            "size": int(size) if not is_dir else 0,
                        }
                    )
            return {"folders": [i for i in items if i["is_dir"]], "files": [i for i in items if not i["is_dir"]]}
        return {"code": res.status_code, "message": "未知错误"}

    def read_file(self, path: str) -> str:
        res = self._request("GET", path)
        return res.text

    def write_file(self, path: str, content: str) -> None:
        self._request("PUT", path, data=content.encode("utf-8"))

    def delete_file(self, path: str) -> None:
        self._request("DELETE", path)

    def mkdir(self, path: str) -> None:
        try:
            self._request("MKCOL", path)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code != 405:
                raise

    def move(self, src: str, dst: str) -> None:
        self._request("MOVE", src, headers={"Destination": f"{self.host}/dav{dst}"})

    def copy(self, src: str, dst: str) -> None:
        self._request("COPY", src, headers={"Destination": f"{self.host}/dav{dst}"})
