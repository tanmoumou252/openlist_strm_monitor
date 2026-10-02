"""dist 构建产物存在性与新鲜度护栏。

护栏语义为"防忘 rebuild"而非精确等值校验（Windows 文件系统 mtime 粒度
有限，源码与 dist mtime 同刻视为通过）。若 dist 缺失（如新 clone）则全部
跳过，与 test_subset_font.py::TestDistAssets 的语义一致。
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DIST_DIR = PROJECT_ROOT / "dist"
DIST_INDEX = DIST_DIR / "index.html"
SRC_WEBUI = PROJECT_ROOT / "src" / "webui"

pytestmark = pytest.mark.skipif(
    not DIST_INDEX.exists(), reason="dist/index.html 不存在（新 clone 或未构建），跳过")


def _latest_source_mtime() -> float:
    """src/webui 前端源码（modules/**/*.js、main.js、index.html、styles）的最新 mtime。"""
    latest = 0.0
    candidates = [SRC_WEBUI / "main.js", SRC_WEBUI / "index.html"]
    modules = SRC_WEBUI / "modules"
    if modules.exists():
        for p in modules.rglob("*.js"):
            candidates.append(p)
    styles = SRC_WEBUI / "styles"
    if styles.exists():
        candidates.extend(styles.rglob("*.css"))
    for p in candidates:
        m = p.stat().st_mtime
        if m > latest:
            latest = m
    return latest


def _dist_js_chunks() -> list[Path]:
    return sorted((DIST_DIR / "assets").glob("*.js"))


class TestDistExistence:
    """dist 存在性与 hashed assets 引用完整性。"""

    def test_dist_index_referenced_assets_all_exist(self):
        """index.html 引用到的每个产物 asset 都必须真实存在于磁盘。

        断言方向为「html 引用 → 磁盘存在」。反向写法（对 glob 出的文件断
        exists）恒为真：glob 只返回已存在的文件，无法捕获 index.html 引用了
        已删 chunk 的悬空引用。
        """
        html = DIST_INDEX.read_text(encoding="utf-8")
        # 字符类含 "/" 以覆盖 assets/ 下的嵌套子目录引用（vite 可输出
        # assets/icons/x.svg 形态）；缺 "/" 会让嵌套悬空引用被静默跳过
        referenced = set(re.findall(
            r"\.?/?assets/([A-Za-z0-9_.\-/]+\.(?:js|css|ico|png|svg|woff2?))", html))
        # 空集判红：解析器与产物结构脱节时必须暴露，而非静默通过
        assert referenced, (
            "未能从 dist/index.html 解析出任何 assets 引用——产物结构漂移或"
            "解析正则失效，本护栏已失去检测能力")
        missing = sorted(
            name for name in referenced
            if not (DIST_DIR / "assets" / name).is_file())
        assert not missing, (
            "dist/index.html 引用了磁盘上不存在的产物（悬空引用，通常是 dist 被"
            f"部分清理或改名后未完整 rebuild）: {missing}")

    def test_dist_entry_chunk_exists(self):
        entries = [c for c in _dist_js_chunks() if c.name.startswith("index-")]
        assert entries, "dist/assets 缺少 index entry chunk"

    def test_dist_assets_js_chunks_exist(self):
        chunks = _dist_js_chunks()
        assert chunks, "dist/assets 下无任何 JS chunk"


class TestDistFreshness:
    """新鲜度护栏：源码 mtime 不得晚于全部 dist chunk 的 mtime。"""

    def test_dist_not_staler_than_sources(self):
        chunks = _dist_js_chunks()
        assert chunks, "dist/assets 下无任何 JS chunk"
        src_latest = _latest_source_mtime()
        dist_oldest = min(c.stat().st_mtime for c in chunks)
        # 容忍 2 秒粒度（FAT / 快速写入下同刻视为通过）
        assert dist_oldest + 2 >= src_latest, (
            "检测到 src/webui 前端源码比 dist 产物新 —— 源码已改未 rebuild dist。"
            f"请执行: cd src/webui && npx vite build（源码最新 mtime={src_latest}, "
            f"dist 最旧 chunk mtime={dist_oldest}）"
        )


class TestStaleFixtureRedPath:
    """tmp_path 模拟陈旧夹具，验证新鲜度断言的 fail 路径可复现（不污染真实 dist）。"""

    def test_stale_dist_triggers_freshness_failure(self, tmp_path):
        fake_src = tmp_path / "src"
        fake_dist_assets = tmp_path / "dist" / "assets"
        fake_src.mkdir(parents=True)
        fake_dist_assets.mkdir(parents=True)
        # dist chunk mtime 在过去，源码 mtime 在现在 → 判定应 fail
        chunk = fake_dist_assets / "index-old.js"
        chunk.write_text("/* stale */", encoding="utf-8")
        old = time.time() - 3600
        os.utime(chunk, (old, old))
        src_file = fake_src / "main.js"
        src_file.write_text("// fresh", encoding="utf-8")

        src_latest = src_file.stat().st_mtime
        dist_oldest = chunk.stat().st_mtime
        assert not (dist_oldest + 2 >= src_latest), (
            "陈旧夹具应触发新鲜度 fail 路径（dist mtime 早于源码）")

    def test_fresh_dist_passes_freshness_check(self, tmp_path):
        fake_src = tmp_path / "src"
        fake_dist_assets = tmp_path / "dist" / "assets"
        fake_src.mkdir(parents=True)
        fake_dist_assets.mkdir(parents=True)
        src_file = fake_src / "main.js"
        src_file.write_text("// old", encoding="utf-8")
        old = time.time() - 3600
        os.utime(src_file, (old, old))
        chunk = fake_dist_assets / "index-new.js"
        chunk.write_text("/* fresh */", encoding="utf-8")

        src_latest = src_file.stat().st_mtime
        dist_oldest = chunk.stat().st_mtime
        assert dist_oldest + 2 >= src_latest, "新鲜 dist 不应触发 fail"
