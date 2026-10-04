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


def _dist_artifact_mtime(chunks: list[Path]) -> float:
    """dist 产物 mtime 聚合口径（唯一实现，生产断言与用例共用）。

    取「最新产物 mtime」：陈旧 rebuild 表现为全部产物落后源码；dist/assets
    中的残留旧文件（手工拷贝/未清理旧 hash 产物）不构成"源码已改未 rebuild"
    证据。用例必须经本函数聚合——自算 min/max 会使护栏口径被改坏而无红。
    空集合抛错：护栏失去输入时不得静默通过。
    """
    if not chunks:
        raise ValueError("dist 产物集合为空，无 mtime 可聚合")
    return max(c.stat().st_mtime for c in chunks)


FUTURE_TOLERANCE_SECONDS = 2  # 未来时间戳判定容差
STALE_TOLERANCE_SECONDS = 2  # 陈旧判定容差（mtime 粒度容忍，与未来时间戳容差语义独立）


def _is_dist_stale(src_latest: float, dist_mtime: float) -> bool:
    """新鲜度判定唯一入口：最新 chunk mtime 落后最新源码超过 2s 容差即判陈旧。

    容忍 2 秒粒度（FAT / 快速写入下同刻视为通过）。生产断言与红/绿路径
    用例都必须经本函数判定，杜绝用例自算算式绕过真实护栏。
    未来时间戳防线：dist mtime 超前当前时钟超过 FUTURE_TOLERANCE_SECONDS
    容差属异常产物（未来时间戳残留使 max 口径永久失效），直接判陈旧。
    """
    if dist_mtime > time.time() + FUTURE_TOLERANCE_SECONDS:
        return True
    return src_latest > dist_mtime + STALE_TOLERANCE_SECONDS


def _stale_message(src_latest: float, dist_newest: float) -> str:
    """陈旧/未来时间戳命中时统一的用户可见失败消息。

    生产断言与红路径用例共用本函数，使用例真断言生产消息（而非自证自明的
    内联副本）：删除本函数返回中的「时间戳异常」提示，用例 match 即失配转红。
    """
    return (
        "检测到 src/webui 前端源码比 dist 产物新 —— 源码已改未 rebuild dist。"
        "请执行: cd src/webui && npx vite build"
        f"（源码最新 mtime={src_latest}, dist 最新 chunk mtime={dist_newest}）。"
        "若 dist mtime 超前当前时钟，请检查 dist/assets 中时间戳异常文件"
    )


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
    """新鲜度护栏：最新 dist chunk 的 mtime 不得早于最新源码 mtime。"""

    def test_dist_not_staler_than_sources(self):
        chunks = _dist_js_chunks()
        assert chunks, "dist/assets 下无任何 JS chunk"
        src_latest = _latest_source_mtime()
        # max 口径：陈旧 rebuild 表现为全部 chunk 落后源码；dist/assets 中的
        # 残留旧文件（手工拷贝/未清理旧 hash 产物）不构成误报证据
        dist_newest = _dist_artifact_mtime(chunks)
        assert not _is_dist_stale(src_latest, dist_newest), _stale_message(
            src_latest, dist_newest)


