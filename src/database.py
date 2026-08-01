from __future__ import annotations

import os
import sqlite3
import threading
import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from utils import escape_like

class ReadWriteLock:
    """读写锁：允许多个读者并发访问，写者独占。

    实现要点：
    - 读者通过 _read_lock 与写者互斥（第一个读者获取，最后一个读者释放）
    - 写者通过 _write_lock 互斥（所有写者串行化）
    - 写者优先：当有写者等待时，新读者阻塞，防止写者饥饿
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._readers = 0
        self._writers_waiting = 0
        self._writers_active = 0
        self._read_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._no_writers = threading.Condition(self._lock)

    @contextmanager
    def read_locked(self):
        with self._lock:
            # 有写者等待或活跃时，读者等待（写者优先，防止饥饿）
            while self._writers_waiting > 0 or self._writers_active > 0:
                self._no_writers.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._lock:
                self._readers -= 1
                if self._readers == 0:
                    self._no_writers.notify_all()

    @contextmanager
    def write_locked(self):
        with self._lock:
            self._writers_waiting += 1
        # 所有写者通过 _write_lock 串行化
        self._write_lock.acquire()
        try:
            with self._lock:
                self._writers_waiting -= 1
                self._writers_active += 1
            # 第一个写者获取 _read_lock，阻止新读者进入
            self._read_lock.acquire()
            try:
                yield
            finally:
                self._read_lock.release()
                with self._lock:
                    self._writers_active -= 1
                    if self._writers_active == 0:
                        self._no_writers.notify_all()
        finally:
            self._write_lock.release()



# ============================================================
# 数据库记录类型定义
# ============================================================


@dataclass(frozen=True)
class ARecord:
    """A 区 STRM 文件记录"""
    local_path: str
    webdav_path: str
    parent_webdav_path: str
    updated_at: float


@dataclass(frozen=True)
class BRecord:
    """B 区 STRM 文件记录"""
    local_path: str
    webdav_path: str
    parent_webdav_path: str
    source_a_path: str | None
    fingerprint: str | None
    status: str
    updated_at: float
    mapping_id: str = ""  # Mapping ID for multi-A↔multi-B isolation


@dataclass(frozen=True)
class IdentityRecord:
    """STRM 身份记录"""
    fingerprint: str
    webdav_path: str
    source_a_path: str | None
    current_b_path: str | None
    updated_at: float


@dataclass(frozen=True)
class CRecord:
    """C 区（幽灵文件）记录"""
    local_path: str
    webdav_path: str
    original_b_path: str
    ghost_root: str
    moved_at: float


@dataclass(frozen=True)
class BoundaryRecord:
    """媒体边界映射记录"""
    mapping_id: str
    fingerprint: str
    source_media_name: str
    current_media_name: str
    engine_entry_path: str
    updated_at: float


@dataclass(frozen=True)
class BLineageSnapshotRecord:
    mapping_id: str
    local_path: str
    file_size: int
    mtime_ns: int
    fingerprint: str | None
    mapping_version: str
    lineage_version: int
    validation_state: str
    verified_at: float


@dataclass(frozen=True)
class ProtectedRootRecord:
    """受保护根目录记录"""
    root_path: str
    trash_path: str
    active: bool
    updated_at: float


@dataclass(frozen=True)
class SubtitleRecord:
    """字幕记录"""
    id: int
    local_path: str
    target_path: str
    fingerprint: str
    season: int | None
    episode: int | None
    lang_code: str | None
    status: str
    created_at: str
    updated_at: str


class Database:
    _last_ghost_cleanup: float

    # 性能优化 PRAGMA 配置（共享于 connection / read_connection）
    _PRAGMA_STATEMENTS: tuple[str, ...] = (
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=10000",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA cache_size=-64000",      # 64MB page cache
        "PRAGMA temp_store=MEMORY",
        "PRAGMA mmap_size=268435456",     # 256MB mmap
    )

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """为新建连接应用统一的性能优化 PRAGMA。"""
        for stmt in self._PRAGMA_STATEMENTS:
            conn.execute(stmt)

    # Simple 分词器资源根目录（src/tokenizers/simple/）
    _SIMPLE_TOKENIZER_DIR: Path = Path(__file__).parent / "tokenizers" / "simple"
    _SIMPLE_DLL: Path = _SIMPLE_TOKENIZER_DIR / "simple.dll"
    _SIMPLE_VERSION_FILE: Path = _SIMPLE_TOKENIZER_DIR / "VERSION"

    def _load_simple_tokenizer(self, conn: sqlite3.Connection) -> bool:
        """加载 simple 中文分词器扩展。成功返回 True，失败软降级返回 False。"""
        try:
            conn.enable_load_extension(True)
            # simple.dll 位于 src/tokenizers/simple/simple.dll
            simple_dll = self._SIMPLE_DLL
            if simple_dll.exists():
                conn.load_extension(str(simple_dll))
                self._fts_tokenizer = 'simple'
                # 读取并缓存版本信息，供运维感知当前分词器版本
                self._simple_version = self._read_simple_version()
                logging.debug(
                    "[DB] Simple tokenizer loaded successfully from %s (version: %s)",
                    simple_dll, self._simple_version or "unknown",
                )
                return True
            else:
                logging.warning(f"[DB] Simple tokenizer not found at {simple_dll}, falling back to unicode61")
                return False
        except Exception as e:
            logging.warning(f"[DB] Failed to load simple tokenizer: {e}, falling back to unicode61")
            return False

    @classmethod
    def _read_simple_version(cls) -> str:
        """读取 src/tokenizers/simple/VERSION 的简明版本标识，失败返回空串。"""
        try:
            text = cls._SIMPLE_VERSION_FILE.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.lower().startswith("bundled-version:"):
                    return stripped.split(":", 1)[1].strip()
            # 兜底：返回首行非空内容
            return next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        except Exception:
            return ""

    # ---- 瞬时 SQLite 错误识别（Windows 杀毒锁文件/磁盘瞬断）----
    _TRANSIENT_HINTS: tuple[str, ...] = (
        "readonly", "database is locked", "disk i/o error",
    )

    def _is_transient_error(self, exc: sqlite3.OperationalError) -> bool:
        """判断 OperationalError 是否为可恢复的瞬时错误。"""
        msg = str(exc).lower()
        return any(h in msg for h in self._TRANSIENT_HINTS)

    def _probe_writeable(self, conn: sqlite3.Connection) -> None:
        """用 BEGIN IMMEDIATE / ROLLBACK 探测连接是否可写（不变更 schema）。

        BEGIN IMMEDIATE 会尝试获取 RESERVED 锁，在文件只读或 query_only
        模式下均会抛 OperationalError。
        仅在 connection() 内部调用。失败时抛出 OperationalError 并附诊断。
        不做重试——重试由上层 _worker 周期自然完成，避免在持有
        ReadWriteLock 期间阻塞。
        """
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            self._log_db_diagnostics()
            raise exc

    def _log_db_diagnostics(self) -> None:
        """输出数据库文件级诊断信息，辅助排查 readonly 问题。"""
        try:
            p = Path(self.db_path)
            exists = p.exists()
            if not exists:
                logging.warning("[DB] 诊断: db_path=%s 文件不存在", self.db_path)
                return
            writable = os.access(str(p), os.W_OK)
            parent = p.parent
            parent_writable = os.access(str(parent), os.W_OK) if parent.exists() else False
            size = p.stat().st_size
            wal = Path(str(p) + "-wal")
            shm = Path(str(p) + "-shm")
            logging.warning(
                "[DB] 诊断: db_path=%s exists=True writable=%s size=%s "
                "parent_writable=%s wal_exists=%s shm_exists=%s",
                self.db_path, writable, size, parent_writable,
                wal.exists(), shm.exists(),
            )
        except Exception as diag_exc:
            logging.debug("[DB] 诊断输出失败: %s", diag_exc)

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """写连接上下文管理器 - 用于修改数据库的操作。

        打开后做一次 BEGIN/ROLLBACK 写能力探测，捕获 Windows 杀软瞬时锁
        等 readonly 异常。探测失败立即上抛（附诊断日志），不做重试——调用方
        rw_lock.write_locked 持锁期间不得 sleep，重试由上层 _worker 周期
        自然完成。
        """
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            self._apply_pragmas(conn)
            self._load_simple_tokenizer(conn)
            self._probe_writeable(conn)
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @contextmanager
    def read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """只读连接上下文管理器 - 不持有写锁，允许并发读取 (WAL模式下安全)。

        与 connection() 共享诊断逻辑：WAL PRAGMA 本身需要写权限，
        瞬时 readonly 同样可能击中读路径（WebUI 路由、watchlist_match）。
        """
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                self._apply_pragmas(conn)
            except sqlite3.OperationalError as exc:
                if self._is_transient_error(exc):
                    self._log_db_diagnostics()
                raise
            self._load_simple_tokenizer(conn)
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @contextmanager
    def bulk_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """批量操作专用连接——打开一次，PRAGMA/分词器只加载一次，全程复用。

        调用方在 with 块内执行所有 SQL。正常结束时自动 COMMIT，异常时 ROLLBACK。

        ⚠️ 绕过 rw_lock 和 _probe_writeable——仅用于启动时单线程批量同步。
        跨进程场景安全（SQLite WAL 自身处理并发），同进程多线程场景不安全。
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        self._apply_pragmas(conn)
        self._load_simple_tokenizer(conn)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.rw_lock = ReadWriteLock()
        self._last_ghost_cleanup = 0.0
        self._ghost_cleanup_lock = threading.Lock()
        self._fts_tokenizer = 'unicode61'  # 默认降级值,simple 加载成功后更新为 'simple'
        self._simple_version: str | None = None  # simple 分词器版本(加载成功后填充,见 _load_simple_tokenizer)

        # ===== 修复：确保数据库文件可写 =====
        self._ensure_db_writable()
        # ====================================
        logging.info("[DB] 开始初始化数据库表结构")
        self._create_schema()
        self.init_subtitle_table()  # 字幕表单独初始化(避免 _create_schema 重复定义)
        logging.info("[DB] 数据库核心表与索引核对并创建完成！")

    def _create_schema(self) -> None:
        """创建核心数据库表结构和索引(幂等操作,可安全重复调用)。"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS a_strm_files (
                    local_path TEXT PRIMARY KEY,
                    webdav_path TEXT NOT NULL,
                    parent_webdav_path TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS b_strm_files (
                    local_path TEXT PRIMARY KEY,
                    webdav_path TEXT NOT NULL,
                    parent_webdav_path TEXT NOT NULL,
                    source_a_path TEXT,
                    fingerprint TEXT,
                    status TEXT DEFAULT 'valid',
                    updated_at REAL NOT NULL,
                    mapping_id TEXT NOT NULL DEFAULT ''
                )
                """)

            # 旧数据库迁移：新列必须在建索引和查询前存在。
            existing_b_columns = {
                row[1] for row in cur.execute("PRAGMA table_info(b_strm_files)").fetchall()
            }
            if existing_b_columns and "mapping_id" not in existing_b_columns:
                cur.execute("ALTER TABLE b_strm_files ADD COLUMN mapping_id TEXT NOT NULL DEFAULT ''")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS strm_identity (
                    fingerprint TEXT PRIMARY KEY,
                    webdav_path TEXT NOT NULL,
                    source_a_path TEXT,
                    current_b_path TEXT,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS c_ghost_files (
                    local_path TEXT PRIMARY KEY,
                    webdav_path TEXT NOT NULL,
                    original_b_path TEXT NOT NULL,
                    ghost_root TEXT NOT NULL,
                    moved_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS ghost_protection (
                    webdav_path TEXT PRIMARY KEY,
                    expire_time REAL NOT NULL,
                    reason TEXT
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS known_folders (
                    folder_path TEXT PRIMARY KEY,
                    source TEXT,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS protected_roots (
                    root_path TEXT PRIMARY KEY,
                    trash_path TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS protected_roots_snapshot (
                    root_path TEXT PRIMARY KEY,
                    trash_path TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_control (
                    control_key TEXT PRIMARY KEY,
                    control_value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS strm_media_boundary (
                    mapping_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    source_media_name TEXT NOT NULL,
                    current_media_name TEXT NOT NULL,
                    engine_entry_path TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (mapping_id, fingerprint)
                )
                """)

            # 旧 boundary 表迁移检测：旧表缺少 mapping_id 或仅有 fingerprint 主键时重建
            existing_boundary_columns = {
                row[1] for row in cur.execute(
                    "PRAGMA table_info(strm_media_boundary)").fetchall()
            }
            if existing_boundary_columns and "mapping_id" not in existing_boundary_columns:
                logging.warning("[DB] 检测到旧版 strm_media_boundary 表，启动安全迁移（删除无可归属 mapping 的旧行）")
                try:
                    cur.execute("""
                        CREATE TABLE strm_media_boundary_new (
                            mapping_id TEXT NOT NULL,
                            fingerprint TEXT NOT NULL,
                            source_media_name TEXT NOT NULL,
                            current_media_name TEXT NOT NULL,
                            engine_entry_path TEXT NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (mapping_id, fingerprint)
                        )
                    """)
                    # 旧记录无法可靠确定 mapping_id，按用户选择不猜测、不迁移，直接重建空表
                    cur.execute("DROP TABLE strm_media_boundary")
                    cur.execute("ALTER TABLE strm_media_boundary_new RENAME TO strm_media_boundary")
                    logging.info("[DB] 旧版 strm_media_boundary 表迁移完成，不可归属的旧记录已删除")
                except Exception as exc:
                    logging.error("[DB] strm_media_boundary 表迁移失败，回滚: %s", exc)
                    raise

            # 映射级身份投影表：每个 mapping_id + fingerprint 组合记录当前 visible B 路径
            cur.execute("""
                CREATE TABLE IF NOT EXISTS b_identity_projection (
                    fingerprint TEXT NOT NULL,
                    mapping_id TEXT NOT NULL,
                    current_b_path TEXT,
                    status TEXT DEFAULT 'valid',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (fingerprint, mapping_id)
                )
                """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_id_proj_mapping ON b_identity_projection(mapping_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_id_proj_fp ON b_identity_projection(fingerprint)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS b_lineage_snapshot (
                    mapping_id TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    fingerprint TEXT,
                    mapping_version TEXT NOT NULL,
                    lineage_version INTEGER NOT NULL,
                    validation_state TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    PRIMARY KEY (mapping_id, local_path)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_lineage_snapshot_path ON b_lineage_snapshot(local_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_lineage_snapshot_version ON b_lineage_snapshot(mapping_id, mapping_version, lineage_version)")

            # 创建索引
            cur.execute("CREATE INDEX IF NOT EXISTS idx_a_strm_webdav_path ON a_strm_files(webdav_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_webdav_path ON b_strm_files(webdav_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_fingerprint ON b_strm_files(fingerprint)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_status ON b_strm_files(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_mapping_id ON b_strm_files(mapping_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_mapping_fp ON b_strm_files(mapping_id, fingerprint)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_identity_webdav_path ON strm_identity(webdav_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_identity_current_b_path ON strm_identity(current_b_path)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_boundary_source_name ON strm_media_boundary(source_media_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_boundary_current_name ON strm_media_boundary(current_media_name)")

            # 排序字段索引（local_path 是 PRIMARY KEY，SQLite 自动索引，无需重复）
            cur.execute("CREATE INDEX IF NOT EXISTS idx_a_strm_updated_at ON a_strm_files(updated_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_b_strm_updated_at ON b_strm_files(updated_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_c_ghost_moved_at ON c_ghost_files(moved_at)")

            # 创建 FTS5 全文搜索虚拟表（使用 self._fts_tokenizer，simple 或 unicode61 降级）
            tok = self._fts_tokenizer
            cur.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS a_strm_files_fts USING fts5(
                    local_path,
                    webdav_path,
                    tokenize='{tok}'
                )
            """)

            cur.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS b_strm_files_fts USING fts5(
                    local_path,
                    webdav_path,
                    tokenize='{tok}'
                )
            """)

            cur.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS c_ghost_files_fts USING fts5(
                    local_path,
                    webdav_path,
                    tokenize='{tok}'
                )
            """)

            # 首次回填：FTS 表为空但主表非空时，从主表批量填充
            self._backfill_fts_if_empty(conn)

            # 孤儿清理：FTS 行数与主表不一致时重建
            self._rebuild_fts_if_stale(conn)

            conn.commit()

    def init_db(self) -> None:
        """初始化数据库表结构（幂等操作，可安全重复调用）。"""
        logging.debug("[DB] init_db 被调用，确保所有表已创建")
        self._create_schema()

    def _backfill_fts_if_empty(self, conn: sqlite3.Connection) -> None:
        """首次回填：FTS 表为空但主表非空时，从主表批量填充。"""
        tables = [
            ("a_strm_files", "a_strm_files_fts"),
            ("b_strm_files", "b_strm_files_fts"),
            ("c_ghost_files", "c_ghost_files_fts"),
        ]
        for main_table, fts_table in tables:
            fts_count = conn.execute(f"SELECT COUNT(*) FROM {fts_table}").fetchone()[0]
            if fts_count == 0:
                main_count = conn.execute(f"SELECT COUNT(*) FROM {main_table}").fetchone()[0]
                if main_count > 0:
                    conn.execute(
                        f"INSERT INTO {fts_table}(rowid, local_path, webdav_path) "
                        f"SELECT rowid, local_path, webdav_path FROM {main_table}"
                    )
                    logging.info(f"[DB] 首次回填 {fts_table}: {main_count} 条记录")

    def _rebuild_fts_if_stale(self, conn: sqlite3.Connection) -> None:
        """孤儿清理：FTS 行数与主表不一致时，全清后重建。"""
        tables = [
            ("a_strm_files", "a_strm_files_fts"),
            ("b_strm_files", "b_strm_files_fts"),
            ("c_ghost_files", "c_ghost_files_fts"),
        ]
        for main_table, fts_table in tables:
            main_count = conn.execute(f"SELECT COUNT(*) FROM {main_table}").fetchone()[0]
            fts_count = conn.execute(f"SELECT COUNT(*) FROM {fts_table}").fetchone()[0]
            if fts_count != main_count:
                conn.execute(f"DELETE FROM {fts_table}")
                if main_count > 0:
                    conn.execute(
                        f"INSERT INTO {fts_table}(rowid, local_path, webdav_path) "
                        f"SELECT rowid, local_path, webdav_path FROM {main_table}"
                    )
                logging.info(
                    f"[DB] 重建 {fts_table}: FTS={fts_count} → 主表={main_count}（修复孤儿/缺失）"
                )

    def upsert_a(self, local_path: str, webdav_path: str,
                 parent_webdav_path: str) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 预读现有记录
            old_row = conn.execute(
                "SELECT rowid, webdav_path, parent_webdav_path, updated_at "
                "FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

            if old_row is None:
                # 新增记录
                conn.execute(
                    """
                    INSERT INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (local_path, webdav_path, parent_webdav_path, now),
                )
                # 插入 FTS（先清理可能残留的同 rowid 孤儿行）
                new_row = conn.execute(
                    "SELECT rowid FROM a_strm_files WHERE local_path = ?", (local_path,)
                ).fetchone()
                if new_row:
                    conn.execute("DELETE FROM a_strm_files_fts WHERE rowid = ?", (new_row[0],))
                    conn.execute(
                        "INSERT INTO a_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                        (new_row[0], local_path, webdav_path),
                    )
            else:
                # 现有记录：比较业务字段
                old_rowid, old_webdav, old_parent, old_updated = old_row
                fields_changed = (
                    old_webdav != webdav_path or
                    old_parent != parent_webdav_path
                )

                if fields_changed:
                    # 字段变化：更新记录和时间戳
                    conn.execute(
                        """
                        UPDATE a_strm_files
                        SET webdav_path = ?, parent_webdav_path = ?, updated_at = ?
                        WHERE local_path = ?
                        """,
                        (webdav_path, parent_webdav_path, now, local_path),
                    )
                    # webdav_path 变化时同步 FTS
                    if old_webdav != webdav_path:
                        conn.execute("DELETE FROM a_strm_files_fts WHERE rowid = ?", (old_rowid,))
                        conn.execute(
                            "INSERT INTO a_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (old_rowid, local_path, webdav_path),
                        )
                # 字段无变化：保留原 updated_at，不操作 FTS

            conn.commit()

    def upsert_b(
        self,
        local_path: str,
        webdav_path: str,
        parent_webdav_path: str,
        source_a_path: str | None,
        mapping_id: str,
        fingerprint: str | None = None,
        status: str = "valid",
    ) -> None:
        if not mapping_id:
            raise ValueError("upsert_b: mapping_id must be a non-empty string")
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 预读现有记录
            old_row = conn.execute(
                "SELECT rowid, webdav_path, parent_webdav_path, source_a_path, "
                "fingerprint, status, mapping_id, updated_at "
                "FROM b_strm_files WHERE local_path = ?",
                (local_path,),
            ).fetchone()

            if old_row is None:
                # 新增记录
                conn.execute(
                    """
                    INSERT INTO b_strm_files(
                        local_path, webdav_path, parent_webdav_path,
                        source_a_path, fingerprint, status, updated_at, mapping_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_path,
                        webdav_path,
                        parent_webdav_path,
                        source_a_path,
                        fingerprint,
                        status,
                        now,
                        mapping_id,
                    ),
                )
                # 插入 FTS（先清理可能残留的同 rowid 孤儿行）
                new_row = conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
                ).fetchone()
                if new_row:
                    conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (new_row[0],))
                    conn.execute(
                        "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                        (new_row[0], local_path, webdav_path),
                    )
            else:
                # 现有记录：比较业务字段
                (old_rowid, old_webdav, old_parent, old_source,
                 old_fp, old_status, old_mapping, old_updated) = old_row
                fields_changed = (
                    old_webdav != webdav_path or
                    old_parent != parent_webdav_path or
                    old_source != source_a_path or
                    old_fp != fingerprint or
                    old_status != status or
                    old_mapping != mapping_id
                )

                if fields_changed:
                    # 字段变化：更新记录和时间戳
                    conn.execute(
                        """
                        UPDATE b_strm_files
                        SET webdav_path = ?, parent_webdav_path = ?,
                            source_a_path = ?, fingerprint = ?,
                            status = ?, mapping_id = ?, updated_at = ?
                        WHERE local_path = ?
                        """,
                        (
                            webdav_path,
                            parent_webdav_path,
                            source_a_path,
                            fingerprint,
                            status,
                            mapping_id,
                            now,
                            local_path,
                        ),
                    )
                    # webdav_path 变化时同步 FTS
                    if old_webdav != webdav_path:
                        conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (old_rowid,))
                        conn.execute(
                            "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (old_rowid, local_path, webdav_path),
                        )
                # 字段无变化：保留原 updated_at，不操作 FTS

            conn.commit()

    def upsert_c(
        self,
        local_path: str,
        webdav_path: str,
        original_b_path: str,
        ghost_root: str,
    ) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先获取旧 rowid（如果存在），删除旧 FTS 行（避免 REPLACE 改变 rowid 后残留孤儿）
            old_row = conn.execute(
                "SELECT rowid FROM c_ghost_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if old_row:
                conn.execute("DELETE FROM c_ghost_files_fts WHERE rowid = ?", (old_row[0],))
            conn.execute(
                """
                INSERT OR REPLACE INTO c_ghost_files(local_path, webdav_path, original_b_path, ghost_root, moved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (local_path, webdav_path, original_b_path, ghost_root, now),
            )
            # 获取新 rowid，插入新 FTS 行
            new_row = conn.execute(
                "SELECT rowid FROM c_ghost_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if new_row:
                # 先删除该 rowid 上可能残留的孤儿 FTS 行（防止 constraint failed）
                conn.execute("DELETE FROM c_ghost_files_fts WHERE rowid = ?", (new_row[0],))
                conn.execute(
                    "INSERT INTO c_ghost_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                    (new_row[0], local_path, webdav_path),
                )
            conn.commit()

    def delete_a_by_local(self, local_path: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先获取 rowid
            row = conn.execute(
                "SELECT rowid FROM a_strm_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if row:
                rowid = row[0]
                conn.execute(
                    "DELETE FROM a_strm_files WHERE local_path = ?", (local_path,)
                )
                conn.execute(
                    "DELETE FROM a_strm_files_fts WHERE rowid = ?", (rowid,)
                )
            conn.commit()

    def get_b_lineage_snapshot(self, mapping_id: str, local_path: str) -> BLineageSnapshotRecord | None:
        mapping_id = self._require_mapping_id(mapping_id)
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            row = conn.execute(
                """SELECT mapping_id, local_path, file_size, mtime_ns, fingerprint,
                          mapping_version, lineage_version, validation_state, verified_at
                   FROM b_lineage_snapshot WHERE mapping_id = ? AND local_path = ?""",
                (mapping_id, local_path),
            ).fetchone()
            return BLineageSnapshotRecord(*row) if row else None

    def upsert_b_lineage_snapshot(
            self, mapping_id: str, local_path: str, file_size: int, mtime_ns: int,
            fingerprint: str | None, mapping_version: str, lineage_version: int,
            validation_state: str, verified_at: float | None = None) -> None:
        mapping_id = self._require_mapping_id(mapping_id)
        if not mapping_version or validation_state != "valid":
            raise ValueError("only valid snapshots with mapping_version may be stored")
        now = time.time() if verified_at is None else verified_at
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """INSERT INTO b_lineage_snapshot(
                    mapping_id, local_path, file_size, mtime_ns, fingerprint,
                    mapping_version, lineage_version, validation_state, verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mapping_id, local_path) DO UPDATE SET
                    file_size=excluded.file_size, mtime_ns=excluded.mtime_ns,
                    fingerprint=excluded.fingerprint, mapping_version=excluded.mapping_version,
                    lineage_version=excluded.lineage_version,
                    validation_state=excluded.validation_state,
                    verified_at=excluded.verified_at""",
                (mapping_id, local_path, file_size, mtime_ns, fingerprint,
                 mapping_version, lineage_version, validation_state, now),
            )
            conn.commit()

    def invalidate_b_lineage_snapshots(
            self, mapping_id: str | None = None, local_path: str | None = None) -> None:
        clauses = []
        params: list[str] = []
        if mapping_id:
            clauses.append("mapping_id = ?")
            params.append(self._require_mapping_id(mapping_id))
        if local_path:
            clauses.append("local_path = ?")
            params.append(local_path)
        with self.rw_lock.write_locked(), self.connection() as conn:
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            conn.execute("DELETE FROM b_lineage_snapshot" + where, params)
            conn.commit()

    def delete_b_by_local(self, local_path: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            row = conn.execute(
                "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if row:
                rowid = row[0]
                conn.execute(
                    "DELETE FROM b_strm_files WHERE local_path = ?", (local_path,))
                conn.execute(
                    "DELETE FROM b_strm_files_fts WHERE rowid = ?", (rowid,))
            conn.commit()

    def delete_c_by_local(self, local_path: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            row = conn.execute(
                "SELECT rowid FROM c_ghost_files WHERE local_path = ?", (local_path,)
            ).fetchone()
            if row:
                rowid = row[0]
                conn.execute(
                    "DELETE FROM c_ghost_files WHERE local_path = ?", (local_path,))
                conn.execute(
                    "DELETE FROM c_ghost_files_fts WHERE rowid = ?", (rowid,))
            conn.commit()

    def get_a_by_local(self, local_path: str) -> ARecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            )
            row = cur.fetchone()
            return ARecord(*row) if row else None

    def get_b_by_local(self, local_path: str) -> BRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files WHERE local_path = ?
                """,
                (local_path,),
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def get_a_by_webdav(self, webdav_path: str) -> ARecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE webdav_path = ?",
                (webdav_path,),
            )
            row = cur.fetchone()
            return ARecord(*row) if row else None

    def get_b_by_webdav(self, webdav_path: str) -> list[BRecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files WHERE webdav_path = ?
                """,
                (webdav_path,),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_a_records(self) -> list[ARecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files")
            return [ARecord(*row) for row in cur.fetchall()]

    def get_all_b(self) -> list[BRecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute("""
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files
                """)
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_b_fingerprints(self, mapping_id: str) -> set[str]:
        """获取指定 mapping 下的所有非空指纹。"""
        mapping_id = self._require_mapping_id(mapping_id)
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT DISTINCT fingerprint FROM b_strm_files WHERE fingerprint IS NOT NULL AND mapping_id = ?",
                (mapping_id,),
            )
            return {row[0] for row in cur.fetchall()}

    def get_all_c(self) -> list[CRecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute("""
                SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at
                FROM c_ghost_files
                """)
            return [CRecord(*row) for row in cur.fetchall()]

    def save_known_folder(self, folder_path: str,
                          source: str = "unknown") -> None:
        if not folder_path or folder_path == "/":
            return
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at)
                VALUES (?, ?, ?)
                """,
                (folder_path, source, now),
            )
            conn.commit()

    def save_known_folders_batch(self, folder_paths: list[str],
                                 source: str = "unknown") -> int:
        if not folder_paths:
            return 0
        now = time.time()
        data = [(fp, source, now) for fp in folder_paths if fp and fp != "/"]
        if not data:
            return 0
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at) VALUES (?, ?, ?)",
                data)
            conn.commit()
            return len(data)

    def get_known_folders(self) -> list[str]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute("SELECT folder_path FROM known_folders")
            return [row[0] for row in cur.fetchall()]

    def remove_known_folder_prefix(self, folder_path: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                "DELETE FROM known_folders WHERE folder_path = ? OR folder_path LIKE ? ESCAPE '\\'",
                (folder_path, escape_like(folder_path.rstrip("/")) + "/%"),
            )
            conn.commit()

    def set_ghost_protection(self, webdav_path: str,
                             seconds: int, reason: str = "") -> None:
        expire = time.time() + seconds
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ghost_protection(webdav_path, expire_time, reason)
                VALUES (?, ?, ?)
                """,
                (webdav_path, expire, reason),
            )
            conn.commit()

    def cleanup_expired_ghosts(self) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                "DELETE FROM ghost_protection WHERE expire_time <= ?", (now,))
            conn.commit()

    def is_ghost_protected(self, webdav_path: str) -> bool:
        """检查路径是否受幽灵保护。

        优化：每 60 秒最多执行一次过期清理，避免热路径中的频繁 DELETE 查询。
        """
        now = time.time()
        # 每 60 秒执行一次过期清理（使用专用锁保护，避免多线程竞态）
        with self._ghost_cleanup_lock:
            if now - self._last_ghost_cleanup > 60:
                self.cleanup_expired_ghosts()
                self._last_ghost_cleanup = now

        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT expire_time FROM ghost_protection WHERE webdav_path = ?",
                (webdav_path,),
            )
            row = cur.fetchone()
            return bool(row and row[0] > now)

    def get_all_ghost_protected_paths(self) -> set[str]:
        now = time.time()
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT webdav_path FROM ghost_protection WHERE expire_time > ?", (now,))
            return {row[0] for row in cur.fetchall()}

    def set_protected_root(self, root_path: str,
                           trash_path: str, active: bool = True) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO protected_roots(root_path, trash_path, active, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (root_path, trash_path, 1 if active else 0, now),
            )
            conn.commit()

    def replace_protected_roots(self, roots: list[tuple[str, str]]) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute("DELETE FROM protected_roots")
            conn.executemany(
                """
                INSERT INTO protected_roots(root_path, trash_path, active, updated_at)
                VALUES (?, ?, 1, ?)
                """,
                [(root_path, trash_path, now)
                 for root_path, trash_path in roots],
            )
            conn.commit()

    def get_protected_roots(self) -> list[ProtectedRootRecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT root_path, trash_path, active, updated_at FROM protected_roots")
            return [ProtectedRootRecord(r[0], r[1], bool(r[2]), r[3]) for r in cur.fetchall()]

    def get_protected_root_paths(self) -> list[str]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute("SELECT root_path FROM protected_roots")
            return [row[0] for row in cur.fetchall()]

    def save_protected_roots_snapshot(
            self, roots: list[tuple[str, str]]) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute("DELETE FROM protected_roots_snapshot")
            conn.executemany(
                """
                INSERT INTO protected_roots_snapshot(root_path, trash_path, updated_at)
                VALUES (?, ?, ?)
                """,
                [(root_path, trash_path, now)
                 for root_path, trash_path in roots],
            )
            conn.commit()

    def get_protected_roots_snapshot_paths(self) -> list[str]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT root_path FROM protected_roots_snapshot")
            return [row[0] for row in cur.fetchall()]

    def set_control(self, key: str, value: str) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )
            conn.commit()

    def get_control(self, key: str, default: str = "") -> str:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT control_value FROM sync_control WHERE control_key = ?",
                (key,),
            )
            row = cur.fetchone()
            return row[0] if row else default

    def _set_controls_conn(self, conn, items: list[tuple[str, str]]) -> None:
        """连接级内部写入辅助，供单事务多键写入（不提交事务）。"""
        now = time.time()
        for key, value in items:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )

    def complete_index_generation(
        self,
        mapping_ids: list[str],
        completed_at: float | None = None,
    ) -> dict:
        """
        一次成功完成全量索引后调用，递增全局 generation 并记录时间戳。
        
        所有控制键写入在同一事务中完成。mapping_ids 必须去重、非空。
        """
        if not mapping_ids:
            raise ValueError("mapping_ids must be non-empty")
        
        # 去重并过滤空字符串
        unique_ids = list({mid.strip() for mid in mapping_ids if mid.strip()})
        if not unique_ids:
            raise ValueError("mapping_ids must contain at least one non-empty string")
        
        now = completed_at or time.time()
        
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 读取当前 generation
            cur = conn.execute(
                "SELECT control_value FROM sync_control WHERE control_key = ?",
                ("index_generation",),
            )
            row = cur.fetchone()
            current_gen = int(row[0]) if row else 0
            new_gen = current_gen + 1
            
            # 批量写入所有控制键
            controls = [
                ("index_generation", str(new_gen)),
                ("index_generation_at", str(now)),
                ("last_full_index_at", str(now)),
            ]
            
            # 为每个 mapping 写入独立的 generation 和时间戳
            for mapping_id in unique_ids:
                controls.append((f"index_generation:{mapping_id}", str(new_gen)))
                controls.append((f"index_generation_at:{mapping_id}", str(now)))
            
            self._set_controls_conn(conn, controls)
            conn.commit()
        
        return {
            "index_generation": new_gen,
            "index_generation_at": now,
            "last_full_index_at": now,
            "mapping_ids_completed": unique_ids,
        }

    def get_index_metadata(self, mapping_id: str | None = None) -> dict:
        """
        获取索引元数据。
        
        不传 mapping_id 时返回全局元数据；传 mapping_id 时附加该 mapping 的元数据。
        缺失的键返回默认值（generation=0, time=0），不伪造历史。
        """
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            # 全局元数据
            def _get_control(key: str, default: str = "") -> str:
                cur = conn.execute(
                    "SELECT control_value FROM sync_control WHERE control_key = ?",
                    (key,),
                )
                row = cur.fetchone()
                return row[0] if row else default
            
            gen = int(_get_control("index_generation", "0"))
            gen_at = float(_get_control("index_generation_at", "0"))
            last_full = float(_get_control("last_full_index_at", "0"))
            mv = _get_control("mapping_version", "")
            mv_at = float(_get_control("mapping_version_generated_at", "0"))
            
            result = {
                "index_generation": gen,
                "index_generation_at": gen_at,
                "last_full_index_at": last_full,
                "mapping_version": mv,
                "mapping_version_generated_at": mv_at,
            }
            
            # 附加指定 mapping 的元数据
            if mapping_id is not None and mapping_id.strip():
                mapping_gen = int(_get_control(f"index_generation:{mapping_id}", "0"))
                mapping_gen_at = float(_get_control(f"index_generation_at:{mapping_id}", "0"))
                result["mapping_id"] = mapping_id
                result["mapping_index_generation"] = mapping_gen
                result["mapping_index_generation_at"] = mapping_gen_at
            else:
                # 空/None mapping_id，返回未知
                result["mapping_id"] = mapping_id or ""
                result["mapping_index_generation"] = 0
                result["mapping_index_generation_at"] = 0
            
            return result

    def set_mapping_version(
        self,
        version: str,
        version_generated_at: float | None = None,
    ) -> None:
        """
        设置 mapping 版本摘要。
        
        仅当版本变化时更新时间戳，避免将首次观察时间冒充历史生成时间。
        """
        now = version_generated_at or time.time()
        
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 读取当前版本
            cur = conn.execute(
                "SELECT control_value FROM sync_control WHERE control_key = ?",
                ("mapping_version",),
            )
            row = cur.fetchone()
            current_version = row[0] if row else ""
            
            # 仅当版本变化时更新
            if version != current_version:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    ("mapping_version", version, now),
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    ("mapping_version_generated_at", str(now), now),
                )
                conn.commit()

    def get_b_under_root(self, webdav_root: str) -> list[BRecord]:
        pattern = escape_like(webdav_root.rstrip("/")) + "/%"
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files
                WHERE webdav_path LIKE ? ESCAPE '\\'
                """,
                (pattern,),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def delete_b_under_root(self, webdav_root: str) -> None:
        pattern = escape_like(webdav_root.rstrip("/")) + "/%"
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先收集待删 rowid，用于同步清理 FTS 行（防止孤儿）
            rowids = [
                r[0] for r in conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE webdav_path = ? OR webdav_path LIKE ? ESCAPE '\\'",
                    (webdav_root, pattern),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM b_strm_files WHERE webdav_path = ? OR webdav_path LIKE ? ESCAPE '\\'",
                (webdav_root, pattern),
            )
            for rid in rowids:
                conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (rid,))
            conn.commit()

    def upsert_identity(
        self,
        fingerprint: str,
        webdav_path: str,
        source_a_path: str | None,
        current_b_path: str | None,
    ) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strm_identity(
                    fingerprint,
                    webdav_path,
                    source_a_path,
                    current_b_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, webdav_path, source_a_path, current_b_path, now),
            )
            conn.commit()

    def get_identity_by_fingerprint(self, fingerprint: str) -> IdentityRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, webdav_path, source_a_path, current_b_path, updated_at
                FROM strm_identity
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            row = cur.fetchone()
            return IdentityRecord(*row) if row else None

    def get_identity_by_webdav(self, webdav_path: str) -> IdentityRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, webdav_path, source_a_path, current_b_path, updated_at
                FROM strm_identity
                WHERE webdav_path = ?
                """,
                (webdav_path,),
            )
            row = cur.fetchone()
            return IdentityRecord(*row) if row else None

    def update_identity_b_path(self, fingerprint: str,
                               current_b_path: str | None) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                UPDATE strm_identity
                SET current_b_path = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (current_b_path, now, fingerprint),
            )
            conn.commit()

    def update_identity_a_path(self, fingerprint: str,
                               source_a_path: str | None) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                UPDATE strm_identity
                SET source_a_path = ?, updated_at = ?
                WHERE fingerprint = ?
                """,
                (source_a_path, now, fingerprint),
            )
            conn.commit()

    def delete_identity_by_fingerprint(self, fingerprint: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                "DELETE FROM strm_identity WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.commit()

    # ===== b_identity_projection 映射级身份投影表 =====

    def upsert_identity_projection(
        self,
        fingerprint: str,
        mapping_id: str,
        current_b_path: str | None,
        status: str = "valid",
    ) -> None:
        """更新映射级身份投影记录"""
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO b_identity_projection(
                    fingerprint, mapping_id, current_b_path, status, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, mapping_id, current_b_path, status, now),
            )
            conn.commit()

    def get_identity_projection(
        self, fingerprint: str, mapping_id: str
    ) -> tuple[str | None, str] | None:
        """获取映射级身份投影记录，返回 (current_b_path, status)"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT current_b_path, status
                FROM b_identity_projection
                WHERE fingerprint = ? AND mapping_id = ?
                """,
                (fingerprint, mapping_id),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None

    def get_all_identity_projections_for_fingerprint(
        self, fingerprint: str
    ) -> list[tuple[str, str | None, str]]:
        """获取指定 fingerprint 在所有映射下的投影记录，返回 [(mapping_id, current_b_path, status), ...]"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT mapping_id, current_b_path, status
                FROM b_identity_projection
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            return [(row[0], row[1], row[2]) for row in cur.fetchall()]

    def delete_identity_projection(self, fingerprint: str, mapping_id: str) -> None:
        """删除映射级身份投影记录"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                "DELETE FROM b_identity_projection WHERE fingerprint = ? AND mapping_id = ?",
                (fingerprint, mapping_id),
            )
            conn.commit()

    def move_b_record(self, old_local_path: str, new_local_path: str) -> bool:
        """
        B 区文件被重命名/隔离后，把 b_strm_files 的 local_path
        从旧路径迁移到新路径，并保留 fingerprint/status。

        使用 INSERT OR REPLACE 实现原子操作，避免先 DELETE 再 INSERT
        时中间失败导致的数据丢失。
        """
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 显式开启事务（B-8）：确保 conflict 检测与 INSERT/DELETE 在同一原子事务内，
            # 避免 SELECT 与写入之间被其它写连接插入目标行（TOCTOU）。
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    SELECT webdav_path,
                           parent_webdav_path,
                           source_a_path,
                           fingerprint,
                           mapping_id,
                           status
                    FROM b_strm_files
                    WHERE local_path = ?
                    """,
                    (old_local_path,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False

                webdav_path, parent_webdav_path, source_a_path, fingerprint, mapping_id, status = row
                now = time.time()
                new_status = status or "valid"

                # 记录旧行 rowid，用于稍后同步清理 FTS 行（防止孤儿）
                old_rowid_row = conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE local_path = ?",
                    (old_local_path,),
                ).fetchone()
                old_rowid = old_rowid_row[0] if old_rowid_row else None

                # 检查新路径是否已被其他 fingerprint 占用 (P2-6)
                conflict = conn.execute(
                    "SELECT fingerprint FROM b_strm_files WHERE local_path = ?",
                    (new_local_path,),
                ).fetchone()
                if conflict and conflict[0] != fingerprint:
                    conn.rollback()
                    logging.warning(
                        "[DB] move_b_record 目标路径已被其他记录占用: %s (旧指纹=%s, 新指纹=%s)",
                        new_local_path, conflict[0], fingerprint)
                    return False

                # 使用 INSERT OR REPLACE 实现原子替换
                conn.execute(
                    """
                    INSERT OR REPLACE INTO b_strm_files(
                        local_path,
                        webdav_path,
                        parent_webdav_path,
                        source_a_path,
                        fingerprint,
                        status,
                        updated_at,
                        mapping_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_local_path,
                        webdav_path,
                        parent_webdav_path,
                        source_a_path,
                        fingerprint,
                        new_status,
                        now,
                        mapping_id,
                    ),
                )
                # 删除旧记录
                conn.execute(
                    "DELETE FROM b_strm_files WHERE local_path = ?",
                    (old_local_path,),
                )
                # 同步 FTS：清理旧行，重建新行（防止孤儿 / rowid 复用冲突）
                if old_rowid is not None:
                    conn.execute(
                        "DELETE FROM b_strm_files_fts WHERE rowid = ?", (old_rowid,))
                new_rowid_row = conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE local_path = ?",
                    (new_local_path,),
                ).fetchone()
                if new_rowid_row:
                    conn.execute(
                        "DELETE FROM b_strm_files_fts WHERE rowid = ?", (new_rowid_row[0],))
                    conn.execute(
                        "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                        (new_rowid_row[0], new_local_path, webdav_path),
                    )
                conn.commit()
                return True
            except sqlite3.Error as e:
                conn.rollback()
                logging.error("[DB] move_b_record 失败: %s", e)
                return False

    def delete_identity_by_b_path(self, current_b_path: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                UPDATE strm_identity
                SET current_b_path = NULL, updated_at = ?
                WHERE current_b_path = ?
                """,
                (time.time(), current_b_path),
            )
            conn.commit()

    def get_a_local_path_by_webdav(self, webdav_path: str) -> str | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path
                FROM a_strm_files
                WHERE webdav_path = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (webdav_path,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_b_instances_by_fingerprint(self, fingerprint: str, mapping_id: str) -> list[BRecord]:
        """返回指定 mapping 下该 fingerprint 的所有 B 实例。"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path,
                       webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status,
                       updated_at,
                       mapping_id
                FROM b_strm_files
                WHERE fingerprint = ? AND mapping_id = ?
                """,
                (fingerprint, mapping_id),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def mark_b_instance_status(self, local_path: str, status: str) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                UPDATE b_strm_files
                SET status = ?, updated_at = ?
                WHERE local_path = ?
                """,
                (status, now, local_path),
            )
            conn.commit()

    def delete_b_by_fingerprint(self, fingerprint: str) -> None:
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先收集待删 rowid，用于同步清理 FTS 行（防止孤儿）
            rowids = [
                r[0] for r in conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE fingerprint = ?",
                    (fingerprint,),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM b_strm_files WHERE fingerprint = ?",
                (fingerprint,),
            )
            for rid in rowids:
                conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (rid,))
            conn.commit()

    def get_b_by_local_full(self, local_path: str) -> BRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path,
                       webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status,
                       updated_at,
                       mapping_id
                FROM b_strm_files
                WHERE local_path = ?
                """,
                (local_path,),
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def clear_identity_b_path_by_fingerprint(self, fingerprint: str) -> None:
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                UPDATE strm_identity
                SET current_b_path = NULL,
                    updated_at = ?
                WHERE fingerprint = ?
                """,
                (now, fingerprint),
            )
            conn.commit()

    @staticmethod
    def _require_mapping_id(mapping_id: str) -> str:
        if not isinstance(mapping_id, str) or not mapping_id.strip():
            raise ValueError("mapping_id must be a non-empty string")
        return mapping_id.strip()

    @classmethod
    def _b_fp_where(cls, fingerprint: str, mapping_id: str) -> tuple[str, tuple]:
        """构建按 fingerprint + mapping_id 过滤的 WHERE 子句和参数元组。

        返回 (where_clause, params)：
        - ("fingerprint = ? AND mapping_id = ?", (fingerprint, mapping_id))

        用于统一 4 个查询方法(get_valid_b_instance_by_fingerprint、
        mark_other_b_instances_duplicate、get_all_b_by_fingerprint、
        b_fingerprint_exists)的过滤逻辑,避免重复代码漂移。
        """
        mapping_id = cls._require_mapping_id(mapping_id)
        return "fingerprint = ? AND mapping_id = ?", (fingerprint, mapping_id)

    def get_valid_b_instance_by_fingerprint(
            self, fingerprint: str, mapping_id: str) -> BRecord | None:
        """
        根据指纹获取指定 mapping 下的有效 B 实例。
        """
        where_fp, params = self._b_fp_where(fingerprint, mapping_id)
        where_parts = [where_fp, "status = 'valid'"]

        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                f"""
                SELECT local_path, webdav_path, parent_webdav_path,
                       source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files
                WHERE {' AND '.join(where_parts)}
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                params,
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def mark_other_b_instances_duplicate(
        self,
        fingerprint: str,
        keep_local_path: str,
        mapping_id: str,
    ) -> list[str]:
        """
        将同 fingerprint + mapping_id 下除 keep_local_path 外的 valid 实例标记为 duplicate。
        返回被标记的 local_path 列表。
        """
        now = time.time()
        where_fp, fp_params = self._b_fp_where(fingerprint, mapping_id)

        with self.rw_lock.write_locked(), self.connection() as conn:
            select_sql = f"""
                SELECT local_path
                FROM b_strm_files
                WHERE {where_fp}
                  AND local_path != ?
                  AND status = 'valid'
            """
            select_params = fp_params + (keep_local_path,)
            cur = conn.execute(select_sql, select_params)
            rows = [row[0] for row in cur.fetchall()]

            update_sql = f"""
                UPDATE b_strm_files
                SET status = 'duplicate',
                    updated_at = ?
                WHERE {where_fp}
                  AND local_path != ?
                  AND status = 'valid'
            """
            update_params = (now,) + fp_params + (keep_local_path,)
            conn.execute(update_sql, update_params)
            conn.commit()

            return rows

    def get_all_b_by_fingerprint(self, fingerprint: str, mapping_id: str) -> list[BRecord]:
        """
        返回指定 mapping 下该 fingerprint 的所有 B 实例。
        """
        where, params = self._b_fp_where(fingerprint, mapping_id)

        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                f"""
                SELECT local_path, webdav_path, parent_webdav_path,
                       source_a_path, fingerprint, status, updated_at, mapping_id
                FROM b_strm_files
                WHERE {where}
                """,
                params,
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_b_records(self) -> list[BRecord]:
        """获取所有 B 区记录(用于启动时对比)"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute("""
                    SELECT local_path,
                           webdav_path,
                           parent_webdav_path,
                           source_a_path,
                           fingerprint,
                           status,
                           updated_at,
                           mapping_id
                    FROM b_strm_files
                """)
            return [BRecord(*row) for row in cur.fetchall()]

    def b_fingerprint_exists(self, fingerprint: str, mapping_id: str) -> bool:
        """检查指定 mapping 下 B 区数据库中是否已存在该指纹。"""
        where, params = self._b_fp_where(fingerprint, mapping_id)

        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                f"SELECT 1 FROM b_strm_files WHERE {where} LIMIT 1", params)
            return cur.fetchone() is not None

    def update_b_local_path(self, old_path: str, new_path: str) -> bool:
        """更新 B 区文件的本地路径（用于文件名改变的情况）"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            cur = conn.execute(
                "UPDATE b_strm_files SET local_path = ? WHERE local_path = ?",
                (new_path,
                 old_path))
            conn.commit()
            return cur.rowcount > 0

    def get_a_count_under_root(self, cloud_media_root: str) -> int:
        """统计 A 区某个剧集根路径下共有多少集"""
        pattern = escape_like(cloud_media_root.rstrip('/')) + '/%'
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM a_strm_files WHERE webdav_path LIKE ? ESCAPE '\\'",
                (pattern,)
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def has_other_b_instance(
        self, mapping_id: str, fingerprint: str, exclude_local_path: str
    ) -> bool:
        """检查是否存在同一 mapping + fingerprint 的其他 B 区实例（排除指定路径）。

        Args:
            mapping_id: 映射ID（必填，禁止全局查询）
            fingerprint: 要检查的指纹
            exclude_local_path: 排除的本地路径
        """
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM b_strm_files WHERE fingerprint = ? AND local_path != ? AND mapping_id = ? LIMIT 1",
                (fingerprint, exclude_local_path, mapping_id),
            )
            return cur.fetchone() is not None

    def upsert_media_boundary(
        self,
        mapping_id: str,
        fingerprint: str,
        source_media_name: str,
        current_media_name: str,
        engine_entry_path: str,
    ) -> None:
        if not mapping_id:
            raise ValueError("upsert_media_boundary: mapping_id must be a non-empty string")
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strm_media_boundary(
                    mapping_id,
                    fingerprint,
                    source_media_name,
                    current_media_name,
                    engine_entry_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mapping_id, fingerprint, source_media_name,
                 current_media_name, engine_entry_path, now),
            )
            conn.commit()

    def get_media_boundary_by_fingerprint(
            self, mapping_id: str, fingerprint: str) -> BoundaryRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT mapping_id, fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE mapping_id = ? AND fingerprint = ?
                """,
                (mapping_id, fingerprint),
            )
            row = cur.fetchone()
            return BoundaryRecord(*row) if row else None

    def get_media_boundaries_by_source_name(
        self, mapping_id: str, source_media_name: str, engine_entry_path: str
    ) -> list[BoundaryRecord]:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT mapping_id, fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE mapping_id = ? AND source_media_name = ? AND engine_entry_path = ?
                """,
                (mapping_id, source_media_name, engine_entry_path),
            )
            return [BoundaryRecord(*row) for row in cur.fetchall()]

    def get_media_boundary_by_current_name(
        self, mapping_id: str, current_media_name: str, engine_entry_path: str
    ) -> BoundaryRecord | None:
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT mapping_id, fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE mapping_id = ? AND current_media_name = ? AND engine_entry_path = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (mapping_id, current_media_name, engine_entry_path),
            )
            row = cur.fetchone()
            return BoundaryRecord(*row) if row else None

    def get_media_boundary_by_source_name_only(
        self, mapping_id: str, source_media_name: str
    ) -> BoundaryRecord | None:
        """根据源媒体名查找边界映射（不限制引擎路径，取最新的）"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                """
                SELECT mapping_id, fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE mapping_id = ? AND source_media_name = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (mapping_id, source_media_name),
            )
            row = cur.fetchone()
            return BoundaryRecord(*row) if row else None

    def _ensure_db_writable(self) -> None:
        """确保数据库文件及其父目录可写。"""
        db_path = Path(self.db_path)

        # 确保父目录存在且可写
        parent_dir = db_path.parent
        if not parent_dir.exists():
            parent_dir.mkdir(parents=True, exist_ok=True)

        # 检查父目录权限
        if not os.access(str(parent_dir), os.W_OK):
            logging.warning("[DB] 数据库目录不可写，尝试修复权限: %s", parent_dir)
            try:
                os.chmod(str(parent_dir), 0o755)
            except Exception as e:
                logging.error("[DB] 无法修复数据库目录权限: %s", e)

        # 如果数据库文件已存在，检查其权限
        if db_path.exists():
            if not os.access(str(db_path), os.W_OK):
                logging.warning("[DB] 数据库文件不可写，尝试修复权限: %s", db_path)
                try:
                    os.chmod(str(db_path), 0o644)
                except Exception as e:
                    logging.error("[DB] 无法修复数据库文件权限: %s", e)
        else:
            # ===== 修复：数据库文件不存在时，创建空文件并立即应用
            # WAL / busy_timeout 等 PRAGMA，避免裸文件引发后续锁竞争 =====
            try:
                temp_conn = sqlite3.connect(str(db_path), timeout=30)
                try:
                    self._apply_pragmas(temp_conn)
                    temp_conn.commit()
                finally:
                    temp_conn.close()
                logging.info("[DB] 已创建数据库文件: %s", db_path)
            except Exception as e:
                logging.error("[DB] 无法创建数据库文件: %s", e)
            # ============================================================

    # ========== 字幕表操作 ==========

    def init_subtitle_table(self) -> None:
        """初始化字幕表"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subtitles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_path TEXT NOT NULL UNIQUE,
                    target_path TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    season INTEGER,
                    episode INTEGER,
                    lang_code TEXT,
                    status TEXT DEFAULT 'valid',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_subtitle_fingerprint
                ON subtitles(fingerprint)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_subtitle_target
                ON subtitles(target_path)
            """)
            conn.commit()

    def upsert_subtitle(
        self,
        local_path: str,
        target_path: str,
        fingerprint: str,
        season: int | None = None,
        episode: int | None = None,
        lang_code: str | None = None,
        status: str = "valid",
    ) -> None:
        """插入或更新字幕记录"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute("""
                INSERT INTO subtitles
                (local_path, target_path, fingerprint, season, episode, lang_code, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_path) DO UPDATE SET
                    target_path = excluded.target_path,
                    fingerprint = excluded.fingerprint,
                    season = excluded.season,
                    episode = excluded.episode,
                    lang_code = excluded.lang_code,
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP
            """, (local_path, target_path, fingerprint, season, episode, lang_code, status))
            conn.commit()

    def get_subtitle_by_local(self, local_path: str) -> SubtitleRecord | None:
        """根据本地路径查询字幕记录"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT id, local_path, target_path, fingerprint, season, episode, lang_code, status, created_at, updated_at FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            row = cur.fetchone()
            return SubtitleRecord(*row) if row else None

    def subtitle_exists(self, local_path: str) -> bool:
        """检查字幕是否已存在"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            return cur.fetchone() is not None

    def get_subtitles_by_fingerprint(self, fingerprint: str) -> list[SubtitleRecord]:
        """根据指纹获取所有字幕"""
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT id, local_path, target_path, fingerprint, season, episode, lang_code, status, created_at, updated_at FROM subtitles WHERE fingerprint = ?",
                (fingerprint,)
            )
            return [SubtitleRecord(*row) for row in cur.fetchall()]

    def delete_subtitle_by_local(self, local_path: str) -> None:
        """删除字幕记录"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(
                "DELETE FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            conn.commit()

    def cleanup_invalid_subtitles(self) -> None:
        """清理目标文件已不存在的字幕记录"""
        with self.rw_lock.write_locked(), self.connection() as conn:
            cur = conn.execute("SELECT local_path, target_path FROM subtitles")
            for local_path, target_path in cur.fetchall():
                if not Path(target_path).exists():
                    conn.execute(
                        "DELETE FROM subtitles WHERE local_path = ?",
                        (local_path,)
                    )
            conn.commit()

    # ========== 批量操作（30000+ 条数据性能优化）==========

    def upsert_a_batch(self, records: list[tuple[str, str, str]]) -> int:
        """
        批量 upsert A 区记录。
        records: [(local_path, webdav_path, parent_webdav_path), ...]
        返回成功插入/更新的行数。
        """
        if not records:
            return 0
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 预读现有记录
            local_paths = [r[0] for r in records]
            placeholders = ','.join('?' * len(local_paths))
            existing_rows = conn.execute(
                f"SELECT local_path, webdav_path, parent_webdav_path, updated_at "
                f"FROM a_strm_files WHERE local_path IN ({placeholders})",
                local_paths,
            ).fetchall()
            existing_map = {row[0]: (row[1], row[2], row[3]) for row in existing_rows}

            # 分类：新增 vs 更新
            to_insert = []
            to_update = []
            for local_path, webdav_path, parent_webdav_path in records:
                if local_path not in existing_map:
                    # 新增
                    to_insert.append((local_path, webdav_path, parent_webdav_path, now))
                else:
                    # 现有记录：比较业务字段
                    old_webdav, old_parent, old_updated = existing_map[local_path]
                    if old_webdav != webdav_path or old_parent != parent_webdav_path:
                        # 字段变化
                        to_update.append((webdav_path, parent_webdav_path, now, local_path,
                                         old_webdav, webdav_path))

            # 执行 INSERT
            if to_insert:
                conn.executemany(
                    """
                    INSERT INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    to_insert,
                )

            # 执行 UPDATE
            for update_data in to_update:
                webdav_path, parent_webdav_path, now, local_path, old_webdav, new_webdav = update_data
                conn.execute(
                    """
                    UPDATE a_strm_files
                    SET webdav_path = ?, parent_webdav_path = ?, updated_at = ?
                    WHERE local_path = ?
                    """,
                    (webdav_path, parent_webdav_path, now, local_path),
                )
                # webdav_path 变化时同步 FTS
                if old_webdav != new_webdav:
                    row = conn.execute(
                        "SELECT rowid FROM a_strm_files WHERE local_path = ?", (local_path,)
                    ).fetchone()
                    if row:
                        conn.execute("DELETE FROM a_strm_files_fts WHERE rowid = ?", (row[0],))
                        conn.execute(
                            "INSERT INTO a_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (row[0], local_path, new_webdav),
                        )

            # 为新增记录插入 FTS
            if to_insert:
                for local_path, webdav_path, _, _ in to_insert:
                    row = conn.execute(
                        "SELECT rowid FROM a_strm_files WHERE local_path = ?", (local_path,)
                    ).fetchone()
                    if row:
                        conn.execute(
                            "INSERT INTO a_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (row[0], local_path, webdav_path),
                        )

            conn.commit()
            return len(records)

    def rebuild_fts_table(self, main_table: str, fts_table: str) -> None:
        """一次性重建 FTS 表（用于批量操作后）。

        性能优化：
        - 清空 FTS 表
        - 从主表批量插入所有记录
        - 使用单个事务

        Args:
            main_table: 主表名（如 "a_strm_files"）
            fts_table: FTS 表名（如 "a_strm_files_fts"）
        """
        with self.rw_lock.write_locked(), self.connection() as conn:
            conn.execute(f"DELETE FROM {fts_table}")
            conn.execute(
                f"INSERT INTO {fts_table}(rowid, local_path, webdav_path) "
                f"SELECT rowid, local_path, webdav_path FROM {main_table}"
            )
            conn.commit()
            logging.info("[DB] 重建 %s 完成", fts_table)

    def upsert_b_batch(self, records: list[tuple]) -> int:
        """
        批量 upsert B 区记录。
        records: [(local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, mapping_id, status), ...]
        返回成功行数。
        """
        if not records:
            return 0
        now = time.time()
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 预读现有记录
            local_paths = [r[0] for r in records]
            placeholders = ','.join('?' * len(local_paths))
            existing_rows = conn.execute(
                f"SELECT local_path, webdav_path, parent_webdav_path, source_a_path, "
                f"fingerprint, status, mapping_id, updated_at "
                f"FROM b_strm_files WHERE local_path IN ({placeholders})",
                local_paths,
            ).fetchall()
            existing_map = {row[0]: (row[1], row[2], row[3], row[4], row[5], row[6], row[7])
                           for row in existing_rows}

            # 分类：新增 vs 更新
            to_insert = []
            to_update = []
            for record in records:
                if len(record) != 7:
                    raise ValueError(f"upsert_b_batch: unexpected record length {len(record)}, expected 7 (local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, mapping_id, status)")
                local_path, webdav_path, parent, source, fp, mapping_id_val, status = record
                if not mapping_id_val:
                    raise ValueError("upsert_b_batch: mapping_id must be a non-empty string in each record")

                if local_path not in existing_map:
                    # 新增
                    to_insert.append((local_path, webdav_path, parent, source, fp, status, now, mapping_id_val))
                else:
                    # 现有记录：比较业务字段（不包括 updated_at）
                    old_webdav, old_parent, old_source, old_fp, old_status, old_mapping, old_updated = existing_map[local_path]
                    if (old_webdav != webdav_path or old_parent != parent or
                        old_source != source or old_fp != fp or
                        old_status != status or old_mapping != mapping_id_val):
                        # 字段变化
                        to_update.append((webdav_path, parent, source, fp, status, mapping_id_val, now, local_path,
                                         old_webdav, webdav_path))

            # 执行 INSERT
            if to_insert:
                conn.executemany(
                    """
                    INSERT INTO b_strm_files(
                        local_path, webdav_path, parent_webdav_path,
                        source_a_path, fingerprint, status, updated_at, mapping_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    to_insert,
                )

            # 执行 UPDATE
            for update_data in to_update:
                webdav_path, parent, source, fp, status, mapping_id_val, now, local_path, old_webdav, new_webdav = update_data
                conn.execute(
                    """
                    UPDATE b_strm_files
                    SET webdav_path = ?, parent_webdav_path = ?,
                        source_a_path = ?, fingerprint = ?,
                        status = ?, mapping_id = ?, updated_at = ?
                    WHERE local_path = ?
                    """,
                    (webdav_path, parent, source, fp, status, mapping_id_val, now, local_path),
                )
                # webdav_path 变化时同步 FTS
                if old_webdav != new_webdav:
                    row = conn.execute(
                        "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
                    ).fetchone()
                    if row:
                        conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (row[0],))
                        conn.execute(
                            "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (row[0], local_path, new_webdav),
                        )

            # 为新增记录插入 FTS
            if to_insert:
                for local_path, webdav_path, _, _, _, _, _, _ in to_insert:
                    row = conn.execute(
                        "SELECT rowid FROM b_strm_files WHERE local_path = ?", (local_path,)
                    ).fetchone()
                    if row:
                        conn.execute(
                            "INSERT INTO b_strm_files_fts(rowid, local_path, webdav_path) VALUES(?,?,?)",
                            (row[0], local_path, webdav_path),
                        )

            conn.commit()
            return len(records)

    def delete_a_batch(self, local_paths: list[str]) -> int:
        """批量删除 A 区记录"""
        if not local_paths:
            return 0
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先收集待删 rowid，用于同步清理 FTS 行（防止孤儿）
            rowids: list[int] = []
            for p in local_paths:
                row = conn.execute(
                    "SELECT rowid FROM a_strm_files WHERE local_path = ?", (p,)
                ).fetchone()
                if row:
                    rowids.append(row[0])
            conn.executemany(
                "DELETE FROM a_strm_files WHERE local_path = ?",
                [(p,) for p in local_paths],
            )
            for rid in rowids:
                conn.execute("DELETE FROM a_strm_files_fts WHERE rowid = ?", (rid,))
            conn.commit()
            return len(local_paths)

    def delete_b_batch(self, local_paths: list[str]) -> int:
        """批量删除 B 区记录"""
        if not local_paths:
            return 0
        with self.rw_lock.write_locked(), self.connection() as conn:
            # 先收集待删 rowid，用于同步清理 FTS 行（防止孤儿）
            rowids: list[int] = []
            for p in local_paths:
                row = conn.execute(
                    "SELECT rowid FROM b_strm_files WHERE local_path = ?", (p,)
                ).fetchone()
                if row:
                    rowids.append(row[0])
            conn.executemany(
                "DELETE FROM b_strm_files WHERE local_path = ?",
                [(p,) for p in local_paths],
            )
            for rid in rowids:
                conn.execute("DELETE FROM b_strm_files_fts WHERE rowid = ?", (rid,))
            conn.commit()
            return len(local_paths)

    # ========== 统计方法（WebUI 仪表盘用）==========

    def get_table_counts(self) -> dict[str, int]:
        """获取各表记录数，使用单次查询优化性能。

        使用 UNION ALL 将多个 COUNT 查询合并为单次查询，
        减少数据库连接开销和查询次数。
        """
        sql = """
            SELECT 'a_strm_files' as table_name, COUNT(*) as count FROM a_strm_files
            UNION ALL
            SELECT 'b_strm_files', COUNT(*) FROM b_strm_files
            UNION ALL
            SELECT 'c_ghost_files', COUNT(*) FROM c_ghost_files
            UNION ALL
            SELECT 'strm_identity', COUNT(*) FROM strm_identity
            UNION ALL
            SELECT 'ghost_protection', COUNT(*) FROM ghost_protection
            UNION ALL
            SELECT 'known_folders', COUNT(*) FROM known_folders
            UNION ALL
            SELECT 'subtitles', COUNT(*) FROM subtitles
            UNION ALL
            SELECT 'strm_media_boundary', COUNT(*) FROM strm_media_boundary
        """
        result = {}
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            try:
                for row in conn.execute(sql).fetchall():
                    result[row[0]] = row[1]
            except sqlite3.OperationalError as e:
                logging.warning("[DB] 获取表统计失败: %s", e)
        return result

    def get_b_status_counts(self) -> dict[str, int]:
        """获取 B 区各状态记录数"""
        result = {}
        with self.rw_lock.read_locked(), self.read_connection() as conn:
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM b_strm_files GROUP BY status"
            )
            for row in cur.fetchall():
                result[row[0] or "unknown"] = row[1]
        return result

    def get_db_file_size(self) -> int:
        """获取数据库文件大小（字节）"""
        try:
            return os.path.getsize(self.db_path)
        except OSError:
            return 0
