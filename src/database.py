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
    fingerprint: str
    source_media_name: str
    current_media_name: str
    engine_entry_path: str
    updated_at: float


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


# ============================================================
# 数据库操作
# ============================================================


class Database:
    _last_ghost_cleanup: float

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        max_retries = 3
        retry_delay = 0.1

        conn = None
        for attempt in range(max_retries):
            try:
                self._ensure_db_writable()

                conn = sqlite3.connect(self.db_path, timeout=30)
                # ========== 性能优化 PRAGMA ==========
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-64000")      # 64MB page cache
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA mmap_size=268435456")     # 256MB mmap
                # =====================================

                # 使用真正的写操作验证连接是否可写
                try:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS _write_test(x)")
                    conn.execute("DROP TABLE _write_test")
                    conn.commit()
                except sqlite3.OperationalError as e:
                    conn.close()
                    conn = None
                    raise sqlite3.OperationalError(
                        f"readonly connection: {e}")

                # 验证通过，yield 连接给调用者
                yield conn
                # 使用完毕后关闭连接
                conn.close()
                conn = None
                return

            except sqlite3.OperationalError as e:
                # 确保连接被关闭
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None

                if "readonly" in str(e).lower() and attempt < max_retries - 1:
                    logging.warning(
                        "[DB] 数据库只读错误，尝试修复权限 (尝试 %s/%s): %s",
                        attempt + 1, max_retries, e
                    )
                    time.sleep(retry_delay)
                    continue
                raise

    @contextmanager
    def read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """只读连接上下文管理器 - 不持有写锁，允许并发读取 (WAL模式下安全)"""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA mmap_size=268435456")
            conn.execute("PRAGMA query_only=ON")
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _ensure_column(
        self,
        cur: sqlite3.Cursor,
        table_name: str,
        column_name: str,
        column_def: str,
    ) -> None:
        cur.execute(f"PRAGMA table_info({table_name})")
        columns = {row[1] for row in cur.fetchall()}

        if column_name not in columns:
            cur.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()

        # ===== 修复：确保数据库文件可写 =====
        self._ensure_db_writable()
        # ====================================
        logging.info("[DB] 开始初始化数据库表结构")
        with self.lock, self.connection() as conn:
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
                    updated_at REAL NOT NULL
                )
                """)
            self._ensure_column(cur, "b_strm_files", "fingerprint", "TEXT")
            self._ensure_column(
                cur,
                "b_strm_files",
                "status",
                "TEXT DEFAULT 'valid'",
            )

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
                CREATE INDEX IF NOT EXISTS idx_a_strm_webdav_path
                ON a_strm_files(webdav_path)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_b_strm_webdav_path
                ON b_strm_files(webdav_path)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_b_strm_fingerprint
                ON b_strm_files(fingerprint)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_b_strm_status
                ON b_strm_files(status)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_identity_webdav_path
                ON strm_identity(webdav_path)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_identity_current_b_path
                ON strm_identity(current_b_path)
                """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS strm_media_boundary (
                    fingerprint TEXT PRIMARY KEY,
                    source_media_name TEXT NOT NULL,
                    current_media_name TEXT NOT NULL,
                    engine_entry_path TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_boundary_source_name
                ON strm_media_boundary(source_media_name)
                """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_boundary_current_name
                ON strm_media_boundary(current_media_name)
                """)

            conn.commit()
            logging.info("[DB] 数据库核心表与索引核对并创建完成！")

    def init_db(self) -> None:
        """初始化数据库（兼容旧调用，实际初始化已在 __init__ 中完成）"""
        logging.debug("[DB] init_db 被调用，数据库已在 __init__ 中初始化")
        pass

    def upsert_a(self, local_path: str, webdav_path: str,
                 parent_webdav_path: str) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (local_path, webdav_path, parent_webdav_path, now),
            )
            conn.commit()

    def upsert_b(
        self,
        local_path: str,
        webdav_path: str,
        parent_webdav_path: str,
        source_a_path: str | None,
        fingerprint: str | None = None,
        status: str = "valid",
    ) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO b_strm_files(
                    local_path,
                    webdav_path,
                    parent_webdav_path,
                    source_a_path,
                    fingerprint,
                    status,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    local_path,
                    webdav_path,
                    parent_webdav_path,
                    source_a_path,
                    fingerprint,
                    status,
                    now,
                ),
            )
            conn.commit()

    def upsert_c(
        self,
        local_path: str,
        webdav_path: str,
        original_b_path: str,
        ghost_root: str,
    ) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO c_ghost_files(local_path, webdav_path, original_b_path, ghost_root, moved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (local_path, webdav_path, original_b_path, ghost_root, now),
            )
            conn.commit()

    def delete_a_by_local(self, local_path: str) -> None:
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM a_strm_files WHERE local_path = ?", (local_path,))
            conn.commit()

    def delete_b_by_local(self, local_path: str) -> None:
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM b_strm_files WHERE local_path = ?", (local_path,))
            conn.commit()

    def delete_c_by_local(self, local_path: str) -> None:
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM c_ghost_files WHERE local_path = ?", (local_path,))
            conn.commit()

    def get_a_by_local(self, local_path: str) -> ARecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            )
            row = cur.fetchone()
            return ARecord(*row) if row else None

    def get_b_by_local(self, local_path: str) -> BRecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at
                FROM b_strm_files WHERE local_path = ?
                """,
                (local_path,),
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def get_a_by_webdav(self, webdav_path: str) -> ARecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE webdav_path = ?",
                (webdav_path,),
            )
            row = cur.fetchone()
            return ARecord(*row) if row else None

    def get_b_by_webdav(self, webdav_path: str) -> list[BRecord]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at
                FROM b_strm_files WHERE webdav_path = ?
                """,
                (webdav_path,),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_a_records(self) -> list[ARecord]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files")
            return [ARecord(*row) for row in cur.fetchall()]

    def get_all_b(self) -> list[BRecord]:
        with self.lock, self.connection() as conn:
            cur = conn.execute("""
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at
                FROM b_strm_files
                """)
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_c(self) -> list[CRecord]:
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO known_folders(folder_path, source, updated_at)
                VALUES (?, ?, ?)
                """,
                (folder_path, source, now),
            )
            conn.commit()

    def get_known_folders(self) -> list[str]:
        with self.lock, self.connection() as conn:
            cur = conn.execute("SELECT folder_path FROM known_folders")
            return [row[0] for row in cur.fetchall()]

    def remove_known_folder_prefix(self, folder_path: str) -> None:
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM known_folders WHERE folder_path = ? OR folder_path LIKE ?",
                (folder_path, folder_path.rstrip("/") + "/%"),
            )
            conn.commit()

    def set_ghost_protection(self, webdav_path: str,
                             seconds: int, reason: str = "") -> None:
        expire = time.time() + seconds
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM ghost_protection WHERE expire_time <= ?", (now,))
            conn.commit()

    def is_ghost_protected(self, webdav_path: str) -> bool:
        """检查路径是否受幽灵保护。
        
        优化：每 60 秒最多执行一次过期清理，避免热路径中的频繁 DELETE 查询。
        """
        now = time.time()
        # 每 60 秒执行一次过期清理
        if not hasattr(self, "_last_ghost_cleanup") or now - self._last_ghost_cleanup > 60:
            self.cleanup_expired_ghosts()
            self._last_ghost_cleanup = now
        
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT expire_time FROM ghost_protection WHERE webdav_path = ?",
                (webdav_path,),
            )
            row = cur.fetchone()
            return bool(row and row[0] > now)

    def set_protected_root(self, root_path: str,
                           trash_path: str, active: bool = True) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT root_path, trash_path, active, updated_at FROM protected_roots")
            return [ProtectedRootRecord(r[0], r[1], bool(r[2]), r[3]) for r in cur.fetchall()]

    def get_protected_root_paths(self) -> list[str]:
        with self.lock, self.connection() as conn:
            cur = conn.execute("SELECT root_path FROM protected_roots")
            return [row[0] for row in cur.fetchall()]

    def save_protected_roots_snapshot(
            self, roots: list[tuple[str, str]]) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT root_path FROM protected_roots_snapshot")
            return [row[0] for row in cur.fetchall()]

    def set_control(self, key: str, value: str) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sync_control(control_key, control_value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, value, now),
            )
            conn.commit()

    def get_control(self, key: str, default: str = "") -> str:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT control_value FROM sync_control WHERE control_key = ?",
                (key,),
            )
            row = cur.fetchone()
            return row[0] if row else default

    def get_b_under_root(self, webdav_root: str) -> list[BRecord]:
        pattern = webdav_root.rstrip("/") + "/%"
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at
                FROM b_strm_files
                WHERE webdav_path = ? OR webdav_path LIKE ?
                """,
                (webdav_root, pattern),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def delete_b_under_root(self, webdav_root: str) -> None:
        pattern = webdav_root.rstrip("/") + "/%"
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM b_strm_files WHERE webdav_path = ? OR webdav_path LIKE ?",
                (webdav_root, pattern),
            )
            conn.commit()

    def upsert_identity(
        self,
        fingerprint: str,
        webdav_path: str,
        source_a_path: str | None,
        current_b_path: str | None,
    ) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM strm_identity WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.commit()

    def move_b_record(self, old_local_path: str, new_local_path: str) -> bool:
        """
        B 区文件被重命名/隔离后，把 b_strm_files 的 local_path
        从旧路径迁移到新路径，并保留 fingerprint/status。

        使用 INSERT OR REPLACE 实现原子操作，避免先 DELETE 再 INSERT
        时中间失败导致的数据丢失。
        """
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status
                FROM b_strm_files
                WHERE local_path = ?
                """,
                (old_local_path,),
            )
            row = cur.fetchone()
            if not row:
                return False

            webdav_path, parent_webdav_path, source_a_path, fingerprint, status = row
            now = time.time()
            new_status = status or "valid"

            try:
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
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_local_path,
                        webdav_path,
                        parent_webdav_path,
                        source_a_path,
                        fingerprint,
                        new_status,
                        now,
                    ),
                )
                # 删除旧记录
                conn.execute(
                    "DELETE FROM b_strm_files WHERE local_path = ?",
                    (old_local_path,),
                )
                conn.commit()
                return True
            except sqlite3.Error as e:
                conn.rollback()
                logging.error("[DB] move_b_record 失败: %s", e)
                return False

    def delete_identity_by_b_path(self, current_b_path: str) -> None:
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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

    def get_b_instances_by_fingerprint(self, fingerprint: str) -> list[BRecord]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path,
                       webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status,
                       updated_at
                FROM b_strm_files
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def mark_b_instance_status(self, local_path: str, status: str) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM b_strm_files WHERE fingerprint = ?",
                (fingerprint,),
            )
            conn.commit()

    def get_b_by_local_full(self, local_path: str) -> BRecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path,
                       webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status,
                       updated_at
                FROM b_strm_files
                WHERE local_path = ?
                """,
                (local_path,),
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def clear_identity_b_path_by_fingerprint(self, fingerprint: str) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
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

    def get_valid_b_instance_by_fingerprint(
            self, fingerprint: str) -> BRecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path,
                       webdav_path,
                       parent_webdav_path,
                       source_a_path,
                       fingerprint,
                       status,
                       updated_at
                FROM b_strm_files
                WHERE fingerprint = ?
                  AND status = 'valid'
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (fingerprint,),
            )
            row = cur.fetchone()
            return BRecord(*row) if row else None

    def mark_other_b_instances_duplicate(
        self,
        fingerprint: str,
        keep_local_path: str,
    ) -> list[str]:
        """
        将同 fingerprint 下除 keep_local_path 外的 valid 实例标记为 duplicate。
        返回被标记的 local_path 列表。
        """
        now = time.time()
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path
                FROM b_strm_files
                WHERE fingerprint = ?
                  AND local_path != ?
                  AND status = 'valid'
                """,
                (fingerprint, keep_local_path),
            )
            rows = [row[0] for row in cur.fetchall()]

            conn.execute(
                """
                UPDATE b_strm_files
                SET status = 'duplicate',
                    updated_at = ?
                WHERE fingerprint = ?
                  AND local_path != ?
                  AND status = 'valid'
                """,
                (now, fingerprint, keep_local_path),
            )
            conn.commit()

            return rows

    def get_all_b_by_fingerprint(self, fingerprint: str) -> list[BRecord]:
        """
        返回该 fingerprint 下所有 B 实例
        """
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path,
                       source_a_path, fingerprint, status, updated_at
                FROM b_strm_files
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            return [BRecord(*row) for row in cur.fetchall()]

    def get_all_b_records(self) -> list[BRecord]:
        """获取所有 B 区记录（用于启动时对比）"""
        with self.read_connection() as conn:
            cur = conn.execute("""
                    SELECT local_path,
                           webdav_path,
                           parent_webdav_path,
                           source_a_path,
                           fingerprint,
                           status,
                           updated_at
                    FROM b_strm_files
                """)
            return [BRecord(*row) for row in cur.fetchall()]

    def b_fingerprint_exists(self, fingerprint: str) -> bool:
        """检查 B 区数据库中是否已存在该指纹"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM b_strm_files WHERE fingerprint = ? LIMIT 1", (fingerprint,))
            return cur.fetchone() is not None

    def update_b_local_path(self, old_path: str, new_path: str) -> bool:
        """更新 B 区文件的本地路径（用于文件名改变的情况）"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "UPDATE b_strm_files SET local_path = ? WHERE local_path = ?",
                (new_path,
                 old_path))
            conn.commit()
            return cur.rowcount > 0

    def insert_b_strm_file(
        self,
        local_path: str,
        webdav_path: str,
        parent_webdav_path: str,
        source_a_path: str,
        fingerprint: str = "",
        status: str = "valid",
        updated_at: float | None = None,
    ) -> bool:
        """插入或更新 B 区 STRM 文件记录"""
        with self.lock, self.connection() as conn:
            cur = conn.cursor()
            if updated_at is None:
                updated_at = time.time()
            try:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO b_strm_files
                    (local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        local_path,
                        webdav_path,
                        parent_webdav_path,
                        source_a_path,
                        fingerprint,
                        status,
                        updated_at,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.Error as e:
                logging.error("[DB] 插入 B 区记录失败: %s", e)
                return False

    def get_a_count_under_root(self, cloud_media_root: str) -> int:
        """统计 A 区某个剧集根路径下共有多少集"""
        pattern = cloud_media_root.rstrip('/') + '/%'
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM a_strm_files WHERE webdav_path LIKE ?",
                (pattern,)
            )
            row = cur.fetchone()
            return row[0] if row else 0

    def has_other_b_instance(self, fingerprint: str,
                             exclude_local_path: str) -> bool:
        """检查是否存在同一指纹的其他 B 区实例（排除指定路径）。"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM b_strm_files WHERE fingerprint = ? AND local_path != ? LIMIT 1",
                (fingerprint, exclude_local_path),
            )
            return cur.fetchone() is not None

    def upsert_media_boundary(
        self,
        fingerprint: str,
        source_media_name: str,
        current_media_name: str,
        engine_entry_path: str,
    ) -> None:
        now = time.time()
        with self.lock, self.connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO strm_media_boundary(
                    fingerprint,
                    source_media_name,
                    current_media_name,
                    engine_entry_path,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, source_media_name,
                 current_media_name, engine_entry_path, now),
            )
            conn.commit()

    def get_media_boundary_by_fingerprint(
            self, fingerprint: str) -> BoundaryRecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            row = cur.fetchone()
            return BoundaryRecord(*row) if row else None

    def get_media_boundaries_by_source_name(
        self, source_media_name: str, engine_entry_path: str
    ) -> list[BoundaryRecord]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE source_media_name = ? AND engine_entry_path = ?
                """,
                (source_media_name, engine_entry_path),
            )
            return [BoundaryRecord(*row) for row in cur.fetchall()]

    def get_media_boundary_by_current_name(
        self, current_media_name: str, engine_entry_path: str
    ) -> BoundaryRecord | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE current_media_name = ? AND engine_entry_path = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (current_media_name, engine_entry_path),
            )
            row = cur.fetchone()
            return BoundaryRecord(*row) if row else None

    def get_media_boundary_by_source_name_only(
        self, source_media_name: str
    ) -> BoundaryRecord | None:
        """根据源媒体名查找边界映射（不限制引擎路径，取最新的）"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE source_media_name = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (source_media_name,),
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
            # ===== 修复：数据库文件不存在时，尝试创建空文件以确保可写 =====
            try:
                # 创建一个临时连接来创建数据库文件
                temp_conn = sqlite3.connect(str(db_path))
                temp_conn.close()
                logging.info("[DB] 已创建数据库文件: %s", db_path)
            except Exception as e:
                logging.error("[DB] 无法创建数据库文件: %s", e)
            # ============================================================

    # ========== 字幕表操作 ==========

    def init_subtitle_table(self) -> None:
        """初始化字幕表"""
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
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
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT id, local_path, target_path, fingerprint, season, episode, lang_code, status, created_at, updated_at FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            row = cur.fetchone()
            return SubtitleRecord(*row) if row else None

    def subtitle_exists(self, local_path: str) -> bool:
        """检查字幕是否已存在"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT 1 FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            return cur.fetchone() is not None

    def get_subtitles_by_fingerprint(self, fingerprint: str) -> list[SubtitleRecord]:
        """根据指纹获取所有字幕"""
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT id, local_path, target_path, fingerprint, season, episode, lang_code, status, created_at, updated_at FROM subtitles WHERE fingerprint = ?",
                (fingerprint,)
            )
            return [SubtitleRecord(*row) for row in cur.fetchall()]

    def delete_subtitle_by_local(self, local_path: str) -> None:
        """删除字幕记录"""
        with self.lock, self.connection() as conn:
            conn.execute(
                "DELETE FROM subtitles WHERE local_path = ?",
                (local_path,)
            )
            conn.commit()

    def cleanup_invalid_subtitles(self) -> None:
        """清理目标文件已不存在的字幕记录"""
        with self.lock, self.connection() as conn:
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
        data = [(lp, wp, pwp, now) for lp, wp, pwp in records]
        with self.lock, self.connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO a_strm_files(local_path, webdav_path, parent_webdav_path, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                data,
            )
            conn.commit()
            return len(data)

    def upsert_b_batch(self, records: list[tuple]) -> int:
        """
        批量 upsert B 区记录。
        records: [(local_path, webdav_path, parent_webdav_path, source_a_path, fingerprint, status), ...]
        返回成功行数。
        """
        if not records:
            return 0
        now = time.time()
        data = [(*r, now) for r in records]
        with self.lock, self.connection() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO b_strm_files(
                    local_path, webdav_path, parent_webdav_path,
                    source_a_path, fingerprint, status, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                data,
            )
            conn.commit()
            return len(data)

    def delete_a_batch(self, local_paths: list[str]) -> int:
        """批量删除 A 区记录"""
        if not local_paths:
            return 0
        with self.lock, self.connection() as conn:
            conn.executemany(
                "DELETE FROM a_strm_files WHERE local_path = ?",
                [(p,) for p in local_paths],
            )
            conn.commit()
            return len(local_paths)

    def delete_b_batch(self, local_paths: list[str]) -> int:
        """批量删除 B 区记录"""
        if not local_paths:
            return 0
        with self.lock, self.connection() as conn:
            conn.executemany(
                "DELETE FROM b_strm_files WHERE local_path = ?",
                [(p,) for p in local_paths],
            )
            conn.commit()
            return len(local_paths)

    # ========== 统计方法（WebUI 仪表盘用）==========

    def get_table_counts(self) -> dict[str, int]:
        """获取各表记录数，用于 WebUI 仪表盘显示"""
        tables = [
            "a_strm_files", "b_strm_files", "c_ghost_files",
            "strm_identity", "ghost_protection", "known_folders",
            "subtitles", "strm_media_boundary",
        ]
        result = {}
        with self.read_connection() as conn:
            for table in tables:
                try:
                    cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    result[table] = cur.fetchone()[0]
                except sqlite3.OperationalError:
                    result[table] = 0
        return result

    def get_b_status_counts(self) -> dict[str, int]:
        """获取 B 区各状态记录数"""
        result = {}
        with self.read_connection() as conn:
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
