"""Sync Service - handles A->B synchronization logic."""

from __future__ import annotations

import logging
import os
import shutil
import time
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

    def initial_scan_a(self) -> None:
        """启动时批量索引 A 区 STRM 文件到数据库。
        
        性能优化：不再调用 handle_a_created_or_modified()（完整处理流程），
        改为纯批量索引。跳过 HTTP 检查、血统校验、字幕处理。
        
        A区冗余清理：由 cleanup_a_redundant_using_api() 负责（使用 OpenList API）。
        
        日志优化：每 100 条输出进度，解决日志冻结问题。
        """
        logging.info("[初始化] 扫描 A 区 STRM 文件...")
        t0 = time.time()
        BATCH_SIZE = 500
        LOG_INTERVAL = 100
        total_strm = 0
        batch: list[tuple[str, str, str]] = []
        parent_set: set[str] = set()

        for a_root in self.app.a_roots:
            if not a_root.exists():
                logging.warning("[初始化] A 区根目录不存在: %s", a_root)
                continue
            for root, _dirs, files in os.walk(a_root):
                for name in files:
                    if not name.lower().endswith(".strm"):
                        continue
                    file_path = Path(root) / name
                    webdav_path = read_strm_webdav_path(file_path)
                    if not webdav_path:
                        logging.warning("[初始化] 无法解析 STRM: %s", file_path)
                        continue
                    parent = webdav_parent(webdav_path)
                    batch.append((str(file_path), webdav_path, parent))
                    parent_set.add(parent)
                    total_strm += 1

                    if total_strm % LOG_INTERVAL == 0:
                        logging.info(
                            "[初始化] A 区扫描进度: %d 条已索引 (%.1fs)...",
                            total_strm, time.time() - t0)

                    if len(batch) >= BATCH_SIZE:
                        self.db.upsert_a_batch(batch)
                        batch.clear()

            if batch:
                self.db.upsert_a_batch(batch)
                batch.clear()

        if parent_set:
            self.db.save_known_folders_batch(list(parent_set), source="a")

        logging.info(
            "[初始化] A 区扫描完成，共索引 %d 个 STRM 文件 (%.1fs)",
            total_strm, time.time() - t0)

    def scan_a_to_b_full_sync(
            self, valid_engine_paths: list[str] | None = None,
            use_bulk: bool = False) -> None:
        """A -> B 全量同步。
        
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
        
        详见 _sync_one_record 方法的设计决策注释。
        """
        BATCH_COMMIT_SIZE = 1000  # 分批提交大小
        
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

        counters = {"success": 0, "skip_ghost": 0, "skip_fp": 0,
                    "skip_missing": 0, "skip_filtered": 0,
                    "skip_exists_diff": 0, "fail": 0}
        log_interval = max(100, total_count // 100)  # 每 100 条或 1%，取较大值（保证最多 0.5 秒间隔）
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

        try:
            # 两种模式都使用 bulk_connection，区别在于提交策略
            with self.db.bulk_connection() as conn:
                for idx, rec in enumerate(all_a_records, 1):
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
                        logging.info(
                            "[初始化] A -> B 进度: %d/%d (%.0f%%) %.1fs | "
                            "成功=%d 跳过=%d 失败=%d",
                            idx, total_count, idx / total_count * 100,
                            time.time() - t0, c["success"],
                            c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                            + c["skip_filtered"] + c["skip_exists_diff"],
                            c["fail"])
                
                # 提交剩余批次
                if batch_count > 0:
                    conn.commit()
                # 单事务模式：bulk commit 后统一去重
                _flush_dedup_queue()
        finally:
            self._cache_ghost = None
            self._cache_b_fp = None

        c = counters
        total_skip = (c["skip_ghost"] + c["skip_fp"] + c["skip_missing"]
                      + c["skip_filtered"] + c["skip_exists_diff"])
        logging.info(
            "[初始化] A -> B 全量同步完成 (%.1fs) | "
            "成功=%d 跳过=%d(ghost=%d fp=%d 不存在=%d 过滤=%d 路径不同=%d) 失败=%d",
            time.time() - t0, c["success"], total_skip,
            c["skip_ghost"], c["skip_fp"], c["skip_missing"],
            c["skip_filtered"], c["skip_exists_diff"], c["fail"])

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
        # 如果 WebDAV 源文件已不存在，说明 A 区是冗余文件，清理掉
        if not self.app.admin_api.check_exists(webdav_path):
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
