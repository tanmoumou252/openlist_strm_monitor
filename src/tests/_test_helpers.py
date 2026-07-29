"""测试共享工厂模块。

将 mock AppService 构建逻辑集中于此，供 conftest.py fixture 和
各测试文件（test_refresh_service / test_subtitle_handler /
test_sync_service）复用，消除三处 `_make_app` 的重复实现。
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock
from unittest.mock import MagicMock, Mock


def build_mock_app(
    tmp_path: Path | None = None,
    *,
    # RefreshService 配置
    refresh_enabled: bool = True,
    refresh_paths: list[str] | None = None,
    interval_seconds: int = 300,
    strm_engine_paths: list[str] | None = None,
    full_audit_interval_days: int = 7,
    # SubtitleHandler 配置
    setup_b_root: bool = False,
    # SyncService 配置
    a_dirs: list[Path] | None = None,
    ghost_protect_seconds: int = 300,
    # 通用配置
    use_mock: bool = False,
):
    """构建最小化 mock AppService 的共享工厂。

    通过 keyword 参数控制不同子系统所需的属性子集：
    - RefreshService: refresh_enabled, refresh_paths, interval_seconds,
                      strm_engine_paths
    - SubtitleHandler: tmp_path + setup_b_root=True
    - SyncService: tmp_path + a_dirs, ghost_protect_seconds
    """
    app_cls = Mock if use_mock else MagicMock
    app = app_cls()

    # RefreshService 配置
    app.config.refresh.enabled = refresh_enabled
    app.config.refresh.interval_seconds = interval_seconds
    app.config.refresh.full_audit_interval_days = full_audit_interval_days
    app.config.refresh_paths = refresh_paths or []
    app.config.strm_engine_paths = strm_engine_paths or []

    # SubtitleHandler 配置（仅准备目录；mapping mock 放在函数末尾，避免被后续覆盖）
    if setup_b_root and tmp_path is not None:
        b_root = tmp_path / "b_root"
        b_root.mkdir(parents=True, exist_ok=True)
        app.b_root = b_root

    # SyncService 配置
    if tmp_path is not None:
        # 只在 use_mock=True 时创建 a 目录（sync_service 需要）
        if use_mock:
            a_root = tmp_path / "a"
            a_root.mkdir(parents=True, exist_ok=True)
            if a_dirs is None:
                a_dirs = [a_root]
        app.a_roots = a_dirs or []

    app.config.behavior.ghost_protect_seconds = ghost_protect_seconds
    app.admin_api = app_cls()
    app.db = app_cls()

    # 默认 DB 查询返回空列表(防止后台线程对 Mock 对象迭代导致 TypeError)
    app.db.get_b_under_root.return_value = []
    app.db.get_all_b_by_fingerprint.return_value = []

    # SyncService 特定方法
    app.build_b_path_from_a = app_cls()
    app._verify_b_path_lineage = app_cls(return_value=True)
    app.ensure_single_visible_instance = app_cls()
    app.handle_a_created_or_modified = app_cls()
    # get_fingerprint_lock 必须返回真正的 Lock 对象，支持上下文管理器协议（P1-4）
    app.get_fingerprint_lock = lambda _fp: Lock()
    app.get_a_root_for_path = app_cls()

    # 默认映射相关方法返回 None（fail-closed 行为）
    app.get_mapping_for_a = app_cls(return_value=None)
    app.get_mapping_for_b = app_cls(return_value=None)
    app.a_b_mappings = []

    # setup_b_root 使用 canonical mapping tuple；放在默认 fail-closed 设置之后，避免被覆盖。
    if setup_b_root and tmp_path is not None:
        b_root = tmp_path / "b_root"
        a_root = tmp_path / "a"
        app.get_mapping_for_a = lambda _p, _ar=a_root, _br=b_root: ("test-mapping", _ar, _br)
        app.get_mapping_for_b = lambda _p, _ar=a_root, _br=b_root: ("test-mapping", _br, _ar)

    return app
