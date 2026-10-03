"""8579 默认端口与 0.0.0.0 绑定地址的单一事实源守护。

[webui] 默认值此前散落多处，本契约锁定：
- src/config.py WebUIConfig dataclass 默认（事实源，本文件钉死其值）
- config.toml.example 解析结果 == dataclass 默认（example 文件首次获得
  真实执行覆盖）
- config.toml（仓库内运行配置，gitignore 管理，不随仓库分发；缺席时
  skipif——洁净检出无此文件，在场时仍守护端口漂移）
- 后台带Bridge启动webui.vbs 内嵌回退默认 == dataclass 默认
- src/webui/routes.py 端口回退字面量 == dataclass 默认

测试夹具（test_config.py 的 _MINIMAL_TOML）属有意最小样例，不在契约内。
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import AppConfig, WebUIConfig  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_TOML = PROJECT_ROOT / "config.toml"
CONFIG_EXAMPLE = PROJECT_ROOT / "config.toml.example"
VBS_PATH = PROJECT_ROOT / "后台带Bridge启动webui.vbs"
ROUTES_PATH = PROJECT_ROOT / "src" / "webui" / "routes.py"


class TestPortSingleSource:
    def test_dataclass_defaults_are_pinned(self):
        cfg = WebUIConfig()
        assert cfg.port == 8579
        assert cfg.bind == "0.0.0.0"

    def test_config_example_parses_to_dataclass_defaults(self):
        cfg = AppConfig.from_file(str(CONFIG_EXAMPLE))
        assert cfg.webui.port == WebUIConfig().port
        assert cfg.webui.bind == WebUIConfig().bind

    @pytest.mark.skipif(
        not CONFIG_TOML.is_file(),
        reason="config.toml 为 gitignore 的用户本地运行配置，洁净检出无此"
               "文件；在场时本断言守护 [webui].port 不漂移")
    def test_committed_config_toml_keeps_default_port(self):
        data = tomllib.loads(CONFIG_TOML.read_text(encoding="utf-8"))
        assert data["webui"]["port"] == WebUIConfig().port, (
            "config.toml 的 [webui].port 与 dataclass 默认漂移；若为有意的"
            "端口自定义，请与 docs 默认值说明一起同步更新")

    def test_vbs_fallback_port_matches_dataclass_default(self):
        # 实测编码为 UTF-8 带 BOM（EF BB BF）；文件首行自称 UTF-16LE 属
        # 陈旧注释，字节取证为准（Fact Supremacy 纠偏，见证据日志）。
        text = VBS_PATH.read_bytes().decode("utf-8-sig")
        m = re.search(r"GetWebUIPort\s*=\s*(\d+)", text)
        assert m is not None, "vbs 缺少 GetWebUIPort 默认端口赋值"
        assert int(m.group(1)) == WebUIConfig().port, (
            "vbs 内嵌回退端口与 config.py 默认不一致")

    def test_routes_port_fallback_literal_matches_dataclass_default(self):
        src = ROUTES_PATH.read_text(encoding="utf-8")
        m = re.search(
            r'webui_port\s*=\s*getattr\(webui_cfg,\s*"port",\s*(\d+)\)'
            r"\s*if\s+webui_cfg\s+else\s+(\d+)", src)
        assert m is not None, "routes.py 端口回退表达式形态变更，请同步本契约"
        assert int(m.group(1)) == WebUIConfig().port
        assert int(m.group(2)) == WebUIConfig().port
