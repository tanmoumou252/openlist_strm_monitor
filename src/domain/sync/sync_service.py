"""Sync Service - handles A->B synchronization logic."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service import AppService
    from database import Database
    from config import AppConfig

from utils import read_strm_webdav_path, safe_remove_file, webdav_parent, make_strm_fingerprint
from utils.strm_utils import STRM_PARSE_VERSION
from utils.file_utils import chunk_list


@dataclass
class _SyncPrep:
    """_sync_one_record 步骤 1-6 的预处理结果（T3 拆分）。

    needs_copy=True：B 目标不存在，需 A→B 拷贝（现行步骤 7）；
    needs_copy=False：B 目标已存在且 WebDAV 相同，直入 upsert（现行步骤 6）。
    """
    local_path: str      # A 源本地路径
    b_local: Path        # B 目标路径
    webdav_path: str
    parent: str
    fingerprint: str
    mapping_id: str
    needs_copy: bool


class SyncService:
    """A->B 同步服务"""

    def __init__(self, app: AppService) -> None:
        self.app = app
        self.config: AppConfig = app.config
        self.db: Database = app.db
        # 启动同步缓存（sync 期间有效，finally 清除）
        self._cache_ghost: set[str] | None = None
        self._cache_b_fp: set[tuple[str, str]] | None = None  # set of (mapping_id, fingerprint)

    def initial_scan_a(
            self, use_bulk: bool = False,
            a_roots: list[Path] | None = None,
            use_snapshot: bool = True) -> None:
        """启动时或刷新时批量索引指定 A 区 STRM 文件到数据库。

        性能优化：
        - 启动时使用 bulk_connection 长连接模式（核心优化：消除反复获取 rw_lock + 打开连接的开销）
        - 使用多线程并发读取 .strm 文件（辅助优化：利用多核 CPU 并发 I/O）
        - 每 100 条或每 2 秒输出一次日志 + records/s 性能基准
        - use_bulk=True 时使用 bulk_connection 长连接模式（仅启动时）
        - use_bulk=False 时使用 upsert_a_batch（定期刷新时）
        - 延迟 FTS 重建（仅 use_bulk=True 时，扫描完成后一次性重建）

        设计决策：为什么不用 OpenList API 扫描 A 区？
        - OpenList API /api/fs/list 单页最多返回 100 个文件（maximum: 100）
        - API 返回目录下所有文件类型（.strm、.nfo、.jpg、.srt 等），无法过滤
        - 因此，使用本地文件系统遍历 + 多线程并发读取是更好的选择

        Args:
            use_bulk: True 用 bulk_connection（启动时，单线程安全）。
                      False 用 upsert_a_batch（刷新时，多线程安全）。
            a_roots: 显式限制扫描的 A 根；None 表示扫描全部配置根，空列表表示不扫描。
            use_snapshot: True（默认）启用 a_strm_snapshot 的 size+mtime+parse_version
                      内容读跳检；False（全量审计）强制逐文件重读正文并重建快照
                      （权威自愈触发源）。快照整体读失败时 fail-open 退化为全读。
                      约束：False 仅允许在 a_roots=None（全量扫描）场景调用——
                      prune 的 keep 集来自本轮实际扫描集合，与局部 a_roots 同用
                      会误剪范围外快照行（触发 WARNING 日志，行为不变）。
        """
        if not use_snapshot and a_roots is not None:
            logging.warning(
                "[初始化] use_snapshot=False 与局部 a_roots 同用：prune keep 集"
                "不含扫描范围外路径，将误剪范围外快照行"
                "（全量审计应使用 a_roots=None）")
        logging.info("[初始化] 扫描 A 区 STRM 文件（%s）...",
                     "bulk模式" if use_bulk else "标准模式")
        if a_roots == []:
            logging.info("[初始化] 未命中主动刷新路径，跳过 A 区扫描")
            return
        t0 = time.time()
        BATCH_SIZE = 1000
        LOG_INTERVAL = 100
        total_strm = 0
        discovered_count = 0
        indexed_count = 0
        batch: list[tuple[str, str, str]] = []
        snap_batch: list[tuple[str, int, int, str, str, int, float]] = []
        audit_paths: list[str] = []  # 仅 use_snapshot=False 时收集，供 prune
        parent_set: set[str] = set()
        last_log_time = time.time()
        # pool 前一次性载入快照（fail-open：读异常返回空 map → 全量重读）。
        # use_snapshot=False（审计）也载入：仅用于跳过"重读结果与既有快照
        # 五字段全等"的恒等行重写（读仍强制全读；写语义等价，省去约 3% 审计
        # 墙钟的恒等 upsert，满足 P-1 audit 无回退闸）。
        snap_map: dict[str, tuple[int, int, str, str, int]] = self.db.load_a_snapshot_map()

        def process_strm_file(file_path: Path) -> tuple[str, str, str, tuple | None] | None:
            """处理单个 .strm：命中 size+mtime+parse_version 未变的快照则跳过正文读。
            返回 (local_path, webdav_path, parent, snapshot_row_or_None) 或 None。"""
            lp = str(file_path)
            try:
                st = os.stat(file_path)
            except OSError:
                # 与 read_strm_webdav_path 对 FileNotFoundError 返回 None 同构
                return None
            snap = snap_map.get(lp) if use_snapshot else None
            # snap = (file_size, mtime_ns, webdav_path, parent, parse_version)
            # 设计决策: 命中采信 size+mtime_ns 双等（不校验内容哈希）——
            # "同 size 同 mtime 恢复"理论上可骗过跳检；权威自愈由
            # refresh_service 周期/手动全量审计（use_snapshot=False）兜底，
            # 误采信窗口止于下一次全量审计，换取审计墙钟 ≤3% 的读跳检收益。
            if (snap is not None and snap[0] == st.st_size and snap[1] == st.st_mtime_ns
                    and snap[2] and st.st_mtime_ns > 0 and snap[4] == STRM_PARSE_VERSION):
                # 复用缓存权威链接，不 open 正文
                return (lp, snap[2], snap[3], None)
            webdav_path = read_strm_webdav_path(file_path)
            if not webdav_path:
                logging.debug("[初始化] 无法解析 STRM: %s", file_path)
                return None
            parent = webdav_parent(webdav_path)
            snap_row = (lp, st.st_size, st.st_mtime_ns, webdav_path, parent,
                        STRM_PARSE_VERSION, time.time())
            return (lp, webdav_path, parent, snap_row)

        def flush_batch():
            """批量写入数据库。闭包捕获 conn 和 batch。"""
            nonlocal indexed_count
            if not batch:
                return
            if use_bulk:
                self._upsert_a_batch_bulk(conn, batch)
                indexed_count += len(batch)
            else:
                written = self.db.upsert_a_batch(batch)
                indexed_count += written if isinstance(written, int) else len(batch)
                self.app.update_progress(a_indexed=indexed_count)
            batch.clear()

        # 根据模式选择连接：bulk_connection 绕过 rw_lock，仅启动时单线程安全
        conn = None
        bulk_ctx = None
        _exc_info = (None, None, None)  # 追踪异常信息
        # __enter__ 嵌套在独立 try/finally 内，确保 raises 时 __exit__ 仍被调用，
        # 避免 sqlite3.connect 失败等场景导致连接泄漏。
        if use_bulk:
            bulk_ctx = self.db.bulk_connection()
            try:
                conn = bulk_ctx.__enter__()
            except BaseException:
                try:
                    bulk_ctx.__exit__(*sys.exc_info())
                except BaseException:
                    pass
                raise

        try:
            roots = self.app.a_roots if a_roots is None else a_roots
            for a_root in roots:
                if not a_root.exists():
                    logging.warning("[初始化] A 区根目录不存在: %s", a_root)
                    continue

                # 收集所有 .strm 文件路径
                strm_files: list[Path] = []
                for root, _dirs, files in os.walk(a_root):
                    for name in files:
                        if name.lower().endswith(".strm"):
                            strm_files.append(Path(root) / name)

                logging.info("[初始化] 发现 %d 个 .strm 文件，开始多线程处理...", len(strm_files))
                discovered_count += len(strm_files)
                self.app.update_progress(a_discovered=discovered_count)

                # 使用多线程并发处理（按 CHUNK_SIZE=2000 有界分批提交，避免数万 Future 占用过多内存）
                CHUNK_SIZE = 2000
                with ThreadPoolExecutor(max_workers=4) as executor:
                    for chunk_start in range(0, len(strm_files), CHUNK_SIZE):
                        chunk_files = strm_files[chunk_start:chunk_start + CHUNK_SIZE]
                        futures = {executor.submit(process_strm_file, fp): fp for fp in chunk_files}

                        for future in as_completed(futures):
                            result = future.result()
                            if result:
                                local_path, webdav_path, parent, snap_row = result
                                batch.append((local_path, webdav_path, parent))
                                parent_set.add(parent)
                                total_strm += 1
                                if snap_row is not None:
                                    if use_snapshot:
                                        snap_batch.append(snap_row)
                                    else:
                                        # 审计模式：恒等行（与既有快照五字段全等）
                                        # 跳过重写，非恒等/新行照常重建
                                        prev = snap_map.get(local_path)
                                        if (prev is None or prev[0] != snap_row[1]
                                                or prev[1] != snap_row[2]
                                                or prev[2] != snap_row[3]
                                                or prev[3] != snap_row[4]
                                                or prev[4] != snap_row[5]):
                                            snap_batch.append(snap_row)
                                    if not use_snapshot:
                                        audit_paths.append(local_path)

                                # 日志输出（每 100 条或每 2 秒）+ 性能基准
                                current_time = time.time()
                                if total_strm % LOG_INTERVAL == 0 or (current_time - last_log_time) >= 2.0:
                                    elapsed = current_time - t0
                                    rate = total_strm / elapsed if elapsed > 0 else 0
                                    logging.info(
                                        "[初始化] A 区扫描进度: %d 条已索引 (%.1fs, %.0f 条/秒)...",
                                        total_strm, elapsed, rate)
                                    last_log_time = current_time

                                # 批量写入
                                if len(batch) >= BATCH_SIZE:
                                    flush_batch()

                # 刷新当前 a_root 的剩余记录
                flush_batch()
        except BaseException:
            # 捕获 BaseException（含 KeyboardInterrupt/SystemExit），
            # 确保 __exit__ 收到正确的异常信息并 rollback bulk_connection，
            # 与 __enter__ 的 except BaseException 语义一致。
            _exc_info = sys.exc_info()
            raise
        finally:
            # bulk_connection 在 __exit__ 时自动 commit（正常退出）或 rollback（异常）
            if bulk_ctx is not None:
                bulk_ctx.__exit__(*_exc_info)

        # 以下操作使用 self.connection()（独立连接），必须在 bulk_connection 提交后执行
        # （R-7 锁边界：bulk_ctx.__exit__ 之前绝不再取 rw_lock.write_locked，否则自死锁）
        if snap_batch:
            self.db.upsert_a_snapshot_bulk(snap_batch)
        if not use_snapshot:
            self.db.prune_a_snapshot_not_in(audit_paths)
        if use_bulk:
            self.app.update_progress(a_indexed=indexed_count)
        if parent_set:
            self.db.save_known_folders_batch(list(parent_set), source="a")

        # 仅 bulk 模式需要重建 FTS（upsert_a_batch 已逐批维护 FTS）
        if use_bulk:
            logging.info("[初始化] 重建 FTS 索引...")
            self.db.rebuild_fts_table("a_strm_files", "a_strm_files_fts")

        elapsed = time.time() - t0
        rate = total_strm / elapsed if elapsed > 0 else 0
        logging.info(
            "[初始化] A 区扫描完成，共索引 %d 个 STRM 文件 (%.1fs, %.0f 条/秒)",
            total_strm, elapsed, rate)

    def _upsert_a_batch_bulk(self, conn, records: list[tuple[str, str, str]]) -> int:
        """批量插入 A 区记录（使用 bulk_connection，跳过 FTS 同步）。

        性能优化：
        - 使用单个连接（不获取 rw_lock）
        - 跳过 FTS 同步（延迟到扫描完成后由 rebuild_fts_table 一次性重建）
        - 变化检测：只在业务字段变化时更新 updated_at
        """
        if not records:
            return 0
        now = time.time()

        # 预读现有记录（分片处理避免 SQL 变量超限）
        local_paths = [r[0] for r in records]
        existing_map = {}
        for chunk in chunk_list(local_paths, 900):
            placeholders = ','.join('?' * len(chunk))
            existing_rows = conn.execute(
                f"SELECT local_path, webdav_path, parent_webdav_path "
                f"FROM a_strm_files WHERE local_path IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in existing_rows:
                existing_map[row[0]] = (row[1], row[2])

        # 分类：新增 vs 更新
        to_insert = []
        to_update = []
        for local_path, webdav_path, parent_webdav_path in records:
            if local_path not in existing_map:
                # 新增
                to_insert.append((local_path, webdav_path, parent_webdav_path, now))
            else:
                # 现有记录：比较业务字段
                old_webdav, old_parent = existing_map[local_path]
                if old_webdav != webdav_path or old_parent != parent_webdav_path:
                    # 字段变化
                    to_update.append((webdav_path, parent_webdav_path, now, local_path))

        # 执行 INSERT
        # bulk 批量新增分支同时写 last_verified_at=now，
        # 与单条 upsert 路径一致，避免启动全量同步新增记录 last_verified_at 恒为 0。
        if to_insert:
            conn.executemany(
                """
                INSERT INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at, last_verified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(lp, wp, pp, now, now) for lp, wp, pp, _ in to_insert],
            )

        # 执行 UPDATE
        if to_update:
            conn.executemany(
                """
                UPDATE a_strm_files
                SET webdav_path = ?, parent_webdav_path = ?, updated_at = ?
                WHERE local_path = ?
                """,
                to_update,
            )

        return len(records)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None,
            use_bulk: bool = False) -> None:
        """A -> B 全量同步，两遍结构（索引 + 执行）。

        第一遍（索引阶段）：遍历所有 A 记录，计算目标路径，构建
        target_path -> [source_info, ...] 的索引并检测目标路径冲突。
        只读操作（ghost/fp 预检），不触发任何文件复制或 DB 写入。

        冲突解决：第一遍完成后，凡存在多个不同 WebDAV 身份指向同一
        目标路径的目标视为冲突，全部安全跳过。

        第二遍（执行阶段）：遍历原始记录，对非冲突目标路径调用
        _sync_one_record 执行同步逻辑；对冲突目标路径跳过。

        Args:
            valid_engine_paths: 限定同步范围。
            use_bulk: True 用 bulk_connection 批量提交（首次启动，无并发）。
                      False 用分批提交（刷新/审计），有并发，每 1000 条提交一次。

        并发安全说明
        -----------
        批量同步使用 bulk_connection 绕过 rw_lock，写盘未提交前其它连接不可见，
        _sync_one_record 不依赖指纹锁，靠三层防御：
        - 内存缓存 _cache_b_fp，拦截同指纹重复。
        - 文件系统检查 b_local.exists()，磁盘文件对多线程可见。
        - ensure_single_visible_instance（兜底去重，通过 dedup_queue 延迟到提交后执行）。
        """
        BATCH_COMMIT_SIZE = 1000  # 分批提交大小
        MAX_CONFLICT_EXAMPLES = 5  # 冲突示例显示上限
        SLOW_OP_THRESHOLD = 3.0  # 慢操作告警阈值（秒）

        logging.info("[初始化] A -> B 全量同步开始 (%s)",
                     "批量模式" if use_bulk else "分批提交模式")
        if valid_engine_paths is not None:
            logging.info("[初始化] 限定同步范围: %s", valid_engine_paths)

        t0 = time.time()
        all_a_records = self.db.get_all_a_records()
        total_count = len(all_a_records)
        logging.info("[初始化] A -> B 同步: 共 %d 条待处理", total_count)

        # use_bulk=True（首次启动，无并发）直接执行；use_bulk=False 整体包在
        # rw_lock.write_locked() 内串行化（预加载只读 DB，锁内开销可控）。
        #
        # 说明：去重 flush（ensure_single_visible_instance → read_locked）必须在
        # rw_lock 写锁释放后执行，因此 _flush_dedup_queue 在锁外统一调用。
        #
        # 说明：扫描阶段预计算 mapping 根，fast mapping 消除 pass1/pass2 两遍
        # resolve 风暴（R8）；本函数顶层 finally 清理。refresh_service 以
        # use_bulk=False 调用时 watcher 活跃——预计算根为 config 派生入口快照，
        # 与现行逐条读语义等价（配置热更新窗口差异随 docs/否决方案.md 登记）。
        self.app.ensure_scan_mapping_roots()

        def _flush_dedup_queue(dedup_queue: list[tuple[str, str, str]]) -> None:
            """提交后执行延迟的去重操作（此时数据已可见，且位于写锁外）。

            Task 5：预筛选仅对真实存在重复的 (mapping_id, fingerprint) 组执行，
            消除零重复时的逐条 ensure 建连开销；预筛查询失败回退全量去重。
            设计决策: use_bulk=True 且 watchers 未启动（启动模式，双条件门控，
            防御未来 use_bulk=True 热调用）时经 quarantine_session 会话批量
            路径等价执行（T1）；预筛查询失败回退现行逐条路径（C13-4）。
            """
            if not dedup_queue:
                return
            dup_groups = None
            try:
                dup_groups = self.app.db.get_duplicate_fingerprint_groups()
                dup_set = {(mid, fp) for mid, fp, _ in dup_groups}
            except Exception as e:
                logging.warning("[A->B] 去重预筛选查询失败，回退全量去重: %s", e)
                dup_set = None
                dup_groups = None  # C13-4：查询/迭代失败（含 Mock 不可迭代）不得进批量入口

            if dup_set is not None:
                filtered_queue = [
                    (mid, fp, b_path) for mid, fp, b_path in dedup_queue
                    if (mid, fp) in dup_set
                ]
            else:
                filtered_queue = dedup_queue

            if not filtered_queue:
                logging.debug(
                    "[A->B] 去重预筛选：全无重复，跳过全部 %d 项 ensure 调用",
                    len(dedup_queue))
                return
            if (dup_groups is not None and use_bulk
                    and not self.app._watchers_live()):
                # C12-2: 复用上方既有 GROUP BY 结果分组（prefer = 组内首个
                # 队列项路径），零新增重复 SELECT
                first_path: dict[tuple[str, str], str] = {}
                for mid, fp, b_path in filtered_queue:
                    first_path.setdefault((mid, fp), b_path)
                batch_groups = [
                    (mid, fp, path) for (mid, fp), path in first_path.items()]
                if batch_groups:
                    self.app._quarantine_duplicate_groups_batch(batch_groups)
                return
            # 已知取舍: 单条去重失败记 WARNING 后继续处理队列项，不阻断主同步。
            for mid, fp, b_path in filtered_queue:
                try:
                    self.app.ensure_single_visible_instance(fp, b_path, mapping_id=mid)
                except Exception as e:
                    logging.warning("[A->B] 去重失败 %s: %s", b_path, e)

        def _run_index_and_execute() -> list[tuple[str, str, str]]:
            """预加载 + 第一遍索引 + 第二遍执行 + 清空缓存，返回延迟去重队列。

            该函数在 use_bulk=False 时被外层 rw_lock.write_locked() 包裹，
            保证对实例级缓存 _cache_ghost/_cache_b_fp 的预加载与清空不会与
            其它并发全量同步交错。
            """
            # 预加载读缓存。注意：此处位于 rw_lock.write_locked() 内（use_bulk=False），
            # 必须用 skip_read_lock=True 跳过 read_locked()，否则因 _writers_active>0
            # 永久等待自死锁；此时持有写锁，读取是安全的。
            self._cache_ghost = self.db.get_all_ghost_protected_paths(skip_read_lock=True)
            self._cache_b_fp = set()
            for m in self.app.a_b_mappings:
                fps = self.db.get_all_b_fingerprints(m.mapping_id, skip_read_lock=True)
                for fp in fps:
                    self._cache_b_fp.add((m.mapping_id, fp))
            logging.info("[初始化] 预加载: ghost=%d, B指纹=%d (%.1fs)",
                         len(self._cache_ghost), len(self._cache_b_fp),
                         time.time() - t0)

            # ===================================================================
            # 第一遍：索引阶段 —— 计算目标路径，检测冲突
            # ===================================================================
            t_pass1 = time.time()
            # target_path -> [(source_a_path, webdav_path, fingerprint, original_index), ...]
            target_index: dict[str, list[tuple[str, str, str, int]]] = {}
            target_conflicts: set[str] = set()  # 存在多个不同 WebDAV 的目标
            # 记录每条 A 记录对应的 target_path 和 mapping_id（第二遍复用）
            rec_target_map: list[str | None] = [None] * total_count
            rec_mapping_map: list[str | None] = [None] * total_count
            pass1_skipped = {"skip_ghost": 0, "skip_fp": 0, "skip_missing": 0,
                             "skip_filtered": 0, "skip_unmapped": 0,
                             "skip_invalid_path": 0}

            for idx, rec in enumerate(all_a_records):
                local_path = rec.local_path
                webdav_path = rec.webdav_path

                # 与 _sync_one_record 一致的预检
                if not Path(local_path).exists():
                    pass1_skipped["skip_missing"] += 1
                    continue

                if valid_engine_paths is not None:
                    if not any(webdav_path == p or webdav_path.startswith(p + "/")
                               for p in valid_engine_paths):
                        pass1_skipped["skip_filtered"] += 1
                        continue

                if webdav_path in self._cache_ghost:
                    pass1_skipped["skip_ghost"] += 1
                    continue

                # 解析 mapping 上下文（fast 版：扫描阶段预计算根）。
                # N3（c7 §5.4）：每条 local_path 恰 resolve 一次（T5），以
                # _resolved_target 透传 fast mapping；非 skip 行复用同一
                # resolved target 调 build_b_path_from_a（resolve 幂等，仅
                # 消除重复 resolve 开销；skip_missing 前置保持）。
                resolved_target = Path(local_path).resolve()
                mapping = self.app._get_mapping_for_a_fast(
                    local_path, _resolved_target=resolved_target)
                if mapping is None:
                    # R3 计数旁挂：静默丢弃必须可见（无 fail-safe 权限）
                    pass1_skipped["skip_unmapped"] += 1
                    logging.debug("[A->B] 无法解析 A 路径的映射上下文, 跳过: %s", local_path)
                    continue
                mapping_id = mapping[0]
                rec_mapping_map[idx] = mapping_id

                fingerprint = make_strm_fingerprint(webdav_path)
                if (mapping_id, fingerprint) in self._cache_b_fp:
                    pass1_skipped["skip_fp"] += 1
                    continue

                # 计算目标路径（只读，无副作用）；仅注入扫描阶段预计算根，
                # 消除内部 get_mapping_for_a 风暴（R8）；根未预载时回退原路径解析。
                # N3 兑现（c.9.2 Task 6）：_a_local_resolved 透传 resolved_target，
                # 消除 build_b_path_from_a 内第二段 resolve（仅 pass1 两处传；
                # 其余 6 处调用方入参未 resolve，保持默认走方法内解析）。
                try:
                    scan_a_root, scan_b_root = self.app._get_scan_roots_for_mapping(
                        mapping_id)
                    if scan_a_root is not None and scan_b_root is not None:
                        b_local = self.app.build_b_path_from_a(
                            resolved_target, webdav_path,
                            a_root=scan_a_root, b_root=scan_b_root,
                            _a_local_resolved=resolved_target)
                    else:
                        b_local = self.app.build_b_path_from_a(
                            resolved_target, webdav_path,
                            _a_local_resolved=resolved_target)
                except ValueError:
                    # R3 计数旁挂：静默丢弃必须可见（无 fail-safe 权限）
                    pass1_skipped["skip_invalid_path"] += 1
                    continue

                target_path = str(b_local)
                rec_target_map[idx] = target_path

                if target_path not in target_index:
                    target_index[target_path] = []
                target_index[target_path].append(
                    (local_path, webdav_path, fingerprint, idx))

                # 冲突检测：同目标 + 不同 WebDAV 身份
                existing_webdavs = {info[1] for info in target_index[target_path]}
                if len(existing_webdavs) > 1:
                    target_conflicts.add(target_path)

            t_pass1_elapsed = time.time() - t_pass1
            logging.info(
                "[初始化] A -> B 索引阶段完成: %d 条索引, %d 个唯一目标, "
                "%d 个冲突目标, 预跳过=%d (%.1fs)",
                total_count - sum(pass1_skipped.values()),
                len(target_index), len(target_conflicts),
                sum(pass1_skipped.values()), t_pass1_elapsed)

            # 输出冲突示例（限量，避免日志洪水）
            # 不可逆边界说明：如果 OpenList 上游已将同名不同扩展名（如 .mkv/.mp4）
            # 覆盖成同一个 .strm，桥接程序只能观察到当前剩余的单个 .strm，无法证明
            # 第二个源曾存在，也不能从云端或 B 区猜测恢复。此处只处理桥接仍能观察到
            # 的冲突（同批次多条 A 记录计算出相同 target_path 但 WebDAV 不同），全部
            # 安全跳过，不输出猜测性"已检测上游覆盖"警告。
            if target_conflicts:
                for i, ct in enumerate(sorted(target_conflicts)[:MAX_CONFLICT_EXAMPLES]):
                    sources = target_index[ct]
                    webdavs = sorted(set(info[1] for info in sources))
                    logging.warning(
                        "[初始化] 目标路径冲突 (%d 个不同 WebDAV): %s | "
                        "WebDAV: %s | 来源数: %d",
                        len(webdavs), ct, webdavs, len(sources))
                if len(target_conflicts) > MAX_CONFLICT_EXAMPLES:
                    logging.warning(
                        "[初始化] ... 还有 %d 个冲突目标未显示",
                        len(target_conflicts) - MAX_CONFLICT_EXAMPLES)

            # ===================================================================
            # 第二遍：执行阶段 —— 对非冲突目标执行同步
            # ===================================================================
            counters = {
                "success": 0,
                "skip_ghost": pass1_skipped["skip_ghost"],
                "skip_fp": pass1_skipped["skip_fp"],
                "skip_missing": pass1_skipped["skip_missing"],
                "skip_filtered": pass1_skipped["skip_filtered"],
                "skip_unmapped": pass1_skipped["skip_unmapped"],
                "skip_invalid_path": pass1_skipped["skip_invalid_path"],
                "skip_exists_diff": 0,
                "skip_target_conflict": 0,
                "fail": 0,
            }
            log_interval = max(100, total_count // 100)
            batch_count = 0
            dedup_queue: list[tuple[str, str, str]] = []  # (mapping_id, fingerprint, b_path)

            t_pass2 = time.time()

            # M1 插桩（常驻观测，零行为变化）：A→B 分段计时累加器。
            # use_bulk=True 分段 = _prepare_sync_one 串行段 / copy 并行段 /
            # _commit_sync_one 串行 SQL 段；use_bulk=False 逐条
            # _sync_one_record 内部含 prepare+copy+commit，记为单一串行段。
            # 两种形态共用同一累加器与同一条输出行。
            seg = {"prepare": 0.0, "copy": 0.0, "commit": 0.0, "pass2_serial": 0.0}

            def _run_pass2(conn):
                """Pass 2 执行阶段（提取为函数以便 use_bulk 分支复用）。

                T3 分流（C5）：use_bulk=True（启动模式）走分块并行拷贝——主线程
                逐条 _prepare_sync_one 收集 Prep，4 线程对需拷贝项执行
                mkdir+copyfile（目标互异由 pass1 冲突检测保证），主线程按原序
                _commit_sync_one（DB upsert/SAVEPOINT 主线程 bulk 连接串行）；
                use_bulk=False（refresh 模式）保持现行逐条交错循环不变
                （BATCH_COMMIT_SIZE 提交逻辑保留）。
                """
                # batch_count 在外层作用域定义，此处需 nonlocal 才能修改
                nonlocal batch_count

                def _progress_log(idx: int) -> None:
                    c = counters
                    total_skip_2 = (
                        c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                        + c["skip_filtered"] + c["skip_unmapped"]
                        + c["skip_invalid_path"] + c["skip_exists_diff"]
                        + c["skip_target_conflict"])
                    logging.info(
                        "[初始化] A -> B 进度: %d/%d (%.0f%%) %.1fs | "
                        "成功=%d 跳过=%d 冲突=%d 失败=%d",
                        idx, total_count, idx / total_count * 100,
                        time.time() - t0, c["success"],
                        total_skip_2, c["skip_target_conflict"], c["fail"])

                if use_bulk:
                    # 设计决策: 启动模式分块并行拷贝（T3，仅 use_bulk=True 启动
                    # 上下文）：copyfile 失败保持现行"不清理半拷贝"语义（C4）；
                    # unlink 回滚仅限主线程 _commit_sync_one 的 DB 失败（尽力回滚）；
                    # 计数器/进度日志/_cache_b_fp 维护语义不变（主线程）。
                    COPY_CHUNK = 64
                    # 设计决策: 拷贝线程 8（T5，原 4）——mkdir+copyfile 为真 I/O
                    # 并行（释放 GIL），按 T0/T4 数据择优
                    with ThreadPoolExecutor(max_workers=8) as copy_executor:
                        for chunk_start in range(0, total_count, COPY_CHUNK):
                            chunk = list(enumerate(
                                all_a_records[chunk_start:chunk_start + COPY_CHUNK],
                                chunk_start + 1))
                            preps: list[tuple[int, _SyncPrep]] = []
                            for idx, rec in chunk:
                                target_path = rec_target_map[idx - 1]
                                if target_path is None:
                                    continue
                                if target_path in target_conflicts:
                                    counters["skip_target_conflict"] += 1
                                    batch_count += 1
                                    continue
                                _t_seg = time.perf_counter()
                                result = self._prepare_sync_one(
                                    rec, valid_engine_paths,
                                    mapping_id=rec_mapping_map[idx - 1])
                                seg["prepare"] += time.perf_counter() - _t_seg
                                if isinstance(result, str):
                                    counters[result] = counters.get(result, 0) + 1
                                    batch_count += 1
                                    if idx % log_interval == 0:
                                        _progress_log(idx)
                                    continue
                                preps.append((idx, result))

                            # 并行拷贝需拷贝项（worker 仅 mkdir+copyfile，纯 I/O）
                            copy_jobs = [(i, p) for i, p in preps if p.needs_copy]

                            def _copy_one(job: tuple[int, _SyncPrep]) -> bool:
                                _i, p = job
                                try:
                                    p.b_local.parent.mkdir(parents=True, exist_ok=True)
                                    shutil.copyfile(p.local_path, p.b_local)
                                    return True
                                except Exception as e:
                                    logging.warning(
                                        "[A->B] 拷贝失败 %s: %s", p.local_path, e)
                                    return False

                            copied: dict[int, bool] = {}
                            _t_seg = time.perf_counter()
                            if copy_jobs:
                                for (job_idx, _p), ok in zip(
                                        copy_jobs,
                                        copy_executor.map(_copy_one, copy_jobs)):
                                    copied[job_idx] = ok
                            seg["copy"] += time.perf_counter() - _t_seg

                            # 主线程按原序提交（成功拷贝项与无需拷贝项）
                            _t_seg = time.perf_counter()
                            for idx, p in preps:
                                if p.needs_copy and not copied.get(idx, False):
                                    counters["fail"] = counters.get("fail", 0) + 1
                                    batch_count += 1
                                    if idx % log_interval == 0:
                                        _progress_log(idx)
                                    continue
                                status = self._commit_sync_one(
                                    conn, p, dedup_queue)
                                counters[status] = counters.get(status, 0) + 1
                                batch_count += 1
                                if idx % log_interval == 0:
                                    _progress_log(idx)
                            seg["commit"] += time.perf_counter() - _t_seg
                    # 提交剩余批次（bulk 模式下 bulk_connection 统一 commit，
                    # 此处保持现行尾部提交语义）
                    if batch_count > 0:
                        conn.commit()
                    return

                for idx, rec in enumerate(all_a_records, 1):
                    target_path = rec_target_map[idx - 1]

                    # Pass 1 中被跳过的记录（ghost/fp/missing/filtered/mapping）target_path 为 None
                    if target_path is None:
                        continue

                    if target_path in target_conflicts:
                        counters["skip_target_conflict"] += 1
                        continue

                    _t_seg = time.perf_counter()
                    result = self._sync_one_record(rec, valid_engine_paths, conn,
                                                   dedup_queue,
                                                   mapping_id=rec_mapping_map[idx - 1])
                    seg["pass2_serial"] += time.perf_counter() - _t_seg
                    counters[result] = counters.get(result, 0) + 1
                    batch_count += 1

                    # 分批提交模式：每 1000 条提交一次
                    if not use_bulk and batch_count >= BATCH_COMMIT_SIZE:
                        conn.commit()
                        self.app.update_progress(
                            synced_records=counters.get("success", 0))
                        # 移除此处的 _flush_dedup_queue() 调用。
                        # 非 bulk 模式下此处位于 rw_lock.write_locked() 内，flush 会调
                        # ensure_single_visible_instance → read_locked，因 _writers_active>0
                        # 永久 wait → 全进程死锁。flush 延迟到锁外统一执行。
                        batch_count = 0
                        logging.debug("[初始化] 分批提交: 已处理 %d 条", idx)

                    if idx % log_interval == 0:
                        _progress_log(idx)

                # 提交剩余批次
                if batch_count > 0:
                    conn.commit()
                    if not use_bulk:
                        self.app.update_progress(
                            synced_records=counters.get("success", 0))
                # 从锁内移除 _flush_dedup_queue()。flush 在下方
                # try/finally 之后统一调用（锁外），避免 rw_lock 写锁内调
                # read_locked 造成自死锁。

            try:
                if use_bulk:
                    # use_bulk=True（启动时，无并发）：bulk_connection 绕过 rw_lock
                    with self.db.bulk_connection() as conn:
                        _run_pass2(conn)
                    self.app.update_progress(
                        synced_records=counters.get("success", 0))
                else:
                    # use_bulk=False（刷新/审计时）：外层已持有 rw_lock.write_locked()，
                    # 此处用标准连接即可（不再重复获取写锁，避免非可重入锁死锁）。
                    with self.db.connection() as conn:
                        _run_pass2(conn)
            finally:
                # 无论事务成功或失败，finally 均清空缓存。
                # 若 bulk/分批 commit 回滚，DB 已恢复但缓存若不清理会残留本次新增的
                # 指纹，导致后续合法记录被 skip_fp 跳过（A→B 复制遗漏）。
                # 清空后下次调用重新预加载，缓存不会残留跨调用。
                self._cache_ghost = None
                self._cache_b_fp = None

            t_pass2_elapsed = time.time() - t_pass2
            c = counters
            total_skip = (c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                          + c["skip_filtered"] + c["skip_unmapped"]
                          + c["skip_invalid_path"] + c["skip_exists_diff"]
                          + c["skip_target_conflict"])
            # R3 运行时守恒 tripwire：计数旁挂不得具备中断启动的能力——
            # 不等仅记 WARNING（不抛异常、不 fail-safe），守恒正确性由测试与
            # 真机日志断言保证。
            if c["success"] + total_skip + c["fail"] != total_count:
                logging.warning(
                    "[初始化] A -> B 计数守恒破坏: 成功=%d 跳过=%d 失败=%d"
                    " != 总待处理=%d",
                    c["success"], total_skip, c["fail"], total_count)
            logging.info(
                "[初始化] A -> B 全量同步完成 (%.1fs) | "
                "成功=%d 跳过=%d(ghost=%d fp=%d 不存在=%d 过滤=%d "
                "未映射=%d 路径无效=%d 路径不同=%d 目标冲突=%d) 失败=%d",
                time.time() - t0, c["success"], total_skip,
                c["skip_ghost"], c["skip_fp"], c["skip_missing"],
                c["skip_filtered"], c["skip_unmapped"], c["skip_invalid_path"],
                c["skip_exists_diff"], c["skip_target_conflict"], c["fail"])
            # M1 插桩：分段计时输出（bulk 形态 prepare/copy/commit 有效，
            # 非 bulk 形态 pass2 串行段有效；未走段为 0）
            logging.info(
                "[初始化] A -> B 分段计时(M1): prepare=%.2fs copy=%.2fs "
                "commit=%.2fs pass2串行=%.2fs (use_bulk=%s)",
                seg["prepare"], seg["copy"], seg["commit"],
                seg["pass2_serial"], use_bulk)

            # 生成人工处理清单（冲突目标）
            if target_conflicts:
                self._write_manual_review_list(target_index, target_conflicts)

            return dedup_queue

        try:
            if use_bulk:
                dedup_queue = _run_index_and_execute()
            else:
                with self.db.rw_lock.write_locked():
                    dedup_queue = _run_index_and_execute()

            # 去重 flush 在写锁外统一执行。此时 rw_lock 写锁已释放，
            # ensure_single_visible_instance 的 read_locked 可正常获取；flush 直查 DB
            # 不依赖缓存，安全。保留 use_bulk 语义不变。
            _flush_dedup_queue(dedup_queue)
        finally:
            self.app.clear_scan_mapping_roots()

    def _write_manual_review_list(self, target_index: dict, target_conflicts: set) -> None:
        """将冲突跳过的 A 源清单写入 B 区根目录的清单文件。

        格式：Markdown 表格，含 A 源路径、WebDAV 路径、目标路径、原因。
        文件名：`_MANUAL_REVIEW_YYYYMMDD_HHMMSS.md`
        """
        from pathlib import Path, PosixPath, WindowsPath
        # 使用第一个冲突目标路径对应的映射上下文获取 B 根
        first_target = next(iter(target_conflicts)) if target_conflicts else None
        if first_target:
            mapping = self.app.get_mapping_for_b(first_target)
            if mapping is None:
                logging.warning("[手动复查] 无法解析目标路径的映射")
                return  # Fail-closed: skip generating manual review list
            _, b_root, _ = mapping
        else:
            # No conflict targets - no need to generate list
            return
        # 在测试场景中 b_root 可能是 Mock 对象，不生成清单
        if not isinstance(b_root, (Path, PosixPath, WindowsPath)):
            return

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        list_path = b_root / f"_MANUAL_REVIEW_{ts}.md"

        lines = [
            "# 人工处理清单",
            "",
            f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            "以下 A 区文件因目标路径冲突被跳过，需人工确认命名后手动复制到 B 区。",
            "",
            "| A 区路径 | WebDAV 路径 | 目标路径 | 原因 |",
            "|----------|-------------|----------|------|",
        ]

        for target_path in sorted(target_conflicts):
            sources = target_index[target_path]
            for local_path, webdav_path, fingerprint, idx in sources:
                lines.append(f"| `{local_path}` | `{webdav_path}` | `{target_path}` | 目标路径冲突 |")

        try:
            list_path.write_text("\n".join(lines), encoding="utf-8")
            logging.info("[初始化] 人工处理清单已生成: %s (%d 个冲突目标)",
                         list_path, len(target_conflicts))
        except Exception as e:
            logging.warning("[初始化] 生成人工处理清单失败: %s", e)

    def _prepare_sync_one(self, rec, valid_engine_paths,
                          mapping_id: str | None = None):
        """现行步骤 1-6（T3 拆分）：exists/engine/ghost/fp/build_b_path/exists+内容比较。

        返回状态字符串（skip_missing/skip_filtered/skip_ghost/skip_fp/fail/
        skip_exists_diff）或 _SyncPrep（含 needs_copy 标记）。纯读无副作用，
        供 use_bulk=True 分块并行拷贝路径逐条调用。
        """
        local_path = rec.local_path
        webdav_path = rec.webdav_path
        parent = rec.parent_webdav_path

        # 1. 检查本地文件是否存在
        if not Path(local_path).exists():
            return "skip_missing"

        # 2. 检查是否在有效引擎路径范围内
        if valid_engine_paths is not None:
            if not any(webdav_path == p or webdav_path.startswith(p + "/")
                       for p in valid_engine_paths):
                return "skip_filtered"

        # 3. 检查 ghost 保护（使用缓存）
        if webdav_path in self._cache_ghost:
            return "skip_ghost"

        # 3a. 解析映射上下文（如未预解析）
        if mapping_id is None:
            mapping = self.app.get_mapping_for_a(local_path)
            if mapping is None:
                return "fail"
            mapping_id, _, _ = mapping

        # 4. 计算指纹并检查 B 区是否已存在（使用缓存）
        fingerprint = make_strm_fingerprint(webdav_path)
        if (mapping_id, fingerprint) in self._cache_b_fp:
            return "skip_fp"

        # 5. 构建 B 区路径（扫描上下文可注入预计算根消除重复 resolve 风暴，R8）
        try:
            a_root, b_root = self.app._get_scan_roots_for_mapping(mapping_id)
            if a_root is not None and b_root is not None:
                b_local = self.app.build_b_path_from_a(
                    local_path, webdav_path, a_root=a_root, b_root=b_root)
            else:
                b_local = self.app.build_b_path_from_a(local_path, webdav_path)
        except ValueError:
            return "fail"

        # 6. B 文件已存在
        if b_local.exists():
            existing_webdav = read_strm_webdav_path(b_local)
            if existing_webdav == webdav_path:
                return _SyncPrep(
                    local_path=local_path, b_local=b_local,
                    webdav_path=webdav_path, parent=parent,
                    fingerprint=fingerprint, mapping_id=mapping_id,
                    needs_copy=False)
            # B 区文件已存在但 WebDAV 路径不同 — 不覆盖，保护用户操作
            logging.warning(
                "[A->B] B 区文件已存在但 WebDAV 路径不同，跳过覆盖: %s "
                "(existing=%s, new=%s)",
                b_local, existing_webdav, webdav_path)
            return "skip_exists_diff"

        return _SyncPrep(
            local_path=local_path, b_local=b_local,
            webdav_path=webdav_path, parent=parent,
            fingerprint=fingerprint, mapping_id=mapping_id,
            needs_copy=True)

    def _commit_sync_one(self, conn, prep: _SyncPrep,
                         dedup_queue: list | None = None) -> str:
        """现行步骤 6/8 的提交半（T3 拆分）：SAVEPOINT + 双 upsert + dedup 追加。

        失败 ROLLBACK TO + RELEASE；prep.needs_copy=True（本流程拷贝产生的
        新文件）时 unlink 回滚（尽力回滚——unlink 失败被有意忽略，设计决策），
        needs_copy=False（目标已存在路径）不删除用户已有文件。计数与
        _cache_b_fp 维护在主线程调用方进行。
        """
        try:
            # 本条记录写入前建 SAVEPOINT，失败只回滚本记录，
            # 不再回滚整批（旧实现 conn.rollback() 会抹掉同批已落盘 B 区的成功行）
            conn.execute("SAVEPOINT sp_rec")
            self._bulk_upsert_b(conn, str(prep.b_local), prep.webdav_path,
                                prep.parent, prep.local_path, prep.fingerprint, prep.mapping_id)
            self._bulk_upsert_identity(conn, prep.fingerprint, prep.webdav_path,
                                       prep.local_path, str(prep.b_local))
            conn.execute("RELEASE sp_rec")
            self._cache_b_fp.add((prep.mapping_id, prep.fingerprint))
            # 去重延迟到事务提交后执行
            if dedup_queue is not None:
                dedup_queue.append((prep.mapping_id, prep.fingerprint, str(prep.b_local)))
            else:
                try:
                    self.app.ensure_single_visible_instance(
                        prep.fingerprint, str(prep.b_local), mapping_id=prep.mapping_id)
                except Exception as e:
                    logging.warning("[A->B] 去重失败 %s: %s", prep.b_local, e)
            return "success"
        except Exception as e:
            if prep.needs_copy:
                logging.error("[A->B] 数据库写入失败 %s: %s", prep.b_local, e)
            else:
                logging.warning("[A->B] B已存在但数据库写入失败 %s: %s", prep.b_local, e)
            # 只回滚到本记录 SAVEPOINT，保留同批其他成功行
            try:
                conn.execute("ROLLBACK TO sp_rec")
                conn.execute("RELEASE sp_rec")
            except Exception as rb_err:
                logging.warning("[A→B] 回滚 SAVEPOINT 失败: %s", rb_err)
            if prep.needs_copy:
                # 回滚：删除已拷贝的文件
                try:
                    if prep.b_local.exists():
                        prep.b_local.unlink()
                except Exception as rollback_err:
                    # 设计决策: 尽力回滚——unlink 失败被有意忽略
                    logging.warning("[A→B] 回滚删除失败 %s: %s", prep.b_local, rollback_err)
            return "fail"

    def _sync_one_record(self, rec, valid_engine_paths, conn,
                         dedup_queue: list | None = None,
                         mapping_id: str | None = None) -> str:
        """处理单条 A 记录的 A→B 同步。返回状态字符串。

        T3 拆分后为串行两半包装（prepare → copy → commit），refresh/无队列
        调用点（use_bulk=False 交错循环、copy_a_record_to_b_if_needed）行为不变。

        完整实现包括：
        1. 路径过滤和缓存检查
        2. B 文件已存在时的处理
        3. 文件拷贝
        4. 数据库写入（使用 bulk_connection）
        5. 重复实例隔离（延迟到提交后执行）

        Args:
            rec: A 区记录对象（ARecord）
            valid_engine_paths: 有效的引擎路径列表
            conn: bulk_connection 的数据库连接（两种模式都使用）
            dedup_queue: 可选的去重队列，传入时将 (mapping_id, fingerprint, b_local) 追加到此列表，
                         而非直接调用 ensure_single_visible_instance（避免在未提交事务上读取）
            mapping_id: 由调用方预解析的映射标识
        Returns:
            "success" / "skip_ghost" / "skip_fp" / "skip_missing" / "skip_filtered"
            / "skip_exists_diff" / "fail"
        """
        prep_or_status = self._prepare_sync_one(
            rec, valid_engine_paths, mapping_id=mapping_id)
        if isinstance(prep_or_status, str):
            return prep_or_status
        prep = prep_or_status

        if prep.needs_copy:
            # 7. 拷贝文件到 B 区（copy 失败保持现行语义：不清理半拷贝、仅记日志）
            try:
                prep.b_local.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(prep.local_path, prep.b_local)
            except Exception as e:
                logging.warning("[A->B] 拷贝失败 %s: %s", prep.local_path, e)
                return "fail"

        return self._commit_sync_one(conn, prep, dedup_queue)

    def _bulk_upsert_b(self, conn, local_path, webdav_path, parent_webdav_path,
                       source_a_path, fingerprint, mapping_id) -> None:
        """在 bulk_connection 的 conn 上写入 B 区记录（绕过 rw_lock）。

        与 database.py:upsert_b() 逻辑相同，但：
        - 直接使用传入的 conn（不获取 rw_lock）
        - 不单独 commit（由 bulk_connection 统一管理）
        - 变化检测：只在业务字段变化时更新 updated_at
        - 保留既有 status（duplicate/quarantined 不被改回 valid）

        FTS 同步流程：
        1. 新增记录：插入 FTS
        2. 更新记录且 webdav_path 变化：删除旧 FTS 行，插入新 FTS 行
        3. 更新记录但 webdav_path 未变：不操作 FTS
        4. 无变化：不操作
        """
        if not mapping_id:
            raise ValueError("_bulk_upsert_b: mapping_id must be a non-empty string")
        now = time.time()

        # 预读现有记录
        old_row = conn.execute(
            "SELECT rowid, webdav_path, parent_webdav_path, source_a_path, "
            "fingerprint, mapping_id FROM b_strm_files WHERE local_path = ?",
            (local_path,),
        ).fetchone()

        if old_row is None:
            # 新增记录
            # bulk 新增分支同时写 last_verified_at=now，
            # 与单条 upsert 路径(`upsert_b`)一致，避免启动全量同步新增
            # B 记录 last_verified_at 恒为 0。
            conn.execute(
                """
                INSERT INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at, mapping_id, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, 'valid', ?, ?, ?)
                """,
                (local_path, webdav_path, parent_webdav_path,
                 source_a_path, fingerprint, now, mapping_id, now),
            )
            # 插入 FTS
            new_row = conn.execute(
                "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if new_row:
                # 先清理可能残留的同 rowid 孤儿 FTS 行（与 database.py upsert_b 一致）
                conn.execute(
                    "DELETE FROM b_strm_files_fts WHERE rowid = ?", (new_row[0],))
                conn.execute(
                    "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                    (new_row[0], local_path, webdav_path),
                )
        else:
            # 现有记录：比较业务字段（不包括 status）
            old_rowid, old_webdav, old_parent, old_source, old_fp, old_mapping = old_row
            fields_changed = (
                old_webdav != webdav_path or
                old_parent != parent_webdav_path or
                old_source != source_a_path or
                old_fp != fingerprint or
                old_mapping != mapping_id
            )

            if fields_changed:
                # 字段变化：更新记录和时间戳（不更新 status）
                conn.execute(
                    """
                    UPDATE b_strm_files
                    SET webdav_path = ?, parent_webdav_path = ?,
                        source_a_path = ?, fingerprint = ?,
                        mapping_id = ?, updated_at = ?
                    WHERE local_path = ?
                    """,
                    (webdav_path, parent_webdav_path, source_a_path,
                     fingerprint, mapping_id, now, local_path),
                )
                # webdav_path 变化时同步 FTS
                if old_webdav != webdav_path:
                    conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (old_rowid,))
                    conn.execute(
                        "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                        (old_rowid, local_path, webdav_path),
                    )
            # 字段无变化：保留原 updated_at 和 status，不操作 FTS

    def _bulk_upsert_identity(self, conn, fingerprint, webdav_path,
                              source_a_path, current_b_path) -> None:
        """在 bulk_connection 的 conn 上写入 identity 记录（绕过 rw_lock）。

        与 database.py:upsert_identity() 逻辑相同，但：
        - 直接使用传入的 conn（不获取 rw_lock）
        - 不单独 commit（由 bulk_connection 统一管理）
        """
        now = time.time()
        conn.execute(
            """
            INSERT OR REPLACE INTO strm_identity(
                fingerprint, webdav_path, source_a_path, current_b_path, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (fingerprint, webdav_path, source_a_path, current_b_path, now),
        )

    def copy_a_record_to_b_if_needed(
            self, a_local_path: str, webdav_path: str, parent_webdav_path: str) -> bool | None:
        """复制 A→B，但会先检查指纹是否已存在。如果存在则跳过。"""
        if self.db.is_ghost_protected(webdav_path):
            return None
        fingerprint = make_strm_fingerprint(webdav_path)
        # 解析映射上下文
        mapping = self.app.get_mapping_for_a(a_local_path)
        if mapping is None:
            logging.warning("[A->B] 无法解析 A 路径的映射上下文, 跳过复制: %s", a_local_path)
            return None
        mapping_id, _, _ = mapping
        # 按 fingerprint 串行化，与 handle_a_created_or_modified 共用同一锁
        fp_lock = self.app.get_fingerprint_lock(fingerprint)
        with fp_lock:
            if self.db.b_fingerprint_exists(fingerprint, mapping_id):
                return None  # 会被统计为 skip_count
            return self.copy_a_record_to_b(
                a_local_path, webdav_path, parent_webdav_path, mapping_id=mapping_id)

    def copy_a_record_to_b(self, a_local_path: str,
                           webdav_path: str, parent: str,
                           mapping_id: str | None = None) -> bool | None:
        try:
            # 1. 计算物理路径
            b_local = self.app.build_b_path_from_a(a_local_path, webdav_path)

            # 1a. 解析映射上下文（如未提供）
            if mapping_id is None:
                mapping = self.app.get_mapping_for_a(a_local_path)
                if mapping is None:
                    logging.error("[A->B复制失败] 无法解析映射上下文: %s", a_local_path)
                    return False
                mapping_id, _, _ = mapping

            # 2. 血统校验（同步阶段）
            if not self.app._verify_b_path_lineage(
                    str(b_local), webdav_path, is_sync_phase=True):
                return False

        except ValueError as exc:
            logging.error("[A->B复制失败] %s", exc)
            return False

        # 3. 检查是否存在同名同内容文件
        if b_local.exists():
            existing_webdav_path = read_strm_webdav_path(b_local)
            if existing_webdav_path == webdav_path:
                try:
                    fingerprint = make_strm_fingerprint(webdav_path)
                    self.db.upsert_b(
                        str(b_local), webdav_path, parent, a_local_path, fingerprint=fingerprint, mapping_id=mapping_id, status="valid"
                    )
                    self.db.upsert_identity(
                        fingerprint=fingerprint,
                        webdav_path=webdav_path,
                        source_a_path=a_local_path,
                        current_b_path=str(b_local),
                    )
                    self.app.ensure_single_visible_instance(
                        fingerprint, str(b_local), mapping_id=mapping_id)
                    return None
                except Exception as e:
                    logging.error("[A->B跳过失败] %s", e)
                    return False
            # 如果文件存在但 webdav 路径不同，拒绝覆写（保护用户编排成果）
            logging.warning(
                "[A->B跳过] B区文件已存在但webdav源不同，拒绝覆写: %s (现有: %s, 请求: %s)",
                b_local, existing_webdav_path, webdav_path
            )
            # 返回 None（语义=跳过），而非字符串 "skip_exists_diff"
            # 调用方 routes.py:3028 将 None 计入 skipped，字符串会误计入 failed
            return None
        # 如果 WebDAV 源文件已不存在，说明 A 区是冗余文件，清理掉。
        # 三态：仅权威 False 才删；None=不可信 → fail-closed 跳过。
        exists = self.app.admin_api.check_exists(webdav_path)
        if exists is None:
            logging.warning(
                "[A->B跳过] WebDAV 存在性不可信，fail-closed 不清理: %s",
                webdav_path,
            )
            return False
        if exists is False:
            logging.warning(
                "[A->B跳过] WebDAV源文件已不存在，跳过复制并清理A区: %s",
                webdav_path,
            )
            # 清理 A 区冗余文件，检查返回值避免物理/DB不一致
            if Path(a_local_path).exists():
                if safe_remove_file(a_local_path):
                    logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
                else:
                    logging.warning("[A区清理] 物理删除失败，跳过DB删除以保持一致性: %s", a_local_path)
                    return False  # 物理删除失败，不删DB记录
            self.db.delete_a_by_local(a_local_path)
            # 快照行同步失效：与 watcher 删除路径（handle_a_deleted 的
            # delete_a_snapshot）对称，不留孤儿行等全量审计 prune 延迟自愈。
            # 幂等清理，不参与 check_exists/mapping_id 判定。
            self.db.delete_a_snapshot(a_local_path)
            # 设置 ghost 保护，防止再次同步
            self.db.set_ghost_protection(
                webdav_path,
                self.config.behavior.ghost_protect_seconds,
                reason="webdav_not_exists",
            )
            return False
        # ====================================
        # 4. 执行物理拷贝
        try:
            b_local.parent.mkdir(parents=True, exist_ok=True)
            # ===== 修复：检查源文件是否存在 =====
            source_path = Path(a_local_path)
            if not source_path.exists():
                logging.error("[A->B复制失败] 源文件不存在: %s", a_local_path)
                return False
            # ====================================
            shutil.copyfile(a_local_path, b_local)
        except Exception as e:
            logging.error("[A->B复制失败] IO错误: %s", e)
            return False

        # 5. 写入数据库
        try:
            fingerprint = make_strm_fingerprint(webdav_path)
            self.db.upsert_b(
                str(b_local),
                webdav_path,
                parent,
                a_local_path,
                fingerprint=fingerprint,
                mapping_id=mapping_id,
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=a_local_path,
                current_b_path=str(b_local),
            )
            self.app.ensure_single_visible_instance(fingerprint, str(b_local), mapping_id=mapping_id)
            return True
        except Exception as e:
            logging.error(
                "[A->B复制失败] DB错误: %s | b_local=%s webdav=%s parent=%s fingerprint=%s",
                e, b_local, webdav_path, parent, fingerprint,
            )
            # 非“删文件后删 DB”路径。勿当作未守卫删除标记。
            safe_remove_file(b_local)
            return False
