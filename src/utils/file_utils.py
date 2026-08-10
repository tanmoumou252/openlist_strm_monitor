"""File system utilities."""

from __future__ import annotations

import os
import shutil
import stat
import time
from pathlib import Path


# 中文数字映射
CN_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15
}


def cn_to_int(s: str) -> int | None:
    """将中文数字转换为整数"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s.startswith("十"):
        if len(s) == 1:
            return 10
        rest = s[1:]
        return 10 + (cn_to_int(rest) or 0)
    if "十" in s:
        parts = s.split("十")
        if len(parts) == 2:
            left = cn_to_int(parts[0]) or 0
            right = cn_to_int(parts[1]) or 0
            return left * 10 + right
    return CN_NUMBERS.get(s)


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def copy_file(src: str | Path, dst: str | Path) -> None:
    ensure_parent(dst)
    shutil.copy2(src, dst)


def move_file(src: str | Path, dst: str | Path) -> None:
    """Move file, handling cross-device (EXDEV) errors by copying then removing."""
    ensure_parent(dst)
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        # Handle cross-device move (EXDEV): copy + remove instead
        import errno
        if hasattr(e, 'errno') and e.errno == errno.EXDEV:
            shutil.copy2(str(src), str(dst))
            os.remove(str(src))
        else:
            raise


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
    for directory in sorted(root_folder.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not directory.is_dir():
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            directory.rmdir()
        except OSError:
            continue


def normalize_path(path: str | Path) -> str:
    """规范化路径：解析为绝对路径，去除尾随斜杠，Windows 下小写。

    Args:
        path: 输入路径

    Returns:
        规范化后的路径字符串
    """
    p = Path(path).resolve()
    result = str(p)
    if os.name == "nt":
        result = result.lower()
    # 去除尾随斜杠（除了根目录）
    if len(result) > 3 and result.endswith('/'):
        result = result[:-1]
    return result


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
    严格删除文件（已废弃，请使用 safe_remove_file）。
    
    Deprecated: Use safe_remove_file instead, which handles permission errors.
    This function is kept for backwards compatibility only.
    
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
