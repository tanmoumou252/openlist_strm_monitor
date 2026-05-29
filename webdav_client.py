import base64
import hashlib
import hmac
import json
import logging
import struct
import os
import time
from urllib.parse import unquote
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple, Union
import requests
from lxml import etree

log = logging.getLogger(__name__)


def _generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    """生成 6 位 TOTP 码（RFC 6238）"""
    if not secret:
        raise ValueError("TOTP Secret 不能为空")
    timestamp = int(time.time() // interval)
    timestamp_bytes = timestamp.to_bytes(8, byteorder="big")
    try:
        secret_bytes = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
    except Exception:
        secret_bytes = base64.b64decode(secret)
    hmac_hash = hmac.new(secret_bytes, timestamp_bytes, hashlib.sha1).digest()
    offset = hmac_hash[-1] & 0x0F
    truncated_hash = hmac_hash[offset : offset + 4]
    otp_int = int.from_bytes(truncated_hash, byteorder="big") & 0x7FFFFFFF
    otp = otp_int % (10**digits)
    return f"{otp:0{digits}d}"


class OpenListAdminClient:
    """OpenList Admin API 客户端（JWT 复用增强版）"""

    def __init__(self, host: str, user: str = "", password: str = "", totp_secret: str = ""):
        self.host = host.rstrip("/")
        if self.host.endswith("/dav"):
            self.host = self.host[:-4]
        self.user = user
        self.password = password
        self.totp_secret = totp_secret
        self.token: Optional[str] = None
        self.session = requests.Session()

        # Token 缓存文件路径 (存放在脚本同目录)
        self.token_cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".admin_token.json")
        self._load_token_from_cache()

    # ================= 内部辅助方法 =================

    def _load_token_from_cache(self):
        """从文件加载缓存的 Token"""
        if os.path.exists(self.token_cache_path):
            try:
                with open(self.token_cache_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.token = data.get("token")
                    log.debug("🔑 已从本地缓存加载 JWT Token")
            except Exception:
                self.token = None

    def _save_token_to_cache(self, token: str):
        """将 Token 保存到本地文件"""
        self.token = token
        try:
            with open(self.token_cache_path, "w", encoding="utf-8") as f:
                json.dump({"token": token, "ts": time.time()}, f)
        except Exception as e:
            log.warning(f"💾 无法保存 Token 缓存: {e}")

    def login(self, force: bool = False) -> bool:
        """登录获取 JWT。force=True 会无视缓存强制联网登录"""
        if not force and self.token:
            return True

        otp_code = _generate_totp(self.totp_secret) if self.totp_secret else None
        url = f"{self.host}/api/auth/login"
        payload = {"username": self.user, "password": self.password, "otp_code": otp_code}

        try:
            # 登录是唯一不走 _do_request 的方法，避免死循环
            res = self.session.post(url, json=payload, timeout=10)
            res.raise_for_status()
            data = res.json()
            token = data.get("data", {}).get("token") or data.get("token")
            if token:
                self._save_token_to_cache(token)
                log.info("🔓 OpenList 登录成功，Token 已更新")
                return True
            log.error(f"❌ 登录响应异常: {data}")
            return False
        except Exception as e:
            log.error(f"❌ 登录请求失败: {e}")
            return False

    def _do_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """统一请求包装器：注入 Token、自动处理 401 过期重试"""
        if not self.token:
            if not self.login():
                return None

        # 注入 Header
        headers = kwargs.get("headers", {})
        headers["Authorization"] = self.token
        headers["Content-Type"] = "application/json"
        kwargs["headers"] = headers

        try:
            res = self.session.request(method, url, **kwargs)

            # 检查是否过期：HTTP 401 或业务 JSON code 401
            should_retry = res.status_code == 401
            if not should_retry:
                try:
                    if res.json().get("code") == 401:
                        should_retry = True
                except:
                    pass

            if should_retry:
                log.warning("⚠️ Token 已过期，尝试自动重新登录...")
                if self.login(force=True):
                    kwargs["headers"]["Authorization"] = self.token
                    res = self.session.request(method, url, **kwargs)
                else:
                    return res  # 登录失败，直接返回 401 结果

            return res
        except Exception as e:
            log.error(f"📡 网络请求异常 ({url}): {e}")
            return None

    # ================= 业务方法 (全量补全) =================

    # 1. 获取存储列表 (Admin API)
    def list_storages(self, page: int = 1, per_page: int = 30) -> Optional[Dict[str, Any]]:
        url = f"{self.host}/api/admin/storage/list"
        res = self._do_request("GET", url, params={"page": page, "per_page": per_page}, timeout=10)
        return res.json() if res and res.status_code == 200 else None

    # 2. 获取存储详情 (Admin API) - 【补全】
    def get_storage_info(self, storage_id: int) -> Optional[Dict[str, Any]]:
        url = f"{self.host}/api/admin/storage/get"
        res = self._do_request("GET", url, params={"id": storage_id}, timeout=10)
        if res and res.status_code == 200:
            try:
                return res.json()
            except:
                log.error("❌ get_storage_info: 响应非 JSON 格式")
        return None

    # 3. 列出目录 (FS API)
    def list_directory(self, path: str = "/", **kwargs) -> Optional[Dict[str, Any]]:
        url = f"{self.host}/api/fs/list"
        payload = {
            "path": path,
            "password": kwargs.get("password", ""),
            "refresh": kwargs.get("refresh", False),
            "page": kwargs.get("page", 1),
            "per_page": kwargs.get("per_page", 30),
        }
        res = self._do_request("POST", url, json=payload, timeout=15)
        return res.json() if res and res.status_code == 200 else None

    # 4. 创建目录 (FS API)
    def mkdir(self, path: str) -> bool:
        url = f"{self.host}/api/fs/mkdir"
        res = self._do_request("POST", url, json={"path": path}, timeout=15)
        return res is not None and res.status_code == 200

    # 5. 移动文件 (FS API)
    def move(self, src: str, dst: str) -> bool:
        url = f"{self.host}/api/fs/move"
        payload = {
            "src_dir": os.path.dirname(src),
            "dst_dir": os.path.dirname(dst),
            "names": [os.path.basename(src)],
        }
        res = self._do_request("POST", url, json=payload, timeout=30)
        return res is not None and res.status_code == 200

    # 6. 删除文件 (FS API)
    def remove(self, path: str) -> bool:
        url = f"{self.host}/api/fs/remove"
        payload = {
            "dir": os.path.dirname(path),
            "names": [os.path.basename(path)],
        }
        res = self._do_request("POST", url, json=payload, timeout=30)
        return res is not None and res.status_code == 200

    # 7. 检查路径是否存在 (逻辑方法)
    def check_exists(self, path: str) -> bool:
        if not path or path == "/":
            res = self.list_directory("/", per_page=1)
            return res is not None and res.get("code") in (0, 200)

        path = path.rstrip("/")
        parts = path.split("/")
        parent_path = "/".join(parts[:-1]) or "/"
        target_name = parts[-1]

        try:
            result = self.list_directory(parent_path, per_page=1000)
            if not result or result.get("code") not in (0, 200):
                return False

            data = result.get("data", {})
            content = data.get("content", []) if isinstance(data, dict) else []
            for item in content:
                if isinstance(item, dict) and item.get("name") == target_name:
                    return True
            return False
        except Exception:
            return False

    # 8. 获取兼容格式的内容列表 (逻辑方法)
    def list_contents(self, path: str):
        result = self.list_directory(path)
        if result is None or result.get("code") not in (0, 200):
            return "404_NOT_FOUND"
        data = result.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []
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
            if not self.list_directory(path, refresh=True):
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
        data: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
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
            if e.response.status_code == 404:
                return False
            raise

    def list_contents(self, path: str) -> Union[Dict, str]:
        try:
            res = self._request(
                "PROPFIND",
                path,
                headers={"Depth": "1", "Content-Type": "text/xml; charset=utf-8"},
            )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
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
            if e.response.status_code != 405:
                raise

    def move(self, src: str, dst: str) -> None:
        self._request("MOVE", src, headers={"Destination": f"{self.host}/dav{dst}"})

    def copy(self, src: str, dst: str) -> None:
        self._request("COPY", src, headers={"Destination": f"{self.host}/dav{dst}"})
