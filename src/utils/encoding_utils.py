"""Subtitle encoding normalization utilities (convert to UTF-8 without BOM)."""

from __future__ import annotations

import codecs
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def copy_subtitle_utf8(src: str | Path, dst: str | Path) -> None:
    """
    复制字幕文件到目标路径,途中将内容标准化为无 BOM 的 UTF-8。

    语义与 ``shutil.copyfile(src, dst)`` 等价:读源、写目标、返回 None。
    唯一差异:写入前对字节做 UTF-8 标准化(去 BOM / 非 UTF-8 转码)。

    fail-safe:若编码无法识别或转换异常,回退为原样字节复制,仅打 WARNING,
    确保不会因为编码问题丢失字幕文件。上游可将 ``shutil.copyfile(sub_file, target)``
    一行替换为本函数,无需其它改动。
    """

    src_path = Path(src)
    dst_path = Path(dst)

    try:
        src_bytes = src_path.read_bytes()
    except OSError as exc:
        # 源读取失败属于真实错误(与 shutil.copyfile 行为一致,让其抛出),
        # 但为避免上游 try 块只捕获宽泛 Exception 时被掩盖,这里明确打 WARNING 再抛。
        logger.warning("[字幕编码转换] 读取源文件失败,回退原样复制: %s (%s)", src_path, exc)
        shutil.copyfile(src_path, dst_path)
        return

    if not src_bytes:
        # 空文件:直接写空目标,避免后续无意义处理。
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(b"")
        return

    try:
        utf8_bytes, source_encoding = _normalize_to_utf8(src_bytes)
    except UnicodeError as exc:
        # 编码无法识别:fail-safe 回退为原样字节复制,不丢字幕。
        logger.warning(
            "[字幕编码转换] 无法识别编码,回退原样复制: %s -> %s (%s)",
            src_path, dst_path, exc,
        )
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        dst_path.write_bytes(src_bytes)
        return

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # 写入失败不回退:源已读取成功,目标写入失败属于真实错误,
    # 与 shutil.copyfile 行为一致,让上游 except 捕获。
    dst_path.write_bytes(utf8_bytes)

    if source_encoding is not None:
        logger.debug(
            "[字幕编码转换] %s -> %s (%s -> utf-8, %d -> %d bytes)",
            src_path, dst_path, source_encoding,
            len(src_bytes), len(utf8_bytes),
        )


def _normalize_to_utf8(data: bytes) -> tuple[bytes, str | None]:
    """
    将字节内容标准化为无 BOM 的 UTF-8。

    返回 ``(utf8_bytes, source_encoding)``:
      - ``source_encoding`` 为 ``None`` 表示原内容已是无 BOM 的 UTF-8,无需转换。
      - 否则 ``source_encoding`` 为识别出的原始编码名(如 ``"gb18030"``)。

    若无法识别编码则抛 ``UnicodeError``。
    """

    if not isinstance(data, bytes):
        raise TypeError("data 必须是 bytes 类型")

    if not data:
        return data, None

    # UTF-8 BOM:内容本身已是 UTF-8,只需移除 BOM。
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):], "utf-8-sig"

    # 严格验证是否已经是无 BOM 的 UTF-8。
    # 注意:仅"严格解码成功"不足以判定为 UTF-8 —— UTF-16-LE 无 BOM 字节流
    # (如 'A\x00B\x00...')也能被 UTF-8 严格解码成含 NUL 字符的乱码。
    # 真正的 UTF-8 字幕文本不会包含 NUL 字符,因此额外检查 NUL 字符可排除误判。
    try:
        decoded = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        if "\x00" not in decoded:
            return data, None

    # 非 UTF-8:检测原始编码并转码。
    source_encoding = _detect_non_utf8_encoding(data)

    text = data.decode(source_encoding, errors="strict")

    # 清除 UTF-16 / UTF-32 解码后可能残留的 BOM 字符。
    text = text.removeprefix("\ufeff")

    return text.encode("utf-8"), source_encoding


