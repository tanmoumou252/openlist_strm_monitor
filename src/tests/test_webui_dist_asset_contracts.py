"""WebUI 构建产物与二进制资产存在性契约。

- dist/assets 必须存在 core-*.js chunk（vite manualChunks 的 core 分组被删
  即 fail——既有 test_webui_dist_freshness.py 守护 index entry、mtime 与
  index.html 悬空 asset 引用，不覆盖 core 分组存在性，是缺口）
- publicDir 资产必须原样复制进 dist（不加哈希、不改名）
- FTS5 中文分词 DLL 必须随仓库分发（缺失时中文搜索静默退化）

dist skipif 为类级 mark：仅作用于 TestDistChunkContracts（dist 产物断言），
TestBinaryAssets 的 DLL 存在性断言不随 dist 缺失被连带跳过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = PROJECT_ROOT / "src" / "webui" / "public"
DIST_DIR = PROJECT_ROOT / "dist"
DLL_PATH = PROJECT_ROOT / "src" / "tokenizers" / "simple" / "simple.dll"


@pytest.mark.skipif(
    not (DIST_DIR / "index.html").exists(),
    reason="dist/index.html 不存在（新 clone 或未构建），dist 契约跳过")
class TestDistChunkContracts:
    def test_core_chunk_exists(self):
        chunks = list((DIST_DIR / "assets").glob("core-*.js"))
        assert chunks, (
            "dist/assets 缺少 core-*.js chunk：vite manualChunks 的 core "
            "分组（modules/core + modules/components）被移除或产物不完整，"
            "请检查 src/webui/vite.config.js 并 rebuild")

    def test_public_dir_assets_copied_verbatim(self):
        assert PUBLIC_DIR.is_dir(), "src/webui/public 目录缺失"
        missing = [
            str(p.relative_to(PUBLIC_DIR))
            for p in sorted(PUBLIC_DIR.rglob("*"))
            if p.is_file()
            and not (DIST_DIR / p.relative_to(PUBLIC_DIR)).is_file()
        ]
        assert not missing, f"publicDir 资产未原样复制进 dist: {missing}"


class TestBinaryAssets:
    def test_fts5_simple_tokenizer_dll_exists(self):
        assert DLL_PATH.is_file(), (
            "src/tokenizers/simple/simple.dll 缺失：中文分词将静默退化为 "
            "unicode61，中文搜索返回空。请补齐该二进制资产")
