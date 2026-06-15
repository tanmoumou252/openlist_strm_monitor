"""STRM Storage Manager - handles STRM storage configuration from OpenList Admin API."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webdav_client import OpenListAdminClient


@dataclass(slots=True, frozen=True)
class StrmStorageInfo:
    """STRM 存储信息"""

    id: int
    mount_path: str
    status: str
    paths: list[str]
    save_local_mode: str

    @property
    def is_working(self) -> bool:
        return self.status == "work"

    @property
    def is_sync_mode(self) -> bool:
        return self.save_local_mode.lower() == "update"


class StrmStorageManager:
    """STRM 存储管理器"""

    def __init__(self, client: OpenListAdminClient) -> None:
        self.client = client

    @staticmethod
    def _extract_paths_from_addition(addition: str) -> list[str]:
        if not addition:
            return []
        try:
            addition_dict = json.loads(addition)
            paths = addition_dict.get("paths", "")
            if isinstance(paths, str):
                return [p.strip() for p in paths.split("\n") if p.strip()]
            elif isinstance(paths, list):
                return [str(p).strip() for p in paths if str(p).strip()]
            return []
        except json.JSONDecodeError:
            logging.warning("解析 addition 失败: %s", addition[:200])
            return []

    @staticmethod
    def _extract_save_local_mode(addition: str) -> str:
        if not addition:
            return ""
        try:
            addition_dict = json.loads(addition)
            return addition_dict.get("SaveLocalMode", "")
        except json.JSONDecodeError:
            return ""

    def get_strm_storages(self) -> list[StrmStorageInfo]:
        """获取所有 STRM 存储"""
        storages = self.client.list_storages()
        if not storages:
            return []
        data = storages.get("data", {})
        content = data.get("content", []) if isinstance(data, dict) else []
        result: list[StrmStorageInfo] = []
        for storage in content:
            if storage.get("driver", "").lower() != "strm":
                continue
            addition = storage.get("addition", "")
            result.append(
                StrmStorageInfo(
                    id=storage.get("id", 0),
                    mount_path=storage.get("mount_path", ""),
                    status=storage.get("status", "unknown"),
                    paths=self._extract_paths_from_addition(addition),
                    save_local_mode=self._extract_save_local_mode(addition),
                )
            )
        return result

    def get_working_sync_storages(self) -> list[StrmStorageInfo]:
        """获取有效的更新模式存储"""
        return [s for s in self.get_strm_storages(
        ) if s.is_working and s.is_sync_mode]

    def validate_against_local_paths(
            self, local_strm_engine_paths: list[str]) -> dict:
        """验证本地配置与 API 的一致性"""
        api_storages = self.get_strm_storages()
        api_mount_paths = {s.mount_path for s in api_storages}
        local_strm_set = set(p.rstrip("/")
                             for p in local_strm_engine_paths if p.strip())
        result: dict = {
            "api_storages": api_storages,
            "missing_in_api": [],
            "extra_in_api": [],
            "non_working": [],
            "non_sync_mode": [],
            "valid": [],
        }
        for local_path in local_strm_set:
            if local_path not in api_mount_paths:
                result["missing_in_api"].append(local_path)
        for storage in api_storages:
            mount = storage.mount_path.rstrip("/")
            if mount not in local_strm_set:
                result["extra_in_api"].append(storage)
                continue
            if not storage.is_working:
                result["non_working"].append(storage)
                continue
            if not storage.is_sync_mode:
                result["non_sync_mode"].append(storage)
                continue
            result["valid"].append(storage)
        return result
