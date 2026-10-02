"""File system utilities."""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def copy_file(src: str | Path, dst: str | Path) -> None:
    ensure_parent(dst)
    shutil.copy2(src, dst)


def move_file(src: str | Path, dst: str | Path) -> None:
    ensure_parent(dst)
    shutil.move(str(src), str(dst))


def safe_remove_file(path: str | Path) -> bool:
    """安全删除文件，返回是否成功删除。"""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
            return True
        return True  # 文件已不存在
    except PermissionError:
        # 尝试修改权限后删除
        try:
            os.chmod(str(path), stat.S_IWRITE | stat.S_IRWXU)
            Path(path).unlink()
            return True
        except Exception:
            return False
    except OSError:
        return False


def remove_empty_dirs(root_folder: str | Path) -> None:
    root_folder = Path(root_folder)
    if not root_folder.exists():
        return

    for current_root, dirs, files in os.walk(root_folder, topdown=False):
        current = Path(current_root)
        if current == root_folder:
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
        except OSError:
            pass


def local_relative(root: str | Path, target: str | Path) -> Path:
    return Path(target).resolve().relative_to(Path(root).resolve())


def local_join(root: str | Path, relative_path: Path) -> Path:
    return Path(root).resolve() / relative_path


def quarantine_file(path: str | Path, suffix: str = ".invalid") -> Path | None:
    """
    将异常文件隔离，避免媒体库继续扫描到 .strm。
    例如：
      xxx.strm -> xxx.strm.invalid
    如果目标已存在，则自动追加时间戳。
    """
    p = Path(path)
    if not p.exists():
        return None

    target = p.with_name(p.name + suffix)
    if target.exists():
        target = p.with_name(f"{p.name}{suffix}.{int(time.time())}")

    try:
        p.rename(target)
        return target
    except OSError:
        return None


def remove_file_strict(path: str | Path) -> bool:
    """
    严格删除文件。
    返回 True 表示文件不存在或删除成功。
    返回 False 表示删除失败。
    """
    p = Path(path)
    try:
        if p.exists():
            p.unlink()
        return True
    except OSError:
        return False
