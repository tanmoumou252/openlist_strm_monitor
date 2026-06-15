"""Subtitle Handler - handles subtitle file processing and synchronization."""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service import AppService
    from database import Database
    from config import AppConfig

from media_renamer import (
    detect_media_type_from_path,
    is_subtitle_file,
    detect_subtitle_language,
    SUBTITLE_EXTS,
    _extract_season_episode,
    _build_standard_name,
    extract_season_from_path,
)


class SubtitleHandler:
    """字幕处理器"""

    def __init__(self, app: AppService) -> None:
        self.app = app
        self.config: AppConfig = app.config
        self.db: Database = app.db

    def process_subtitle_file(self, a_subtitle_path: str | Path) -> None:
        """
        独立处理单个字幕文件：从文件名提取季集信息，直接复制到B区标准目录。
        使用数据库记录已处理字幕，避免重复处理。
        """
        sub_file = Path(a_subtitle_path).resolve()
        if not sub_file.exists():
            return

        # 获取A区根目录
        a_root = self.app.get_a_root_for_path(sub_file)
        if a_root is None:
            return

        # 计算字幕指纹（基于文件路径和内容修改时间）
        stat = sub_file.stat()
        fingerprint = hashlib.sha256(
            f"{sub_file}:{stat.st_size}:{stat.st_mtime}".encode()
        ).hexdigest()

        # 检查数据库：已存在且目标文件仍在，跳过
        existing = self.db.get_subtitle_by_local(str(sub_file))
        if existing:
            target_path = existing[2]
            if Path(target_path).exists():
                return  # 已处理且目标存在，静默跳过
            # 目标不存在，重新处理

        # ========== 关键修复：先判断媒体类型，再决定处理方式 ==========

        # 1. 优先基于路径判断媒体类型
        media_type = detect_media_type_from_path(sub_file)

        # 2. 如果是电影，直接走电影模式
        if media_type == "movie":
            self._process_movie_subtitle(sub_file, a_root, fingerprint)
            return

        # 3. 检查同目录STRM文件，辅助判断是电影还是番剧
        parent_dir = sub_file.parent
        strm_files = list(parent_dir.glob("*.strm"))

        # 单STRM且无法提取季集 → 电影
        if len(strm_files) <= 1:
            if not strm_files:
                # 无STRM，按电影处理
                self._process_movie_subtitle(sub_file, a_root, fingerprint)
                return
            # 有1个STRM，检查是否能提取季集
            strm_season, strm_episode = _extract_season_episode(
                strm_files[0].name)
            if strm_season is None or strm_episode is None:
                # STRM无季集信息，是电影
                self._process_movie_subtitle(sub_file, a_root, fingerprint)
                return

        # ========== 番剧模式 ==========
        self._process_anime_subtitle(sub_file, a_root, fingerprint)

    def _process_movie_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        """处理电影字幕：复制到B区同目录，重命名为 电影名.forced.zho.简体.ass"""
        # 查找同目录下的STRM文件作为关联目标
        parent_dir = sub_file.parent
        strm_files = list(parent_dir.glob("*.strm"))

        if strm_files:
            # 使用STRM文件名（不含扩展名）作为基础
            movie_stem = strm_files[0].stem
        else:
            # 没有STRM，使用字幕文件名（去掉语言标识）
            movie_stem = sub_file.stem
            # 去掉常见的语言后缀
            for suffix in [".forced", ".zho", ".简体", ".繁体",
                           ".sc", ".tc", ".chs", ".cht", ".scjp"]:
                movie_stem = movie_stem.replace(suffix, "")
            movie_stem = movie_stem.rstrip(".")

        # 构建目标路径：保持同目录结构
        rel_parent = sub_file.relative_to(a_root).parent
        b_target_dir = self.app.b_root / rel_parent
        b_target_dir.mkdir(parents=True, exist_ok=True)

        # 语言信息
        lang_info = detect_subtitle_language(sub_file.name)
        if lang_info is None:
            new_name = f"{movie_stem}.forced.zho.中文{sub_file.suffix.lower()}"
        else:
            _code, _label, _priority = lang_info
            new_name = f"{movie_stem}.forced.{_code}.{_label}{
                sub_file.suffix.lower()}"

        target = b_target_dir / new_name

        # 如果目标已存在，更新数据库并跳过
        if target.exists():
            logging.debug("[字幕跳过] 目标文件已存在: %s", target)
            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=None,
                episode=None,
                lang_code=lang_info[0] if lang_info else None,
            )
            return

        try:
            shutil.copyfile(sub_file, target)
            logging.info("[字幕复制] 电影字幕: %s -> %s", sub_file, target)

            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=None,
                episode=None,
                lang_code=lang_info[0] if lang_info else None,
            )
        except Exception as e:
            logging.warning("[字幕复制失败] %s: %s", sub_file, e)

    def _process_anime_subtitle(
            self, sub_file: Path, a_root: Path, fingerprint: str) -> None:
        """处理番剧字幕：提取季集，复制到 Season XX/S01E01..."""
        # 提取季集信息
        season, episode = _extract_season_episode(sub_file.name)

        # 如果字幕本身没有季集信息，尝试从相邻的 STRM 文件提取
        if season is None or episode is None:
            parent_dir = sub_file.parent

            # 1. 先查同目录
            for strm_file in parent_dir.glob("*.strm"):
                season, episode = _extract_season_episode(strm_file.name)
                if season is not None and episode is not None:
                    logging.debug("[字幕关联] 从同目录STRM提取: %s -> S%02dE%02d",
                                  strm_file.name, season, episode)
                    break

            # 2. 如果当前在媒体根目录（不是Season目录），查子目录
            if (season is None or episode is None) and not re.match(
                    r"(?i)^season\s*\d+$", parent_dir.name):
                for sub_dir in parent_dir.iterdir():
                    if sub_dir.is_dir() and re.match(r"(?i)^season\s*\d+$", sub_dir.name):
                        for strm_file in sub_dir.glob("*.strm"):
                            season, episode = _extract_season_episode(
                                strm_file.name)
                            if season is not None and episode is not None:
                                logging.debug("[字幕关联] 从子目录STRM提取: %s -> S%02dE%02d",
                                              strm_file.name, season, episode)
                                break
                        if season is not None and episode is not None:
                            break

        # 如果还是无法提取，降级为电影处理
        if season is None or episode is None:
            logging.warning("[字幕处理] 无法提取番剧季集，降级为电影模式: %s", sub_file)
            self._process_movie_subtitle(sub_file, a_root, fingerprint)
            return

        # 构建标准路径
        rel = sub_file.relative_to(a_root)
        rel_parts = list(rel.parts)

        # 检查是否已有 Season 目录或中文季目录
        has_season_dir = False
        cn_season_index = -1
        for i, part in enumerate(rel_parts[:-1]):
            if re.match(r"(?i)^season\s*\d+$", part):
                has_season_dir = True
                break
            if re.match(r"^第[一二三四五六七八九十\d]+季$", part):
                cn_season_index = i

        if has_season_dir:
            b_target_dir = self.app.b_root / rel.parent
        elif cn_season_index >= 0:
            b_target_dir = self.app.b_root / \
                Path(*rel_parts[:cn_season_index]) / f"Season {season:02d}"
        else:
            if len(rel_parts) >= 2:
                b_target_dir = self.app.b_root / \
                    Path(*rel_parts[:-1]) / f"Season {season:02d}"
            else:
                b_target_dir = self.app.b_root / rel.parent

        b_target_dir.mkdir(parents=True, exist_ok=True)

        base_name = _build_standard_name(season, episode)
        lang_info = detect_subtitle_language(sub_file.name)

        if lang_info is None:
            new_name = f"{base_name}.forced.zho.中文{sub_file.suffix.lower()}"
        else:
            _code, _label, _priority = lang_info
            new_name = f"{base_name}.forced.{_code}.{_label}{
                sub_file.suffix.lower()}"

        target = b_target_dir / new_name

        # 如果目标已存在，更新数据库并跳过
        if target.exists():
            logging.debug("[字幕跳过] 目标文件已存在: %s", target)
            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=season,
                episode=episode,
                lang_code=lang_info[0] if lang_info else None,
            )
            return

        try:
            shutil.copyfile(sub_file, target)
            logging.info("[字幕复制] 番剧字幕: %s -> %s", sub_file, target)

            self.db.upsert_subtitle(
                local_path=str(sub_file),
                target_path=str(target),
                fingerprint=fingerprint,
                season=season,
                episode=episode,
                lang_code=lang_info[0] if lang_info else None,
            )
        except Exception as e:
            logging.warning("[字幕复制失败] %s: %s", sub_file, e)
