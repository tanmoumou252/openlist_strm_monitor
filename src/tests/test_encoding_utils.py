"""
字幕编码转换中间件测试

测试范围:
1. _normalize_to_utf8 对各种编码字节的处理(纯函数层面)
2. copy_subtitle_utf8 文件级端到端(读源 -> 转码 -> 写目标)
3. fail-safe:无法识别编码时回退原样复制,不丢字幕

运行方式:
  pytest src/tests/test_encoding_utils.py -v
"""

from __future__ import annotations

import codecs
import sys
from pathlib import Path

import pytest

# 确保 src/ 在 sys.path 中(conftest.py 也会处理,此处冗余保护)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.encoding_utils import (
    copy_subtitle_utf8,
    _normalize_to_utf8,
)


# ============================================================
# _normalize_to_utf8 纯函数测试
# ============================================================


class TestNormalizeToUtf8:
    """测试字节级编码标准化逻辑"""

    def test_empty_bytes(self):
        """空数据原样返回,无异常"""
        result, encoding = _normalize_to_utf8(b"")
        assert result == b""
        assert encoding is None

    def test_utf8_without_bom(self):
        """无 BOM 的 UTF-8 原样返回,encoding 为 None"""
        data = "[Script Info]\nTitle: Test".encode("utf-8")
        result, encoding = _normalize_to_utf8(data)
        assert result == data
        assert encoding is None

    def test_utf8_with_bom(self):
        """带 BOM 的 UTF-8 去 BOM 返回"""
        data = codecs.BOM_UTF8 + "[Script Info]".encode("utf-8")
        result, encoding = _normalize_to_utf8(data)
        assert result == b"[Script Info]"
        assert encoding == "utf-8-sig"

    def test_gb18030_simplified_chinese(self):
        """GB18030 简体中文转 UTF-8"""
        text = "简体字幕测试"
        data = text.encode("gb18030")
        # GB18030 不是 UTF-8,前置断言
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8", errors="strict")

        result, encoding = _normalize_to_utf8(data)
        assert result.decode("utf-8") == text
        assert encoding == "gb18030"

    def test_big5_traditional_chinese(self):
        """Big5 繁体中文转 UTF-8

        回归点:Big5 字节流也能被 GB18030 严格解码成乱码,必须用 CJK
        字符命中率消歧,否则会误判为 GB18030。
        """
        text = "繁體字幕測試"
        data = text.encode("big5")
        with pytest.raises(UnicodeDecodeError):
            data.decode("utf-8", errors="strict")

        result, encoding = _normalize_to_utf8(data)
        assert result.decode("utf-8") == text
        assert encoding == "big5"

    def test_utf16_le_with_bom(self):
        """UTF-16 LE 带 BOM 转 UTF-8,清除残留 BOM"""
        text = "测试字幕"
        data = codecs.BOM_UTF16_LE + text.encode("utf-16-le")
        result, encoding = _normalize_to_utf8(data)
        assert result.decode("utf-8") == text
        assert encoding == "utf-16"
        # 确保没有残留 BOM 字符
        assert not result.startswith(codecs.BOM_UTF8)

    def test_utf16_be_with_bom(self):
        """UTF-16 BE 带 BOM 转 UTF-8"""
        text = "测试字幕"
        data = codecs.BOM_UTF16_BE + text.encode("utf-16-be")
        result, encoding = _normalize_to_utf8(data)
        assert result.decode("utf-8") == text
        assert encoding == "utf-16"

    def test_utf16_le_without_bom(self):
        """UTF-16 LE 无 BOM 启发式判断(ASCII 占比高触发 null ratio)

        回归点:UTF-16-LE 字节流也能被 GB18030 严格解码成乱码,启发式
        必须先于 GB18030/Big5 尝试,否则会误判为 GB18030。
        """
        # ASCII 字符在 UTF-16-LE 下高字节为 0x00,odd 位置 null 占比高
        text = "A测试B测试C测试D测试"
        data = text.encode("utf-16-le")
        result, encoding = _normalize_to_utf8(data)
        assert result.decode("utf-8") == text
        assert encoding == "utf-16-le"

    def test_unrecognized_encoding_raises(self):
        """无法识别的字节抛 UnicodeError"""
        # 256 字节全排列,既非 UTF-8 也非 GB18030/Big5/UTF-16
        data = bytes(range(256)) * 4
        with pytest.raises(UnicodeError):
            _normalize_to_utf8(data)

    def test_type_error_on_non_bytes(self):
        """非 bytes 输入抛 TypeError"""
        with pytest.raises(TypeError):
            _normalize_to_utf8("not bytes")  # type: ignore[arg-type]


# ============================================================
# copy_subtitle_utf8 文件级端到端测试
# ============================================================


