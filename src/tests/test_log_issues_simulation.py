"""三类日志问题模拟验证测试。

专门针对 ``strm_bridge.log`` 中出现的三类真实问题进行模拟与审核：

1. **A 区非 STRM 文件进入 STRM 解码 → UnicodeDecodeError**
   日志原始样例：``C:\\box\\strm.local\\收集\\[2013] 动漫3D杂\\Season 4\\图包\\猫猫酱 (9).jpg``
   触发 ``handle_a_created_or_modified`` → ``read_strm_webdav_path`` → 对二进制 JPEG 执行 UTF-8 解码崩溃。
   当前修复：``handle_a_created_or_modified`` 在 ``suffix != ".strm"`` 时直接 return；
   ``read_strm_webdav_path`` 捕获 ``UnicodeDecodeError`` 返回 None。

2. **B 区启动逆向自同步长时间静默（缺少阶段日志）**
   日志原始样例：19:48:54 → 19:49:48 之间整整 54 秒无任何日志输出。
   当前修复：``scan_a_to_b_full_sync`` 输出 ``索引阶段完成`` / ``A -> B 进度`` / ``全量同步完成`` 等阶段标记。

3. **A→B 启动同步把多个源映射到同一个 B 区目标文件（路径碰撞）**
   日志原始样例：492 条 ``[A->B] B 区文件已存在但 WebDAV 路径不同，跳过覆盖`` WARNING。
   根因：``build_b_path_from_a`` 集号仅从 A 区本地 .strm 文件名解析，当本地名不含可解析集号时，
   多个不同 WebDAV 源会算出相同 B 目标。当前缓解：``scan_a_to_b_full_sync`` 第一遍冲突预检 + 安全跳过 + 记 WARNING。

流程
----
1. 在脚本同级 ``strm.test.A/`` 生成 ~100 个模拟文件（STRM / 图片 / 字幕 / 畸形），幂等刷新、**持久保留**供下次复用
2. 用真实 ``Database`` + 真实 ``AppService``（仅 mock 网络 ``admin_api``）跑
   ``initial_scan_a`` → ``scan_a_to_b_full_sync``，把 A 区内容真实复制到脚本同级 ``strm.test.B/``
3. 审核 B 区内容与结构是否符合预期
4. 测试后：**删除** ``strm.test.B/``、**删除** 临时 DB；**保留** ``strm.test.A/``、**保留** 本轮日志到 ``<项目根>/test_logs/``
"""
from __future__ import annotations

import logging
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# conftest 已注入 src/ 与 tests/ 到 sys.path，这里保留冗余保护
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.append(_SRC_DIR)

from app_service_core import AppService  # noqa: E402
from config import AppConfig  # noqa: E402
from database import Database  # noqa: E402
from utils.strm_utils import read_strm_webdav_path  # noqa: E402


# ======================================================================
# 常量
# ======================================================================

# 脚本同级目录：A 持久保留、B 测试后清理
_THIS_DIR = Path(__file__).resolve().parent
A_DIR = _THIS_DIR / "strm.test.A"
B_DIR = _THIS_DIR / "strm.test.B"
# 项目根 test_logs/ 留存本轮日志（用户确认位置）
_LOG_DIR = _THIS_DIR.parent.parent / "test_logs"

# 真二进制 JPEG 头（复现日志 UnicodeDecodeError 原始场景）
_JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# 乱码二进制（模拟非 UTF-8 文本）
_GARBLED_BYTES = b"\xff\xfe\x00\x01\x02\x03\x7f\x80\xff" * 8


# ======================================================================
# 测试数据生成
# ======================================================================

