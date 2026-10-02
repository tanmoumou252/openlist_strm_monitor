"""_validate_strm_engines 纯函数单元测试。

护栏目标：在 openlist.strm_engines 写入 DB 前校验其形态，拦截异常载荷
（如误把全部引擎/坏结构塞入）。详见 src/webui/routes.py。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# 冗余保护：确保 src/ 在 sys.path（与 test_webui_http.py 一致）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webui.routes import _validate_strm_engines  # noqa: E402


class TestValidateStrmEngines:
    def test_empty_list_ok(self):
        assert _validate_strm_engines("[]") is True

    def test_single_engine_ok(self):
        v = json.dumps([{"engine": "/测试a",
                         "monitored_paths": ["/m/电影"]}])
        assert _validate_strm_engines(v) is True

    def test_multi_engine_ok(self):
        v = json.dumps([
            {"engine": "/a", "monitored_paths": ["/m1"]},
            {"engine": "/b", "monitored_paths": ["/m2", "/m3"]},
        ])
        assert _validate_strm_engines(v) is True

    def test_non_json_rejected(self):
        assert _validate_strm_engines("not-json") is False

    def test_non_array_rejected(self):
        assert _validate_strm_engines('{"engine":"/a"}') is False

    def test_element_not_dict_rejected(self):
        assert _validate_strm_engines('["/a"]') is False

    def test_missing_engine_key_rejected(self):
        assert _validate_strm_engines('[{"monitored_paths":[]}]') is False

    def test_empty_engine_string_rejected(self):
        assert _validate_strm_engines('[{"engine":"","monitored_paths":[]}]') is False

    def test_monitored_paths_not_list_rejected(self):
        assert _validate_strm_engines(
            '[{"engine":"/a","monitored_paths":"x"}]') is False

    def test_monitored_paths_non_str_rejected(self):
        assert _validate_strm_engines(
            '[{"engine":"/a","monitored_paths":[1]}]') is False