class TestCopySubtitleUtf8:
    """测试文件级复制 + 转码"""

    @pytest.fixture
    def tmp_src_dst(self, tmp_path):
        """提供临时源/目标路径对"""
        src = tmp_path / "src.ass"
        dst = tmp_path / "subdir" / "dst.ass"  # 目标在子目录,测试自动建父目录
        return src, dst

    def test_empty_file(self, tmp_src_dst):
        """空文件:目标被创建且为空"""
        src, dst = tmp_src_dst
        src.write_bytes(b"")
        copy_subtitle_utf8(src, dst)
        assert dst.exists()
        assert dst.read_bytes() == b""

    def test_utf8_file_copied_as_is(self, tmp_src_dst):
        """已是 UTF-8:目标字节 == 源字节"""
        src, dst = tmp_src_dst
        data = "[Script Info]\nTitle: Test".encode("utf-8")
        src.write_bytes(data)
        copy_subtitle_utf8(src, dst)
        assert dst.read_bytes() == data

    def test_gb18030_file_converted(self, tmp_src_dst):
        """GB18030 源:目标为合法 UTF-8 且文本等价"""
        src, dst = tmp_src_dst
        text = "简体字幕测试"
        src.write_bytes(text.encode("gb18030"))
        copy_subtitle_utf8(src, dst)
        result = dst.read_bytes()
        # 目标必须可作 UTF-8 严格解码
        assert result.decode("utf-8", errors="strict") == text

    def test_big5_file_converted(self, tmp_src_dst):
        """Big5 源:目标为合法 UTF-8 且文本等价"""
        src, dst = tmp_src_dst
        text = "繁體字幕測試"
        src.write_bytes(text.encode("big5"))
        copy_subtitle_utf8(src, dst)
        result = dst.read_bytes()
        assert result.decode("utf-8", errors="strict") == text

    def test_utf8_with_bom_file_stripped(self, tmp_src_dst):
        """带 BOM 的 UTF-8 源:目标无 BOM"""
        src, dst = tmp_src_dst
        src.write_bytes(codecs.BOM_UTF8 + "测试".encode("utf-8"))
        copy_subtitle_utf8(src, dst)
        result = dst.read_bytes()
        assert not result.startswith(codecs.BOM_UTF8)
        assert result.decode("utf-8") == "测试"

    def test_unrecognized_encoding_fallback_copy(self, tmp_src_dst):
        """无法识别编码:fail-safe 回退原样复制,目标字节 == 源字节"""
        src, dst = tmp_src_dst
        data = bytes(range(256)) * 4
        src.write_bytes(data)
        # 不应抛异常
        copy_subtitle_utf8(src, dst)
        assert dst.exists()
        assert dst.read_bytes() == data

    def test_parent_dir_auto_created(self, tmp_src_dst):
        """目标父目录不存在时自动创建"""
        src, dst = tmp_src_dst
        src.write_bytes("测试".encode("utf-8"))
        assert not dst.parent.exists()
        copy_subtitle_utf8(src, dst)
        assert dst.exists()

    def test_string_path_args_accepted(self, tmp_path):
        """str 类型路径参数兼容(与 shutil.copyfile 一致)"""
        src = tmp_path / "s.ass"
        dst = tmp_path / "d.ass"
        src.write_bytes("测试".encode("utf-8"))
        copy_subtitle_utf8(str(src), str(dst))
        assert dst.read_bytes().decode("utf-8") == "测试"


# ============================================================
# 真实字幕样本探测(可选验证,容错处理)
# ============================================================


class TestRealSubtitleSamples:
    """对 src/tests/strm.test.A/ 下的真实字幕样本做探测性验证。

    样本文件名暗示编码:
      *.chs.简体.*  -> 可能是 GB18030
      *.cht.繁體.*  -> 可能是 Big5
      *.eng.*       -> 可能是 UTF-8/ASCII

    本测试容错:若样本不存在则跳过;若样本已是 UTF-8 则断言原样;
    若样本为其他编码则断言转码成功。两种情况都算通过。
    """

    SAMPLE_DIR = Path(__file__).resolve().parent / "strm.test.A"

    @pytest.mark.parametrize("rel_path", [
        "anime/ShowA/Season 01/S01E01.chs.简体.srt",
        "anime/ShowA/Season 01/S01E01.cht.繁體.ass",
        "anime/ShowA/Season 01/S01E01.eng.srt",
        "movies/Inception/Inception.chs.简体.srt",
        "movies/Matrix/Matrix.cht.繁體.ass",
    ])
    def test_real_sample_roundtrip(self, rel_path, tmp_path):
        """对真实样本:复制后目标必须是合法 UTF-8"""
        src = self.SAMPLE_DIR / rel_path
        if not src.exists():
            pytest.skip(f"样本不存在: {src}")

        dst = tmp_path / "out" / src.name
        copy_subtitle_utf8(src, dst)

        # 无论源是什么编码,目标必须可作 UTF-8 严格解码
        result = dst.read_bytes()
        assert not result.startswith(codecs.BOM_UTF8), "目标不应包含 BOM"
        # 严格 UTF-8 解码必须成功
        result.decode("utf-8", errors="strict")