def _detect_non_utf8_encoding(data: bytes) -> str:
    """
    检测非 UTF-8 字幕内容的字符编码。

    仅模块内部调用。检测顺序固定,且 UTF-32 必须先于 UTF-16 判断
    (UTF-32 BOM 的部分字节可能匹配 UTF-16 BOM)。

    关键修复:UTF-16 无 BOM 启发式必须先于 GB18030/Big5 尝试 ——
    GB18030 编码空间极广,能严格解码 UTF-16 字节流成乱码,
    若放在启发式之前会让 UTF-16 无 BOM 字幕全部误判为 GB18030。

    关键修复:Big5 与 GB18030 存在编码空间重叠(Big5 字节序列往往也能被
    GB18030 严格解码成乱码),因此需要用 CJK 字符命中率消歧,
    不能简单"GB18030 优先命中即返回"。
    """

    # UTF-32 BOM(LE / BE)。
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return "utf-32"

    # UTF-16 BOM(LE / BE)。
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"

    # UTF-16 无 BOM 启发式判断(必须在 GB18030/Big5 之前)。
    utf16_encoding = _detect_utf16_without_bom(data)
    if utf16_encoding is not None:
        return utf16_encoding

    # 简繁中文常见编码消歧。
    # GB18030 是 GB2312 / GBK 的超集,编码空间极广,能严格解码大量非中文字节,
    # 因此不能简单"先试 GB18030 命中即返回"——Big5 字节也能被它解码成乱码。
    # 改为:两个候选都尝试严格解码,优先返回解码结果中 CJK 表意文字命中率更高的那个。
    return _detect_chinese_encoding(data)


def _detect_chinese_encoding(data: bytes) -> str:
    """
    在 GB18030 与 Big5 之间消歧。

    两者都能严格解码同一字节序列的情况很常见(Big5 字节往往也能被 GB18030
    解码成乱码)。用 CJK 统一表意文字(U+4E00..U+9FFF)在解码结果中的
    命中率作为判据:正确编码的解码结果应包含大量 CJK 字符,误判编码的
    解码结果中 CJK 字符占比通常很低或字符错乱。

    若只有其中一个能严格解码,直接返回那个;若两个都能解码,返回 CJK
    占比更高的;若都不能,抛 UnicodeError。
    """

    candidates: list[tuple[str, str]] = []
    for encoding in ("gb18030", "big5"):
        try:
            decoded = data.decode(encoding, errors="strict")
            candidates.append((encoding, decoded))
        except UnicodeDecodeError:
            continue

    if not candidates:
        raise UnicodeError("无法识别字幕内容的字符编码")

    if len(candidates) == 1:
        return candidates[0][0]

    # 两个候选都能解码:比较 CJK 表意文字命中率。
    def cjk_ratio(text: str) -> float:
        if not text:
            return 0.0
        cjk_count = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        return cjk_count / len(text)

    # 返回 CJK 占比更高的编码;占比相同则倾向 GB18030(简体场景更常见)。
    candidates.sort(key=lambda item: cjk_ratio(item[1]), reverse=True)
    if cjk_ratio(candidates[0][1]) == cjk_ratio(candidates[1][1]):
        # 占比相同,优先 GB18030(简体字幕在中文社区更普遍)。
        for enc, _ in candidates:
            if enc == "gb18030":
                return enc
        return candidates[0][0]
    return candidates[0][0]


def _detect_utf16_without_bom(data: bytes) -> str | None:
    """
    检测无 BOM 的 UTF-16 LE / BE。

    基于 null byte 在奇偶位置上的占比做启发式判断:字幕文本 ASCII 占比高,
    UTF-16 编码下 ASCII 字符的高字节为 0x00,因此 null byte 会集中出现在
    偶数位置(LE)或奇数位置(BE)。采样前 4096 字节以加速。
    """

    sample = data[:4096]

    if len(sample) < 4:
        return None

    even_bytes = sample[0::2]
    odd_bytes = sample[1::2]

    even_null_ratio = even_bytes.count(0) / len(even_bytes)
    odd_null_ratio = odd_bytes.count(0) / len(odd_bytes)

    if odd_null_ratio > 0.3 and even_null_ratio < 0.1:
        encoding = "utf-16-le"
    elif even_null_ratio > 0.3 and odd_null_ratio < 0.1:
        encoding = "utf-16-be"
    else:
        return None

    # 启发式命中后仍需严格解码验证,避免误判。
    try:
        data.decode(encoding, errors="strict")
        return encoding
    except UnicodeDecodeError:
        return None