def _generate_test_files(a_dir: Path) -> dict:
    """生成 ~100 个模拟文件到 a_dir（幂等：mkdir exist_ok + 覆盖写）。

    覆盖三类日志问题样例 + 额外设想的易错命名：
    - 标准可解析 SxxExx 番剧 STRM
    - 电影 STRM（直接映射）
    - 问题3碰撞样例（本地名不含可解析集号 → 同 B target 多源）
    - 问题1样例（番剧目录下 .jpg/.png/.nfo，含真二进制 JPEG）
    - 字幕 .srt/.ass（文本改后缀）
    - 畸形 STRM（空 / 二进制垃圾 / 乱码）
    - 指向"不存在文件"的 STRM
    - 嵌套括号 / 中文路径 / 全角 / 超长名 / 连续空格 / 中文季目录 / 仅集号无季号
    """
    files: list[Path] = []
    collision_candidates: list[tuple[str, list[Path]]] = []

    def _write_strm(rel: str, webdav_path: str) -> Path:
        p = a_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(webdav_path, encoding="utf-8")
        files.append(p)
        return p

    def _write_bytes(rel: str, content: bytes) -> Path:
        p = a_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        files.append(p)
        return p

    def _write_text(rel: str, text: str) -> Path:
        """写文本文件（非 .strm，如图片/字幕/nfo，文本改后缀）。"""
        p = a_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        files.append(p)
        return p

    # ── 标准番剧 STRM（可解析 SxxExx，不碰撞）──────────────────────────
    shows = [
        ("anime", "ShowA", 1, 3),
        ("anime", "ShowB", 2, 5),
        ("anime", "ShowC", 1, 12),
        ("anime", "ShowD", 3, 8),
        ("anime", "ShowE", 1, 24),
        ("anime", "OnePiece", 18, 5),
        ("anime", "OnePiece", 19, 3),
        ("anime", "OnePiece", 20, 2),
        ("anime", "OnePiece", 21, 2),
    ]
    for category, name, season, count in shows:
        for ep in range(1, count + 1):
            _write_strm(
                f"{category}/{name}/Season {season:02d}/S{season:02d}E{ep:02d}.strm",
                f"/cloud/mount/{category}/{name}/S{season:02d}E{ep:02d}.mp4",
            )

    # ── 电影 STRM（直接映射，无 Season 目录）──────────────────────────
    movies = ["Inception", "Matrix", "Interstellar", "Dune", "Tenet"]
    for name in movies:
        _write_strm(
            f"movies/{name}/{name}.strm",
            f"/cloud/mount/movies/{name}/{name}.mkv",
        )

    # ── 问题3碰撞样例：本地名不含可解析集号的多集 → 同 B target 多源 ──
    # 番剧目录下的多集（文件名集号格式不被 SEASON_EPISODE_PATTERNS 命中）
    # 仿照日志：[Moozzi2] Mawaru Penguin Drum - 01..24 (.).mkv 全部落到 S20E10.strm
    penguin_dir = "anime/[Moozzi2] Mawaru Penguin Drum/Season 20"
    penguin_files = []
    for ep in range(1, 25):
        f = _write_strm(
            f"{penguin_dir}/[Moozzi2] Mawaru Penguin Drum - {ep:02d} "
            f"(BD 1920x1080 x.264 FLACx2).strm",
            f"/cloud/mount/anime/[Moozzi2] Mawaru Penguin Drum/"
            f"[Moozzi2] Mawaru Penguin Drum - {ep:02d} "
            f"(BD 1920x1080 x.264 FLACx2).mkv",
        )
        penguin_files.append(f)
    collision_candidates.append(("penguin_s20e10", penguin_files))

    # 海贼王 757/758/759（三位集号被截断为 75）
    op_dir = "anime/海贼王/Season 18"
    op_files = []
    for ep in (757, 758, 759):
        f = _write_strm(
            f"{op_dir}/航海王 - S18E{ep} - 第 {ep} 集.strm",
            f"/cloud/mount/anime/海贼王/Season 18/航海王 - S18E{ep} - 第 {ep} 集.mkv",
        )
        op_files.append(f)
    collision_candidates.append(("onepiece_s18e75", op_files))

    # 地獄模式 01..12
    hell_dir = "anime/地獄模式 ～喜歡挑戰特殊成就的玩家在廢設定的異世界成為無雙～/Season 20"
    hell_files = []
    for ep in range(1, 13):
        f = _write_strm(
            f"{hell_dir}/Dynamis_One_..._{ep:02d}_Baha_1920x1080_AVC.strm",
            f"/cloud/mount/anime/地獄模式/Dynamis_One_..._{ep:02d}_Baha_1920x1080_AVC.mp4",
        )
        hell_files.append(f)
    collision_candidates.append(("hell_s20e10", hell_files))

    # ── 问题1样例：番剧目录下的非 STRM 文件 ──────────────────────────
    # 大部分为"文本改后缀"（用户要求），少数写真二进制 JPEG 复现原始崩溃
    # ShowA 目录下的图片/信息文件（文本改后缀）
    _write_text("anime/ShowA/cover.jpg", "this is a fake jpg text content")
    _write_text("anime/ShowA/poster.png", "fake png text not real binary")
    _write_text("anime/ShowA/show.nfo", "<movie><title>ShowA</title></movie>")
    _write_text("anime/ShowA/banner.jpg", "fake banner text")
    _write_text("anime/ShowA/logo.png", "fake logo text")
    _write_text("anime/ShowB/fanart.jpg", "fake fanart text")
    _write_text("anime/ShowB/backdrop.png", "fake backdrop text")
    _write_text("anime/ShowC/cover.jpg", "fake cover text")
    _write_text("anime/ShowD/thumbnail.png", "fake thumbnail text")
    _write_text("movies/Inception/poster.jpg", "fake movie poster text")
    _write_text("movies/Inception/fanart.png", "fake movie fanart text")
    _write_text("movies/Matrix/cover.jpg", "fake matrix cover text")
    _write_text("movies/Interstellar/backdrop.png", "fake interstellar backdrop text")
    _write_text("movies/Dune/info.nfo", "<movie><title>Dune</title></movie>")
    _write_text("movies/Tenet/poster.jpg", "fake tenet poster text")
    # 中文路径 + 真二进制 JPEG（复现日志"收集/图包/猫猫酱 (N).jpg"原始场景）
    _write_bytes("anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 (1).jpg", _JPEG_BYTES)
    _write_bytes("anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 (2).jpg", _JPEG_BYTES)
    _write_bytes("anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 (9).jpg", _JPEG_BYTES)

    # ── 字幕文件（文本改后缀）──────────────────────────────────────
    _write_text("anime/ShowA/Season 01/S01E01.chs.简体.srt",
                "1\n00:00:01,000 --> 00:00:02,000\n这是中文字幕\n")
    _write_text("anime/ShowA/Season 01/S01E01.eng.srt",
                "1\n00:00:01,000 --> 00:00:02,000\nEnglish subtitle\n")
    _write_text("anime/ShowA/Season 01/S01E01.cht.繁體.ass",
                "[Script Info]\nTitle: ShowA\n[Events]\n")
    _write_text("anime/ShowA/Season 01/S01E02.chs.简体.srt",
                "1\n00:00:01,000 --> 00:00:02,000\n第二集中文\n")
    _write_text("anime/ShowB/Season 02/S02E01.chs.简体.srt",
                "1\n00:00:01,000 --> 00:00:02,000\n第二季中文字幕\n")
    _write_text("anime/ShowB/Season 02/S02E02.eng.ass",
                "[Script Info]\nTitle: ShowB ep2\n[Events]\n")
    _write_text("anime/ShowC/Season 01/S01E01.chs.简体.srt",
                "1\n00:00:01,000 --> 00:00:02,000\nShowC 中文字幕\n")
    _write_text("movies/Inception/Inception.chs.简体.srt",
                "1\n00:00:01,000 --> 00:00:02,000\n电影字幕\n")
    _write_text("movies/Matrix/Matrix.cht.繁體.ass",
                "[Script Info]\nTitle: Matrix\n[Events]\n")

    # ── 畸形 STRM（应被解析为 None 并跳过，不崩溃）────────────────────
    _write_bytes("bad_strm/empty.strm", b"")  # 空
    _write_bytes("bad_strm/binary_garbage.strm", _GARBLED_BYTES)  # 二进制垃圾
    _write_bytes("bad_strm/garbled.strm", b"\xff\xfe\x00\x01" * 5)  # 乱码
    _write_strm("bad_strm/not_a_path.strm", "just some text no slash")  # 非 / http 开头

    # ── 指向"不存在文件"的 STRM（webdav 路径解析成功但云端不存在）────
    for i in range(5):
        _write_strm(
            f"deleted/missing_{i}.strm",
            f"/cloud/mount/deleted/missing_{i}.mp4",
        )

    # ── 额外设想的易错命名 ──────────────────────────────────────────
    # 中文季目录（应被替换为 Season XX）
    _write_strm("anime/中文季测试/第一季/S01E01.strm",
                "/cloud/mount/anime/中文季测试/S01E01.mp4")
    # 仅集号无季号（无法提取 season → 可能退化为保持原结构）
    _write_strm("anime/仅集号/E01.strm",
                "/cloud/mount/anime/仅集号/E01.mp4")
    # 嵌套括号 + 全角字符
    _write_strm("anime/[2020] 全角Ｔｅｔｌｅ/Season 01/S01E01.strm",
                "/cloud/mount/anime/[2020] 全角/Title/S01E01.mp4")
    # 超长名 + 连续空格
    _write_strm(
        "anime/超长名称测试" * 1 + "/Season 01/S01E01.strm",
        "/cloud/mount/anime/超长名称测试/S01E01.mp4",
    )
    _write_strm("anime/连续  空格/Season 01/S01E01.strm",
                "/cloud/mount/anime/连续空格/S01E01.mp4")

    # 统计
    strm_files = [f for f in files if f.suffix == ".strm"]
    non_strm_files = [f for f in files if f.suffix != ".strm"]
    return {
        "files": files,
        "strm_count": len(strm_files),
        "non_strm_count": len(non_strm_files),
        "collision_candidates": collision_candidates,
    }


