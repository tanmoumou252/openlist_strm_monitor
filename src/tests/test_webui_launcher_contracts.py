"""启动器契约守护。

vbs / bat 启动脚本与 Python 侧的一致性：
- vbs 实测编码为 UTF-8 带 BOM（EF BB BF）；文件首行自称 UTF-16LE(FF FE)
  属陈旧注释，字节取证为准（见证据日志 Fact Supremacy 纠偏记录）
- vbs 设置 BRIDGE_HEADLESS="1"，与 server.py 的消费字面量互证
- vbs 内嵌端口回退默认与 config.py 的 WebUIConfig().port 一致
- vbs 与两个 bat 均含 Python >= 3.11 版本检查
- bat 依赖探测 import 的包名集合 ⊆ requirements.txt 顶层包名集合
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import WebUIConfig  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[2]
VBS_PATH = PROJECT_ROOT / "后台带Bridge启动webui.vbs"
BAT_EMBEDDED = PROJECT_ROOT / "嵌入式启动.bat"
BAT_ENV = PROJECT_ROOT / "环境变量启动.bat"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
SERVER_PY = PROJECT_ROOT / "src" / "webui" / "server.py"


def _vbs_text() -> str:
    raw = VBS_PATH.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", (
        "vbs 实测为 UTF-8 带 BOM（EF BB BF）；若编码被改动（如转回 "
        "UTF-16LE 或去 BOM），请同步更新本契约与首行编码说明注释")
    return raw.decode("utf-8-sig")


def _bat_text(path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


class TestVbsContract:
    def test_utf8_bom_present(self):
        _vbs_text()  # 断言在 helper 内

    def test_sets_bridge_headless_consistent_with_server(self):
        assert 'BRIDGE_HEADLESS") = "1"' in _vbs_text(), (
            "vbs 必须设置 BRIDGE_HEADLESS=1")
        server = SERVER_PY.read_text(encoding="utf-8")
        assert 'os.environ.get("BRIDGE_HEADLESS") == "1"' in server, (
            "server.py 的 BRIDGE_HEADLESS 消费语义变更，与 vbs 设置不一致")

    def test_fallback_port_matches_config_default(self):
        m = re.search(r"GetWebUIPort\s*=\s*(\d+)", _vbs_text())
        assert m is not None, "vbs 缺少 GetWebUIPort 默认端口回退"
        assert int(m.group(1)) == WebUIConfig().port

    def test_python_version_check_present(self):
        assert "sys.version_info >= (3, 11)" in _vbs_text()


class TestBatContracts:
    def test_python_version_check_present_in_both_bats(self):
        for path in (BAT_EMBEDDED, BAT_ENV):
            assert "sys.version_info >= (3, 11)" in _bat_text(path), (
                f"{path.name} 缺 Python 3.11 检查")

    def test_dependency_probe_imports_subset_of_requirements(self):
        req_names = set()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"[A-Za-z0-9._-]+", line)
            if m:
                req_names.add(m.group(0).lower())
        for path in (BAT_EMBEDDED, BAT_ENV):
            probed = set(
                re.findall(r"import\s+(watchdog|requests|lxml)\b",
                           _bat_text(path)))
            assert probed == {"watchdog", "requests", "lxml"}, (
                f"{path.name} 依赖探测集变化 {sorted(probed)}，需与 "
                "requirements.txt 顶层包对齐")
            assert probed <= req_names, (
                f"{path.name} 探测的包不在 requirements.txt 顶层: {probed - req_names}")
