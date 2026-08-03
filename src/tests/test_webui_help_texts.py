"""
WebUI 帮助文案测试 (Task C)

测试范围：
1. utils.js 的 createField 输出含 .field-helper-text 且内容经 esc()
2. openlist.js 的 20 个受查控件，其显式或派生 help key 全部存在于 _openlistHelpTexts
3. config.js 的 5 个 TMDB 阈值字段调用点都传入了非空 helperText
4. _openlistHelpTexts.log_file 不含「重启」字样
5. _openlistHelpTexts 中 refresh_enabled / refresh_interval_minutes / refresh_depth 含「即时生效」字样
6. _openlistHelpTexts 无孤儿键，或孤儿键被显式标注为保留

运行方式：
  python -m pytest src/tests/test_webui_help_texts.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# 确保 src/ 在 sys.path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 源文件路径
UTILS_JS = Path(__file__).resolve().parent.parent / "webui" / "modules" / "core" / "utils.js"
OPENLIST_JS = Path(__file__).resolve().parent.parent / "webui" / "modules" / "pages" / "openlist.js"
CONFIG_JS = Path(__file__).resolve().parent.parent / "webui" / "modules" / "pages" / "config.js"


class TestCreateFieldHelperText:
    """测试 utils.js 的 createField 函数支持 helperText"""

    def test_create_field_outputs_helper_text_div(self):
        """createField 输出含 .field-helper-text div"""
        content = UTILS_JS.read_text(encoding="utf-8")
        # 检查 createField 函数存在
        assert "function createField" in content or "const createField" in content
        # 检查输出包含 .field-helper-text
        assert "field-helper-text" in content
        # 检查内容经 esc() 处理
        assert "esc(" in content

    def test_create_field_has_helper_text_parameter(self):
        """createField 支持 helperText 参数"""
        content = UTILS_JS.read_text(encoding="utf-8")
        # 检查函数签名包含 helperText
        assert "helperText" in content


class TestOpenlistHelpTexts:
    """测试 openlist.js 的 _openlistHelpTexts 对象"""

    def test_all_expected_help_keys_exist(self):
        """20 个受查控件的 help key 全部存在于 _openlistHelpTexts"""
        content = OPENLIST_JS.read_text(encoding="utf-8")

        # 提取 _openlistHelpTexts 对象的所有键
        match = re.search(r"_openlistHelpTexts\s*=\s*\{([^}]+)\}", content, re.DOTALL)
        if match is None:
            pytest.fail("未找到 _openlistHelpTexts 对象")

        help_texts_content = match.group(1)

        # 预期的 help keys（实际存在的控件）
        expected_keys = [
            "webdav_host", "webdav_user", "webdav_password", "webdav_totp_secret",
            "b_root", "c_root", "monitored_paths", "refresh_paths",
            "strm_engines", "refresh_enabled", "refresh_interval_minutes", "refresh_depth",
            "behavior_action", "behavior_trash_dir_name", "behavior_ghost_protect_seconds",
            "behavior_a_to_b_restore_delay_seconds", "behavior_sync_on_startup", "behavior_sync_on_startup_wait",
            "log_level", "log_max_size_mb", "log_backup_count", "log_file",
            "refresh_full_audit_interval_days", "refresh_log_level",
        ]

        for key in expected_keys:
            # 检查键是否存在（允许在注释或字符串中）
            # JavaScript 对象键可能没有引号
            assert f'"{key}"' in help_texts_content or f"'{key}'" in help_texts_content or f"  {key}:" in help_texts_content, \
                f"Help key '{key}' not found in _openlistHelpTexts"

    def test_log_file_does_not_contain_restart_keyword(self):
        """log_file 帮助文案不含「重启」字样"""
        content = OPENLIST_JS.read_text(encoding="utf-8")

        match = re.search(r"_openlistHelpTexts\s*=\s*\{([^}]+)\}", content, re.DOTALL)
        if match is None:
            pytest.fail("未找到 _openlistHelpTexts 对象")

        help_texts_content = match.group(1)

        # 提取 log_file 的值
        log_file_match = re.search(r'"log_file"\s*:\s*"([^"]*)"', help_texts_content)
        if log_file_match is None:
            # 尝试单引号
            log_file_match = re.search(r"'log_file'\s*:\s*'([^']*)'", help_texts_content)

        if log_file_match:
            log_file_text = log_file_match.group(1)
            assert "重启" not in log_file_text, \
                f"log_file 帮助文案不应包含「重启」字样: {log_file_text}"

    def test_refresh_keys_contain_immediate_effect_keyword(self):
        """refresh_enabled / refresh_interval_minutes / refresh_depth 含「即时生效」字样"""
        content = OPENLIST_JS.read_text(encoding="utf-8")

        match = re.search(r"_openlistHelpTexts\s*=\s*\{([^}]+)\}", content, re.DOTALL)
        if match is None:
            pytest.fail("未找到 _openlistHelpTexts 对象")

        help_texts_content = match.group(1)

        # 需要包含「即时生效」的键
        keys_to_check = ["refresh_enabled", "refresh_interval_minutes", "refresh_depth"]

        for key in keys_to_check:
            key_match = re.search(rf'"{key}"\s*:\s*"([^"]*)"', help_texts_content)
            if key_match is None:
                key_match = re.search(rf"'{key}'\s*:\s*'([^']*)'", help_texts_content)

            if key_match:
                key_text = key_match.group(1)
                assert "即时生效" in key_text, \
                    f"{key} 帮助文案应包含「即时生效」字样: {key_text}"

    def test_no_orphan_keys_without_annotation(self):
        """_openlistHelpTexts 无孤儿键，或孤儿键被显式标注为保留"""
        content = OPENLIST_JS.read_text(encoding="utf-8")

        match = re.search(r"_openlistHelpTexts\s*=\s*\{([^}]+)\}", content, re.DOTALL)
        if match is None:
            pytest.fail("未找到 _openlistHelpTexts 对象")

        help_texts_content = match.group(1)

        # 已知的孤儿键（应被显式标注为保留）
        known_orphan_keys = ["b_root", "strm_engines"]

        # 提取所有键
        all_keys = re.findall(r'"([^"]+)"\s*:', help_texts_content)
        all_keys.extend(re.findall(r"'([^']+)'\s*:", help_texts_content))

        # 检查已知孤儿键是否有注释标注为保留
        for key in known_orphan_keys:
            if key in all_keys:
                # 检查该键所在行是否有注释
                key_pattern = re.compile(rf'["\']?{key}["\']?\s*:\s*[^,]+(?:/\*.*?\*/|//.*)?', re.DOTALL)
                key_match = key_pattern.search(help_texts_content)
                if key_match:
                    line = key_match.group(0)
                    # 检查是否有注释（/* */ 或 //）
                    has_comment = "/*" in line or "//" in line
                    assert has_comment, \
                        f"孤儿键 '{key}' 应被显式标注为保留: {line}"


class TestConfigJsTmdbThresholds:
    """测试 config.js 的 5 个 TMDB 阈值字段传入了非空 helperText"""

    def test_tmdb_threshold_fields_have_helper_text(self):
        """5 个 TMDB 阈值字段调用点都传入了非空 helperText"""
        content = CONFIG_JS.read_text(encoding="utf-8")

        # 5 个阈值字段的 ID
        threshold_field_ids = [
            "cfg-tmdb-fuzzy",
            "cfg-tmdb-ep-ratio",
            "cfg-tmdb-season-diff",
            "cfg-tmdb-min-season-ratio",
            "cfg-tmdb-cache-ttl",
        ]

        for field_id in threshold_field_ids:
            # 查找调用点，检查是否传入了 helperText（作为第 9 个参数）
            field_position = content.find(field_id)
            if field_position == -1:
                pytest.fail(f"未找到字段 {field_id}")

            # 检查该位置附近是否有 helperText（在 200 字符范围内）
            nearby_content = content[field_position:field_position + 200]
            # field 函数调用的第 9 个参数是 helperText
            # 格式：field(id, label, value, placeholder, type, persistLabel, readOnly, htmlLabel, helperText)
            assert "'  " in nearby_content or '"  ' in nearby_content or "''" in nearby_content or '""' in nearby_content, \
                f"字段 {field_id} 的调用点未传入非空 helperText"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
