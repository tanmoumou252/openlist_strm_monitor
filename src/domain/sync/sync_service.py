"""Sync Service - handles A->B synchronization logic."""

from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app_service import AppService
    from database import Database
    from config import AppConfig

from utils import read_strm_webdav_path, safe_remove_file, webdav_parent, make_strm_fingerprint


class SyncService:
    """A->B 同步服务"""

    def __init__(self, app: AppService) -> None:
        self.app = app
        self.config: AppConfig = app.config
        self.db: Database = app.db
        # 启动同步缓存（sync 期间有效，finally 清除）
        self._cache_ghost: set[str] | None = None
        self._cache_b_fp: set[str] | None = None

    def initial_scan_a(self, use_bulk: bool = False) -> None:
        """启动时批量索引 A 区 STRM 文件到数据库。
        
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
        """
        logging.info("[初始化] 扫描 A 区 STRM 文件（%s）...", 
                     "bulk模式" if use_bulk else "标准模式")
        t0 = time.time()
        BATCH_SIZE = 1000
        LOG_INTERVAL = 100
        total_strm = 0
        batch: list[tuple[str, str, str]] = []
        parent_set: set[str] = set()
        last_log_time = time.time()

        def process_strm_file(file_path: Path) -> tuple[str, str, str] | None:
            """处理单个 .strm 文件，返回 (local_path, webdav_path, parent) 或 None"""
            webdav_path = read_strm_webdav_path(file_path)
            if not webdav_path:
                logging.debug("[初始化] 无法解析 STRM: %s", file_path)
                return None
            parent = webdav_parent(webdav_path)
            return (str(file_path), webdav_path, parent)

        def flush_batch():
            """批量写入数据库。闭包捕获 conn 和 batch。"""
            if not batch:
                return
            if use_bulk:
                self._upsert_a_batch_bulk(conn, batch)
            else:
                self.db.upsert_a_batch(batch)
            batch.clear()

        # 根据模式选择连接：bulk_connection 绕过 rw_lock，仅启动时单线程安全
        conn = None
        bulk_ctx = None
        if use_bulk:
            bulk_ctx = self.db.bulk_connection()
            conn = bulk_ctx.__enter__()

        try:
            for a_root in self.app.a_roots:
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
                
                # 使用多线程并发处理
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = {executor.submit(process_strm_file, fp): fp for fp in strm_files}
                    
                    for future in as_completed(futures):
                        result = future.result()
                        if result:
                            local_path, webdav_path, parent = result
                            batch.append((local_path, webdav_path, parent))
                            parent_set.add(parent)
                            total_strm += 1

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
        finally:
            # bulk_connection 在 __exit__ 时自动 commit（正常退出）或 rollback（异常）
            if bulk_ctx is not None:
                bulk_ctx.__exit__(None, None, None)

        # 以下操作使用 self.connection()（独立连接），必须在 bulk_connection 提交后执行
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
        - 使用 INSERT OR REPLACE（简化逻辑，延迟 FTS 重建不关心中间 rowid 变化）
        """
        if not records:
            return 0
        now = time.time()
        data = [(lp, wp, pwp, now) for lp, wp, pwp in records]
        
        conn.executemany(
            """
            INSERT OR REPLACE INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            data,
        )
        
        return len(data)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None,
            use_bulk: bool = False) -> None:
        """A -> B 全量同步（两遍结构：索引 + 执行）。
        
        第一遍（索引阶段）：遍历所有 A 记录，计算目标路径，建立
        target_path -> [source_info, ...] 索引，并检测目标路径冲突。
        只读操作（ghost/fp 缓存检查），不产生文件复制或 DB 写入。
        
        冲突检测：第一遍完成后，遍历索引，标记存在多个不同 WebDAV
        身份的目标为冲突，全部安全跳过。
        
        第二遍（执行阶段）：遍历原始记录，对非冲突目标调用
        _sync_one_record 执行现有同步逻辑；对冲突目标跳过。
        
        Args:
            valid_engine_paths: 限制同步范围。
            use_bulk: True 用单事务提交（首次启动，无并发）。
                      False 用分批提交（主动刷新，有并发，每 1000 条提交一次）。
        
        并发安全说明
        -----------
        批量同步使用 bulk_connection 绕过 rw_lock，写入未提交前对其他连接不可见。
        _sync_one_record 不使用指纹锁，依赖三层防御：
        - L1: 内存缓存 _cache_b_fp（处理同批次重复）
        - L2: 文件系统检查 b_local.exists()（磁盘文件可见）
        - L3: ensure_single_visible_instance（兜底去重，通过 dedup_queue 延迟到提交后执行）
        """
        BATCH_COMMIT_SIZE = 1000  # 分批提交大小
        MAX_CONFLICT_EXAMPLES = 5  # 汇总输出最多示例数
        SLOW_OP_THRESHOLD = 3.0  # 慢操作告警阈值（秒）
        
        logging.info("[初始化] A -> B 全量同步开始 (%s)", 
                     "单事务模式" if use_bulk else "分批提交模式")
        if valid_engine_paths is not None:
            logging.info("[初始化] 限制同步范围: %s", valid_engine_paths)

        t0 = time.time()
        all_a_records = self.db.get_all_a_records()
        total_count = len(all_a_records)
        logging.info("[初始化] A -> B 同步: 共 %d 条待处理", total_count)

        # 预加载读缓存（两种模式都受益）
        self._cache_ghost = self.db.get_all_ghost_protected_paths()
        self._cache_b_fp = self.db.get_all_b_fingerprints()
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
        # 记录每条 A 记录对应的 target_path（第二遍复用）
        rec_target_map: list[str | None] = [None] * total_count
        pass1_skipped = {"skip_ghost": 0, "skip_fp": 0, "skip_missing": 0,
                         "skip_filtered": 0}
        
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
            
            fingerprint = make_strm_fingerprint(webdav_path)
            if fingerprint in self._cache_b_fp:
                pass1_skipped["skip_fp"] += 1
                continue
            
            # 计算目标路径（只读，无副作用）
            try:
                b_local = self.app.build_b_path_from_a(local_path, webdav_path)
            except ValueError:
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
        # 安全跳过，不输出猜测性“已检测上游覆盖”警告。
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
            "skip_exists_diff": 0,
            "skip_target_conflict": 0,
            "fail": 0,
        }
        log_interval = max(100, total_count // 100)
        batch_count = 0
        dedup_queue: list[tuple[str, str]] = []  # 延迟去重队列

        def _flush_dedup_queue():
            """提交后执行延迟的去重操作（此时数据已可见）"""
            if not dedup_queue:
                return
            for fp, b_path in dedup_queue:
                try:
                    self.app.ensure_single_visible_instance(fp, b_path)
                except Exception as e:
                    logging.warning("[A->B] 去重失败 %s: %s", b_path, e)
            dedup_queue.clear()

        t_pass2 = time.time()
        try:
            with self.db.bulk_connection() as conn:
                for idx, rec in enumerate(all_a_records, 1):
                    target_path = rec_target_map[idx - 1]
                    
                    # Pass 1 中被跳过的记录（ghost/fp/missing/filtered）target_path 为 None
                    if target_path is None:
                        continue
                    
                    if target_path in target_conflicts:
                        counters["skip_target_conflict"] += 1
                        continue
                    
                    result = self._sync_one_record(rec, valid_engine_paths, conn,
                                                   dedup_queue)
                    counters[result] = counters.get(result, 0) + 1
                    batch_count += 1

                    # 分批提交模式：每 1000 条提交一次，释放锁
                    if not use_bulk and batch_count >= BATCH_COMMIT_SIZE:
                        conn.commit()
                        _flush_dedup_queue()
                        batch_count = 0
                        logging.debug("[初始化] 分批提交: 已处理 %d 条", idx)

                    if idx % log_interval == 0:
                        c = counters
                        total_skip_2 = (
                            c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                            + c["skip_filtered"] + c["skip_exists_diff"]
                            + c["skip_target_conflict"])
                        logging.info(
                            "[初始化] A -> B 进度: %d/%d (%.0f%%) %.1fs | "
                            "成功=%d 跳过=%d 冲突=%d 失败=%d",
                            idx, total_count, idx / total_count * 100,
                            time.time() - t0, c["success"],
                            total_skip_2, c["skip_target_conflict"], c["fail"])
                
                # 提交剩余批次
                if batch_count > 0:
                    conn.commit()
                # 单事务模式：bulk commit 后统一去重
                _flush_dedup_queue()
        finally:
            self._cache_ghost = None
            self._cache_b_fp = None

        t_pass2_elapsed = time.time() - t_pass2
        c = counters
        total_skip = (c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                      + c["skip_filtered"] + c["skip_exists_diff"]
                      + c["skip_target_conflict"])
        logging.info(
            "[初始化] A -> B 全量同步完成 (%.1fs) | "
            "成功=%d 跳过=%d(ghost=%d fp=%d 不存在=%d 过滤=%d "
            "路径不同=%d 目标冲突=%d) 失败=%d",
            time.time() - t0, c["success"], total_skip,
            c["skip_ghost"], c["skip_fp"], c["skip_missing"],
            c["skip_filtered"], c["skip_exists_diff"],
            c["skip_target_conflict"], c["fail"])

        # 生成人工处理清单（冲突目标）
        if target_conflicts:
            self._write_manual_review_list(target_index, target_conflicts)

    def _write_manual_review_list(self, target_index: dict, target_conflicts: set) -> None:
        """将冲突跳过的 A 源清单写入 B 区根目录的清单文件。
        
        格式：Markdown 表格，含 A 源路径、WebDAV 路径、目标路径、原因。
        文件名：`_MANUAL_REVIEW_YYYYMMDD_HHMMSS.md`
        """
        from pathlib import Path, PosixPath, WindowsPath
        # 使用第一个冲突目标路径对应的 B 根
        first_target = next(iter(target_conflicts)) if target_conflicts else None
        if first_target:
            b_root = self.app.get_b_root_for_path(first_target)
        else:
            b_root = self.app.b_root  # 兼容回退
        # 在测试场景中 self.app.b_root 可能是 Mock 对象，不生成清单
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

    def _sync_one_record(self, rec, valid_engine_paths, conn,
                         dedup_queue: list | None = None) -> str:
        """处理单条 A 记录的 A→B 同步。返回状态字符串。
        
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
            dedup_queue: 可选的去重队列，传入时将 (fingerprint, b_local) 追加到此列表，
                         而非直接调用 ensure_single_visible_instance（避免在未提交事务上读取）
        Returns:
            "success" / "skip_ghost" / "skip_fp" / "skip_missing" / "skip_filtered"
            / "skip_exists_diff" / "fail"
        
        设计决策：为什么不使用指纹锁（get_fingerprint_lock）
        --------------------------------------------------
        经过代码验证，_sync_one_record 不需要添加指纹锁，原因如下：
        
        1. 现有三层防御已经足够：
           - L1: 内存缓存 _cache_b_fp — 快速过滤已知指纹
           - L2: 文件系统检查 b_local.exists() — 磁盘文件对所有线程可见
           - L3: ensure_single_visible_instance — 兜底去重（将多余实例改名为 .duplicate）
        
        2. 添加指纹锁会引入性能灾难：
           - 指纹锁持有时间从毫秒级变成秒级（包含文件拷贝）
           - 50,000 条记录 × 每次持锁 0.1-1 秒 = 1.4-2.8 小时总锁持有时间
           - 会严重阻塞 watchdog 的 handle_a_created_or_modified（使用同一把锁）
        
        3. b_fingerprint_exists 看不到 bulk_connection 的未提交写入：
           - bulk_connection 绕过 rw_lock，直接 sqlite3.connect
           - b_fingerprint_exists 获取 rw_lock 读锁，打开新连接
           - SQLite 事务隔离导致新连接看不到未提交写入
           - "双重检查"只能看到 watchdog 的已提交写入，看不到同批次写入
           - 内存缓存 _cache_b_fp 已经能处理同批次重复
        
        4. 并发场景分析：
           - _sync_one_record vs handle_a_created_or_modified：
             handle_a_created_or_modified 在指纹锁内检查 b_local.exists()，
             如果文件已存在则 upsert 已有文件并 return，不到达 copy_a_record_to_b
           - _sync_one_record vs copy_a_record_to_b_if_needed：
             同样被 L2 文件系统检查覆盖
           - 真正的 TOCTOU（两个线程同时检查 b_local.exists() → 都得到 False）：
             概率极低（需要微秒级时序），且 L3 兜底
        
        结论：添加指纹锁不带来实质安全提升，但引入性能风险和代码复杂度。
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

        # 4. 计算指纹并检查 B 区是否已存在（使用缓存）
        fingerprint = make_strm_fingerprint(webdav_path)
        if fingerprint in self._cache_b_fp:
            return "skip_fp"

        # 5. 构建 B 区路径
        try:
            b_local = self.app.build_b_path_from_a(local_path, webdav_path)
        except ValueError:
            return "fail"

        # 6. B 文件已存在
        if b_local.exists():
            existing_webdav = read_strm_webdav_path(b_local)
            if existing_webdav == webdav_path:
                # 使用 bulk_connection 写入数据库
                self._bulk_upsert_b(conn, str(b_local), webdav_path,
                                    parent, local_path, fingerprint)
                self._bulk_upsert_identity(conn, fingerprint, webdav_path,
                                           local_path, str(b_local))
                self._cache_b_fp.add(fingerprint)
                # 去重延迟到事务提交后执行
                if dedup_queue is not None:
                    dedup_queue.append((fingerprint, str(b_local)))
                else:
                    try:
                        self.app.ensure_single_visible_instance(fingerprint, str(b_local))
                    except Exception as e:
                        logging.warning("[A->B] 去重失败 %s: %s", b_local, e)
                return "success"
            else:
                # B 区文件已存在但 WebDAV 路径不同 — 不覆盖，保护用户操作
                logging.warning(
                    "[A->B] B 区文件已存在但 WebDAV 路径不同，跳过覆盖: %s "
                    "(existing=%s, new=%s)",
                    b_local, existing_webdav, webdav_path)
                return "skip_exists_diff"

        # 7. 拷贝文件到 B 区
        try:
            b_local.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_path, b_local)
        except Exception as e:
            logging.warning("[A->B] 拷贝失败 %s: %s", local_path, e)
            return "fail"

        # 8. 写入数据库（使用 bulk_connection）
        try:
            self._bulk_upsert_b(conn, str(b_local), webdav_path,
                                parent, local_path, fingerprint)
            self._bulk_upsert_identity(conn, fingerprint, webdav_path,
                                       local_path, str(b_local))
            self._cache_b_fp.add(fingerprint)
            # 去重延迟到事务提交后执行
            if dedup_queue is not None:
                dedup_queue.append((fingerprint, str(b_local)))
            else:
                try:
                    self.app.ensure_single_visible_instance(fingerprint, str(b_local))
                except Exception as e:
                    logging.warning("[A->B] 去重失败 %s: %s", b_local, e)
            return "success"
        except Exception as e:
            logging.error("[A->B] 数据库写入失败 %s: %s", b_local, e)
            # 回滚：删除已拷贝的文件
            try:
                if b_local.exists():
                    b_local.unlink()
            except Exception:
                pass
            return "fail"

    def _bulk_upsert_b(self, conn, local_path, webdav_path, parent_webdav_path,
                       source_a_path, fingerprint) -> None:
        """在 bulk_connection 的 conn 上写入 B 区记录（绕过 rw_lock）。
        
        与 database.py:upsert_b() 逻辑相同，但：
        - 直接使用传入的 conn（不获取 rw_lock）
        - 不单独 commit（由 bulk_connection 统一管理）
        - 正确处理 FTS 孤儿行（与 upsert_b 一致的 rowid 管理）
        
        FTS 孤儿行处理流程：
        1. 获取旧 rowid（如果存在）
        2. 删除旧 rowid 的 FTS 行（避免 REPLACE 改变 rowid 后残留孤儿）
        3. INSERT OR REPLACE 基表
        4. 获取新 rowid
        5. 删除新 rowid 上可能残留的孤儿 FTS 行（防止 constraint failed）
        6. 插入新 FTS 行
        """
        now = time.time()
        
        # 步骤 1-2：获取旧 rowid，删除旧 FTS 行
        old_row = conn.execute(
            "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
        ).fetchone()
        if old_row:
            conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (old_row[0],))
        
        # 步骤 3：INSERT OR REPLACE 基表
        conn.execute(
            """
            INSERT OR REPLACE INTO b_strm_files(
                local_path, webdav_path, parent_webdav_path,
                source_a_path, fingerprint, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'valid', ?)
            """,
            (local_path, webdav_path, parent_webdav_path,
             source_a_path, fingerprint, now),
        )
        
        # 步骤 4-6：获取新 rowid，清理残留，插入新 FTS 行
        new_row = conn.execute(
            "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
        ).fetchone()
        if new_row:
            # 先删除该 rowid 上可能残留的孤儿 FTS 行（防止 constraint failed）
            conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (new_row[0],))
            # 插入新 FTS 行
            conn.execute(
                "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                (new_row[0], local_path, webdav_path),
            )

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
        # 按 fingerprint 串行化，与 handle_a_created_or_modified 共用同一锁（P1-4）
        fp_lock = self.app.get_fingerprint_lock(fingerprint)
        with fp_lock:
            if self.db.b_fingerprint_exists(fingerprint):
                return None  # 会被统计为 skip_count
            return self.copy_a_record_to_b(
                a_local_path, webdav_path, parent_webdav_path)

    def copy_a_record_to_b(self, a_local_path: str,
                           webdav_path: str, parent: str) -> bool | None:
        try:
            # 1. 计算物理路径
            b_local = self.app.build_b_path_from_a(a_local_path, webdav_path)

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
                        str(b_local), webdav_path, parent, a_local_path, fingerprint=fingerprint, status="valid"
                    )
                    self.db.upsert_identity(
                        fingerprint=fingerprint,
                        webdav_path=webdav_path,
                        source_a_path=a_local_path,
                        current_b_path=str(b_local),
                    )
                    self.app.ensure_single_visible_instance(
                        fingerprint, str(b_local))
                    return None
                except Exception as e:
                    logging.error("[A->B跳过失败] %s", e)
                    return False
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
            # 清理 A 区冗余文件
            if Path(a_local_path).exists():
                safe_remove_file(a_local_path)
                logging.info("[A区清理] 删除冗余STRM: %s", a_local_path)
            self.db.delete_a_by_local(a_local_path)
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
                status="valid")
            self.db.upsert_identity(
                fingerprint=fingerprint,
                webdav_path=webdav_path,
                source_a_path=a_local_path,
                current_b_path=str(b_local),
            )
            self.app.ensure_single_visible_instance(fingerprint, str(b_local))
            return True
        except Exception as e:
            logging.error(
                "[A->B复制失败] DB错误: %s | b_local=%s webdav=%s parent=%s fingerprint=%s",
                e, b_local, webdav_path, parent, fingerprint,
            )
            safe_remove_file(b_local)
            return False