class TestStaleFixtureRedPath:
    """tmp_path 模拟陈旧夹具，验证新鲜度断言的 fail 路径可复现（不污染真实 dist）。"""

    def test_stale_dist_triggers_freshness_failure(self, tmp_path):
        """红路径：全部 chunk 均落后源码 → _is_dist_stale 判 True。"""
        fake_src = tmp_path / "src"
        fake_dist_assets = tmp_path / "dist" / "assets"
        fake_src.mkdir(parents=True)
        fake_dist_assets.mkdir(parents=True)
        chunk = fake_dist_assets / "index-old.js"
        chunk.write_text("/* stale */", encoding="utf-8")
        old = time.time() - 3600
        os.utime(chunk, (old, old))
        src_file = fake_src / "main.js"
        src_file.write_text("// fresh", encoding="utf-8")

        src_latest = src_file.stat().st_mtime
        dist_newest = _dist_artifact_mtime([chunk])
        assert _is_dist_stale(src_latest, dist_newest), (
            "陈旧夹具应触发新鲜度 fail 路径（dist mtime 早于源码）")

    def test_fresh_dist_passes_freshness_check(self, tmp_path):
        """绿路径：dist 新于源码 → _is_dist_stale 判 False。"""
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
        dist_newest = _dist_artifact_mtime([chunk])
        assert not _is_dist_stale(src_latest, dist_newest), (
            "新鲜 dist 不应触发 fail")

    def test_leftover_stale_chunk_does_not_flag_stale(self, tmp_path):
        """残留旧 chunk + 本次构建新 chunk 并存 → 不得误报陈旧。

        max 口径下"陈旧 rebuild"表现为全部 chunk 落后；单个残留旧文件
        （手工拷贝/未清理旧 hash 产物）不构成"源码已改未 rebuild"证据。
        真实文件夹具：两个 chunk 落盘 + os.utime 设 mtime，杜绝纯字面量
        断言（夹具必须复现真实产物并存形态）。
        """
        fake_dist_assets = tmp_path / "dist" / "assets"
        fake_dist_assets.mkdir(parents=True)
        fresh_chunk = fake_dist_assets / "index-new.js"
        stale_chunk = fake_dist_assets / "index-old.js"
        fresh_chunk.write_text("/* fresh */", encoding="utf-8")
        stale_chunk.write_text("/* stale */", encoding="utf-8")
        now = time.time()
        os.utime(fresh_chunk, (now - 1, now - 1))
        os.utime(stale_chunk, (now - 3600, now - 3600))

        # 经生产聚合口径（_dist_artifact_mtime）取值：口径被改坏（如退回 min）
        # 时本断言必红——这正是本用例对生产护栏的检测力所在
        dist_newest = _dist_artifact_mtime(
            sorted(fake_dist_assets.glob("*.js")))
        assert not _is_dist_stale(now, dist_newest), (
            "fresh 与 stale chunk 并存时不得误报陈旧（残留旧 chunk 不应翻红）")
        assert _is_dist_stale(now, _dist_artifact_mtime([stale_chunk])), (
            "仅 stale chunk 存在时仍必须判陈旧（护栏检测力不得因 max 口径丧失）")


class TestFutureTimestampGuard:
    """dist 最新 mtime 远超当前时钟 → 判陈旧（未来时间戳残留防线）。

    未来时间戳残留文件使 max 口径"取最新"永久失效（最新永远是未来值，
    护栏对后续一切源码改动静默放行）。防线居测试文件内部（判定函数本就
    定义于本文件），不外溢业务源码。判定容差与陈旧判定同为
    FUTURE_TOLERANCE_SECONDS。
    """

    def test_future_dist_mtime_flags_stale(self, tmp_path):
        future = time.time() + FUTURE_TOLERANCE_SECONDS * 10
        assert _is_dist_stale(time.time(), future), (
            "dist mtime 超前当前时钟属异常产物（未来时间戳残留），必须判陈旧")

    def test_future_hit_reports_distinguishable_message(self):
        """未来时间戳命中时，生产失败消息必须含「时间戳异常」可区分提示。

        断言生产 `_stale_message` 真实返回值，而非用例内联副本；删生产提示
        则 match 失配转红（消除自证自明）。
        """
        future = time.time() + FUTURE_TOLERANCE_SECONDS * 10
        src_latest = time.time() - 3600
        assert _is_dist_stale(src_latest, future), (
            "未来时间戳必须判陈旧（护栏检测力前提）")
        msg = _stale_message(src_latest, future)
        assert "时间戳异常" in msg, (
            f"生产失败消息必须含可区分的时间戳异常提示，实际: {msg!r}")
        with pytest.raises(AssertionError, match="时间戳异常"):
            assert not _is_dist_stale(src_latest, future), _stale_message(
                src_latest, future)
