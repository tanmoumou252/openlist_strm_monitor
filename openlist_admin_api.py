""" [测试] OpenList Admin API 客户端
功能：
 1. 登录 (POST /api/auth/login) - 内置 TOTP 生成
 2. 获取存储列表 (GET /api/admin/storage/list)
 3. 获取存储详情 (GET /api/admin/storage/info)
 4. 列出目录内容 (POST /api/fs/list)
 5. 移动文件 (POST /api/fs/move)
 6. 删除文件 (POST /api/fs/remove)"""

import os
import requests
import logging
import time
import hmac
import hashlib
import base64
from typing import Optional, Dict, Any, List
from pathlib import Path

log = logging.getLogger("openlist_api")


class OpenListAdminClient:
    def __init__(self, host: str, user: str = "", password: str = "", totp_secret: str = ""):
        # 统一处理 host，确保没有末尾斜杠且不包含 /dav
        self.host = host.rstrip("/")
        if self.host.endswith("/dav"):
            self.host = self.host[:-4]
        self.user = user
        self.password = password
        self.totp_secret = totp_secret
        self.token: Optional[str] = None

    # ==================================================================
    # 内置 TOTP 生成方法（不依赖外部模块）
    # ==================================================================
    @staticmethod
    def _generate_totp(secret: str) -> str:
        """生成 6 位 TOTP 码（RFC 6238）"""
        if not secret:
            raise ValueError("TOTP Secret 不能为空")
        # 1. 获取当前时间戳（30 秒为一个周期）
        timestamp = int(time.time() // 30)
        # 2. 将时间戳转换为 8 字节大端字节序
        timestamp_bytes = timestamp.to_bytes(8, byteorder="big")
        # 3. 将 secret 解码为字节（支持 base32 或 base64）
        try:
            # 尝试 base32 解码（OpenList 通常使用 base32）
            secret_bytes = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
        except (binascii.Error, ValueError):
            # 如果 base32 失败，尝试 base64
            secret_bytes = base64.b64decode(secret)
        # 4. 计算 HMAC-SHA1
        hmac_hash = hmac.new(secret_bytes, timestamp_bytes, hashlib.sha1).digest()
        # 5. 动态截断（RFC 4226）
        offset = hmac_hash[-1] & 0x0F
        truncated_hash = hmac_hash[offset : offset + 4]
        # 6. 转换为 32 位整数
        otp_int = int.from_bytes(truncated_hash, byteorder="big") & 0x7FFFFFFF
        # 7. 取模 10^6 得到 6 位码
        otp = otp_int % 1000000
        return f"{otp:06d}"  # 补零到 6 位

    # ==================================================================
    # 登录 (POST /api/auth/login)
    # ==================================================================
    def login(self, otp_code: Optional[str] = None) -> bool:
        """POST /api/auth/login"""
        if not otp_code and self.totp_secret:
            otp_code = self._generate_totp(self.totp_secret)
        url = f"{self.host}/api/auth/login"
        payload = {
            "username": self.user,
            "password": self.password,
            "otp_code": otp_code,
        }
        try:
            res = requests.post(url, json=payload, timeout=10)
            res.raise_for_status()
            data = res.json()
            # 提取 token（兼容多种返回结构）
            token = None
            if isinstance(data.get("data"), dict):
                token = data["data"].get("token")
            if not token:
                token = data.get("token")
            if token:
                self.token = token
                log.info("✅ 登录成功")
                return True
            else:
                log.error(f"❌ 登录失败：无法提取 token，响应：{data}")
                return False
        except Exception as e:
            log.error(f"❌ 登录请求异常：{e}")
            return False

    def _get_headers(self) -> Dict[str, str]:
        """构建通用请求头"""
        if not self.token:
            raise Exception("未登录，请先调用 login()")
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

    # ==================================================================
    # 存储相关接口 (openlist_api_storages.md)
    # ==================================================================
    def list_storages(self, page: int = 1, per_page: int = 30) -> Optional[Dict[str, Any]]:
        """GET /api/admin/storage/list"""
        url = f"{self.host}/api/admin/storage/list"
        params = {"page": page, "per_page": per_page}
        try:
            res = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
            res.raise_for_status()
            log.info("✅ 获取存储列表成功")
            return res.json()
        except Exception as e:
            log.error(f"❌ 获取存储列表失败：{e}")
            return None

    def get_storage_info(self, storage_id: int) -> Optional[Dict[str, Any]]:
        """GET /api/admin/storage/get?id={id}"""
        url = f"{self.host}/api/admin/storage/get"
        params = {"id": storage_id}
        # 使用 id 而不是 storage_id
        try:
            res = requests.get(url, headers=self._get_headers(), params=params, timeout=10)
            res.raise_for_status()
            # 检查响应体是否为空
            if not res.text.strip():
                log.error(f"❌ 存储 {storage_id} 详情响应为空")
                return None
            data = res.json()
            log.info(f"✅ 获取存储 {storage_id} 详情成功")
            return data
        except requests.exceptions.JSONDecodeError as e:
            log.error(f"❌ 存储 {storage_id} 详情响应非 JSON 格式：{res.text}")
            return None
        except Exception as e:
            log.error(f"❌ 获取存储 {storage_id} 详情失败：{e}")
            return None

    # ==================================================================
    # 文件系统相关接口 (openlist_api_list_directory_trigger_strm.md)
    # ==================================================================
    def list_directory(
        self,
        path: str = "/",
        password: str = "",
        refresh: bool = False,
        page: int = 1,
        per_page: int = 30,
    ) -> Optional[Dict[str, Any]]:
        """POST /api/fs/list"""
        url = f"{self.host}/api/fs/list"
        payload = {
            "path": path,
            "password": password,
            "refresh": refresh,
            "page": page,
            "per_page": per_page,
        }
        try:
            res = requests.post(url, headers=self._get_headers(), json=payload, timeout=10)
            res.raise_for_status()
            log.info(f"✅ 列出目录 {path} 成功")
            return res.json()
        except Exception as e:
            log.error(f"❌ 列出目录失败：{e}")
            return None

    def mkdir(self, path: str) -> bool:
        """创建目录"""
        url = f"{self.host}/api/fs/mkdir"
        headers = self._get_headers()
        data = {"path": path}
        try:
            res = requests.post(url, headers=headers, json=data, timeout=30)
            res.raise_for_status()
            return True  # ← **直接返回 True**
        except Exception as e:
            log.error("[AdminAPI] MKDIR失败: %s (%s)", path, e)
            return False

    def move(self, src: str, dst: str) -> bool:
        """移动文件"""
        url = f"{self.host}/api/fs/move"
        headers = self._get_headers()
        data = {
            "src_dir": os.path.dirname(src),
            "dst_dir": os.path.dirname(dst),
            "names": [os.path.basename(src)],
        }
        try:
            res = requests.post(url, headers=headers, json=data, timeout=30)
            res.raise_for_status()
            return True  # ← **直接返回 True**
        except Exception as e:
            log.error(f"❌ 移动文件失败：{e}")
            return False

    def remove(self, path: str) -> bool:
        """删除文件或目录"""
        url = f"{self.host}/api/fs/remove"
        headers = self._get_headers()
        data = {
            "dir": os.path.dirname(path),
            "names": [os.path.basename(path)],
        }
        try:
            res = requests.post(url, headers=headers, json=data, timeout=30)
            res.raise_for_status()
            return True  # ← **直接返回 True**
        except Exception as e:
            log.error(f"❌ 删除文件失败：{e}")
            return False