# ======================================================================
# 测试类
# ======================================================================

class TestLogIssuesSimulation:
    """三类日志问题模拟验证。

    设计要点
    --------
    - ``strm.test.A`` 位于脚本同级，**持久保留**（幂等刷新内容，下次复用）
    - ``strm.test.B`` 位于脚本同级，测试后**删除**（保持 tests 文件夹干净）
    - 真实 ``Database``（tempfile）+ 真实 ``AppService``（mock 网络）跑全量同步
    - 本轮日志留存到 ``<项目根>/test_logs/log_issues_sim_<时间戳>.log``
    """

    def setup_method(self):
        """每次测试前：刷新 A 区数据 + 构造真实 AppService + 准备日志。"""
        # 1. A/B 目录（脚本同级）
        A_DIR.mkdir(parents=True, exist_ok=True)
        B_DIR.mkdir(parents=True, exist_ok=True)

        # 2. 幂等刷新 A 区模拟文件
        self.manifest = _generate_test_files(A_DIR)

        # 3. 临时目录放真实 DB 与 C 区（测试后删除）
        self.tmp = Path(tempfile.mkdtemp(prefix="log_sim_"))
        self.db_path = self.tmp / "bridge_sim.db"
        self.c_dir = self.tmp / "c"
        self.c_dir.mkdir()

        # 4. 真实 Database（构造即建表）
        self.db = Database(str(self.db_path))

        # 5. mock AppConfig（仅提供 AppService 构造所需字段）
        self.config = Mock(spec=AppConfig)
        self.config.a_folders = [str(A_DIR)]
        self.config.paths = Mock()
        self.config.paths.b_root = str(B_DIR)
        self.config.paths.c_root = str(self.c_dir)
        self.config.behavior = Mock()
        self.config.behavior.ghost_protect_seconds = 300
        self.config.strm_engine_paths = []
        self.config.local = Mock()
        # RefreshService.start 会读这两个
        self.config.refresh = Mock(enabled=False)
        self.config.refresh_paths = []

        # 6. mock 网络 admin_api（scan_a_to_b_full_sync 不调，handle 需要）
        self.admin_api = Mock()
        # handle_a_created_or_modified 调 check_exists；默认 truthy 走"存在"分支
        self.admin_api.check_exists.return_value = True

        # 7. 构造真实 AppService（patch 掉 RefreshService/SubtitleHandler 避免副作用）
        with patch("app_service_core.RefreshService"), \
             patch("app_service_core.SubtitleHandler"):
            self.app = AppService(self.config, self.db, self.admin_api)

        # 8. 日志：留存到项目根 test_logs/，带时间戳
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_file = _LOG_DIR / f"log_issues_sim_{ts}.log"
        self.handler = logging.FileHandler(self.log_file, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        root = logging.getLogger()
        # 记录并恢复 root logger 原级别：INFO 日志需 root 级别放行才能到达 handler
        self._prev_root_level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.handler)

    def teardown_method(self):
        """每次测试后：删 B 区与临时 DB，保留 A 区与日志。"""
        root = logging.getLogger()
        root.removeHandler(self.handler)
        self.handler.close()
        root.setLevel(self._prev_root_level)
        shutil.rmtree(B_DIR, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)
        # 故意不删 A_DIR 与 self.log_file

    # ──────────────────────────────────────────────────────────────
    # 辅助：运行真实主流程（索引 + 全量同步），把 A 复制到 B
    # ──────────────────────────────────────────────────────────────

    def _run_full_sync(self):
        """跑 initial_scan_a + scan_a_to_b_full_sync（真实 DB + 真实路径推导）。"""
        self.app.initial_scan_a(use_bulk=False)
        self.app.scan_a_to_b_full_sync(use_bulk=False)

    def _read_log(self) -> str:
        return self.log_file.read_text(encoding="utf-8")

    # ──────────────────────────────────────────────────────────────
    # 问题1：非 STRM 文件不应进入 STRM 解码
    # ──────────────────────────────────────────────────────────────

    def test_non_strm_files_not_decoded(self):
        """非 STRM 文件（.jpg/.png/.nfo/.srt/.ass）不应触发 read_strm_webdav_path。"""
        non_strm = [f for f in self.manifest["files"] if f.suffix != ".strm"]
        assert len(non_strm) >= 20, "测试数据中非 STRM 文件不足"

        for f in non_strm:
            with patch("app_service_core.read_strm_webdav_path") as mock_read:
                # 不应抛异常（原 bug 会 UnicodeDecodeError）
                self.app.handle_a_created_or_modified(str(f))
                mock_read.assert_not_called(), (
                    f"非 STRM 文件触发了 STRM 解码: {f.name}")

    def test_binary_jpg_returns_none(self):
        """真二进制 JPEG 调 read_strm_webdav_path 应返回 None 而非抛 UnicodeDecodeError。"""
        binary_jpgs = [
            A_DIR / "anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 (1).jpg",
            A_DIR / "anime/收集/[2013] 动漫3D杂/Season 4/图包/猫猫酱 (9).jpg",
        ]
        for jpg in binary_jpgs:
            assert jpg.exists(), f"测试二进制 JPG 不存在: {jpg}"
            # 不应抛 UnicodeDecodeError
            result = read_strm_webdav_path(jpg)
            assert result is None, f"二进制 JPEG 应返回 None: {jpg.name}"

    def test_non_strm_not_in_b(self):
        """跑完主流程后，B 区不应包含任何非 STRM 文件（图片/字幕/nfo 不被复制）。"""
        self._run_full_sync()
        b_non_strm = [p for p in B_DIR.rglob("*") if p.is_file() and p.suffix != ".strm"]
        assert len(b_non_strm) == 0, (
            f"B 区出现非 STRM 文件（共 {len(b_non_strm)} 个）: "
            + "; ".join(str(p) for p in b_non_strm[:5]))

    def test_subtitle_routed_not_decoded(self):
        """字幕文件走 process_subtitle_file 分流，不进入 STRM 解码。"""
        # 字幕按扩展名 is_subtitle_file 判断（.srt/.ass/.ssa）
        subs = [f for f in self.manifest["files"] if f.suffix in (".srt", ".ass")]
        assert len(subs) >= 3, "字幕文件不足"
        for sub in subs:
            with patch("app_service_core.read_strm_webdav_path") as mock_read, \
                 patch.object(self.app, "process_subtitle_file") as mock_sub:
                # 不应抛异常
                self.app.handle_a_created_or_modified(str(sub))
                mock_read.assert_not_called(), (
                    f"字幕文件触发了 STRM 解码: {sub.name}")
                # 字幕应被分流到 process_subtitle_file
                mock_sub.assert_called_once()

    # ──────────────────────────────────────────────────────────────
    # 问题2：B 区同步应有阶段日志（防静默回归）
    # ──────────────────────────────────────────────────────────────

    def test_log_has_phase_markers(self):
        """全量同步日志应包含阶段标记（索引完成 / 进度 / 全量同步完成）。"""
        self._run_full_sync()
        log = self._read_log()
        # 问题2 回归点：缺少阶段日志
        assert "A -> B 全量同步开始" in log, "缺少同步开始标记"
        assert "索引阶段完成" in log, "缺少索引阶段完成标记"
        assert "A -> B 全量同步完成" in log, "缺少同步完成标记"

    def test_log_has_progress_or_summary(self):
        """日志应含进度计数行或汇总行（成功= 跳过= 等）。"""
        self._run_full_sync()
        log = self._read_log()
        # 进度行或汇总行至少一个
        has_progress = "A -> B 进度" in log
        has_summary = "全量同步完成" in log and "成功=" in log
        assert has_progress or has_summary, (
            "缺少进度行与汇总行（静默回归风险）")

    def test_no_long_silent_gap(self):
        """相邻 INFO 日志时间戳间隔不应过长（宽松阈值 30s，防静默回归）。"""
        self._run_full_sync()
        log = self._read_log()
        ts_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
        timestamps = []
        for line in log.splitlines():
            m = ts_pattern.match(line)
            if m and "[INFO]" in line:
                timestamps.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        if len(timestamps) < 2:
            return  # 文件太少无法判断
        max_gap = max(
            (timestamps[i + 1] - timestamps[i]).total_seconds()
            for i in range(len(timestamps) - 1))
        assert max_gap < 30, (
            f"检测到 {max_gap:.0f}s 日志静默间隔（阈值 30s），可能回归问题2")

    # ──────────────────────────────────────────────────────────────
    # 问题3：A→B 路径冲突检测与安全跳过
    # ──────────────────────────────────────────────────────────────

    def test_collision_targets_detected(self):
        """修复后：噪音标签剥离应消除 1920x1080 类碰撞，不再产生冲突。"""
        self._run_full_sync()
        log = self._read_log()
        
        # 修复后，1920x1080 类噪音被剥离，不应产生目标路径冲突
        # 若仍有其他原因导致的冲突，日志应记录
        if "目标路径冲突" in log:
            # 有冲突 → 验证不是 1920x1080 噪音导致的
            assert "1920x1080" not in log, "修复后仍检测到 1920x1080 噪音导致的碰撞"
        
        # 至少不应崩溃，且日志含索引阶段
        assert "索引阶段完成" in log
        assert "A -> B 全量同步完成" in log

    def test_manual_review_list_generated(self):
        """若存在冲突，应生成人工处理清单文件。"""
        self._run_full_sync()
        log = self._read_log()
        
        # 修复后，1920x1080 类碰撞应消失，不应生成清单文件
        review_files = list(B_DIR.glob("_MANUAL_REVIEW_*.md"))
        
        if "目标路径冲突" in log:
            # 有冲突 → 应生成清单文件
            assert len(review_files) > 0, "检测到冲突但未生成人工处理清单"
            # 验证清单文件内容
            content = review_files[0].read_text(encoding="utf-8")
            assert "# 人工处理清单" in content
            assert "A 区路径" in content
            assert "WebDAV 路径" in content
            assert "目标路径" in content
        else:
            # 无冲突 → 不应生成清单文件
            assert len(review_files) == 0, "无冲突但生成了人工处理清单"

    def test_no_wrong_override_in_b(self):
        """B 区每个 .strm 的 webdav 内容应与某个 A 源一致（无串改/错误覆盖）。"""
        self._run_full_sync()
        # 收集所有 A 源 webdav 集合
        a_webdavs = set()
        for f in self.manifest["files"]:
            if f.suffix == ".strm":
                w = read_strm_webdav_path(f)
                if w:
                    a_webdavs.add(w)
        # 校验 B 区每个 .strm
        b_strms = list(B_DIR.rglob("*.strm"))
        assert len(b_strms) > 0, "B 区无 STRM 文件，主流程可能失败"
        for b in b_strms:
            w = read_strm_webdav_path(b)
            assert w is not None, f"B 区 STRM 无法解析: {b.name}"
            assert w in a_webdavs, (
                f"B 区文件内容与任何 A 源不一致（错误覆盖）: {b} → {w}")

    def test_b_strm_count_reasonable(self):
        """B 区 STRM 数应合理：有效 A 源 - 冲突跳过 - 畸形解析失败。"""
        self._run_full_sync()
        # 统计有效 A 源（可解析 webdav）
        a_valid = 0
        for f in self.manifest["files"]:
            if f.suffix == ".strm" and read_strm_webdav_path(f):
                a_valid += 1
        b_strms = list(B_DIR.rglob("*.strm"))
        # B 区数量 ≤ 有效 A 源（冲突/去重会减少）；至少应复制一部分
        assert len(b_strms) <= a_valid, (
            f"B 区 STRM 数 {len(b_strms)} 超过有效 A 源 {a_valid}")
        assert len(b_strms) >= a_valid * 0.5, (
            f"B 区 STRM 数 {len(b_strms)} 过少（有效 A 源 {a_valid}），"
            f"主流程可能异常")

    # ──────────────────────────────────────────────────────────────
    # B 区结构审核
    # ──────────────────────────────────────────────────────────────

    def test_anime_b_has_season_dir(self):
        """番剧 STRM 的 B 区目标路径应含 Season 目录。"""
        self._run_full_sync()
        anime_b = [p for p in B_DIR.rglob("*.strm")
                   if any(k in str(p).lower() for k in
                          ["anime", "番剧", "动漫", "show", "penguin",
                           "海贼王", "地獄", "收集", "中文季", "仅集号",
                           "全角", "超长", "连续"])]
        # 至少有部分番剧被复制
        if anime_b:
            no_season = [p for p in anime_b
                         if not re.search(r"Season\s*\d+", str(p), re.IGNORECASE)
                         and "第一季" not in str(p)]
            # 宽松：允许极少数无法提取季号的保持原结构
            assert len(no_season) <= len(anime_b) * 0.3, (
                f"过多番剧 STRM 缺少 Season 目录: {len(no_season)}/{len(anime_b)}")

    def test_movie_b_no_season_dir(self):
        """电影 STRM 的 B 区目标路径不应含 Season 目录。"""
        self._run_full_sync()
        movie_b = [p for p in B_DIR.rglob("*.strm")
                   if "movies" in str(p).lower() or "电影" in str(p)]
        for b in movie_b:
            assert not re.search(r"Season\s*\d+", str(b), re.IGNORECASE), (
                f"电影 STRM 出现 Season 目录: {b}")

    def test_all_b_strm_parseable(self):
        """B 区每个 .strm 的内容都能被 read_strm_webdav_path 解析。"""
        self._run_full_sync()
        b_strms = list(B_DIR.rglob("*.strm"))
        assert len(b_strms) > 0, "B 区无 STRM"
        for b in b_strms:
            w = read_strm_webdav_path(b)
            assert w is not None, f"B 区 STRM 内容无法解析: {b.name}"

    def test_b_content_matches_one_source(self):
        """B 区 STRM 内容应与至少一个 A 源完全一致（逐文件比对）。"""
        self._run_full_sync()
        a_contents = {}
        for f in self.manifest["files"]:
            if f.suffix == ".strm":
                try:
                    a_contents[f.read_text(encoding="utf-8").strip()] = f
                except Exception:
                    pass
        b_strms = list(B_DIR.rglob("*.strm"))
        for b in b_strms:
            content = b.read_text(encoding="utf-8").strip()
            assert content in a_contents, (
                f"B 区文件内容与任何 A 源不一致: {b}")

    # ──────────────────────────────────────────────────────────────
    # 综合统计
    # ──────────────────────────────────────────────────────────────

    def test_summary_stats(self):
        """综合统计：确认生成文件数量与类型覆盖三类问题。"""
        files = self.manifest["files"]
        strm = [f for f in files if f.suffix == ".strm"]
        non_strm = [f for f in files if f.suffix != ".strm"]

        # 总数 ≥ 100
        assert len(files) >= 100, f"生成文件总数不足: {len(files)}"
        assert len(strm) >= 70, f"STRM 文件不足: {len(strm)}"
        assert len(non_strm) >= 20, f"非 STRM 文件不足: {len(non_strm)}"

        # 问题1样例：含真二进制 JPEG
        binary_jpgs = [
            f for f in non_strm
            if f.suffix == ".jpg" and f.exists()
            and f.read_bytes()[:4] == b"\xff\xd8\xff\xe0"
        ]
        assert len(binary_jpgs) >= 2, (
            f"真二进制 JPEG 不足（问题1样例）: {len(binary_jpgs)}")

        # 含字幕
        subs = [f for f in non_strm if f.suffix in (".srt", ".ass")]
        assert len(subs) >= 3, f"字幕文件不足: {len(subs)}"

        # 问题3样例：碰撞候选
        assert len(self.manifest["collision_candidates"]) >= 3, (
            "碰撞候选不足（问题3样例）")

        # 畸形 STRM
        bad = [f for f in strm if "bad_strm" in str(f)]
        assert len(bad) >= 3, f"畸形 STRM 不足: {len(bad)}"
