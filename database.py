from __future__ import annotations

import os
import sqlite3
import threading
import time
import logging
from contextlib import contextmanager
from typing import Generator


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.RLock()

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

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

    def init_db(self) -> None:
        logging.info("[DB] 开始初始化数据库表结构: %s", self.db_path)
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

    def get_a_by_local(self, local_path: str) -> tuple | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files WHERE local_path = ?",
                (local_path,),
            )
            return cur.fetchone()

    def get_b_by_local(self, local_path: str) -> tuple | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, updated_at
                FROM b_strm_files WHERE local_path = ?
                """,
                (local_path,),
            )
            return cur.fetchone()

    def get_b_by_webdav(self, webdav_path: str) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, updated_at
                FROM b_strm_files WHERE webdav_path = ?
                """,
                (webdav_path,),
            )
            return cur.fetchall()

    def get_all_a(self) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT local_path, webdav_path, parent_webdav_path, updated_at FROM a_strm_files")
            return cur.fetchall()

    def get_all_b(self) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute("""
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, updated_at
                FROM b_strm_files
                """)
            return cur.fetchall()

    def get_all_c(self) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute("""
                SELECT local_path, webdav_path, original_b_path, ghost_root, moved_at
                FROM c_ghost_files
                """)
            return cur.fetchall()

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
        self.cleanup_expired_ghosts()
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT expire_time FROM ghost_protection WHERE webdav_path = ?",
                (webdav_path,),
            )
            row = cur.fetchone()
            return bool(row and row[0] > time.time())

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

    def get_protected_roots(self) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                "SELECT root_path, trash_path, active, updated_at FROM protected_roots")
            return cur.fetchall()

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

    def get_b_under_root(self, webdav_root: str) -> list[tuple]:
        pattern = webdav_root.rstrip("/") + "/%"
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT local_path, webdav_path, parent_webdav_path, source_a_path, updated_at
                FROM b_strm_files
                WHERE webdav_path = ? OR webdav_path LIKE ?
                """,
                (webdav_root, pattern),
            )
            return cur.fetchall()

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

    def get_identity_by_fingerprint(self, fingerprint: str) -> tuple | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, webdav_path, source_a_path, current_b_path, updated_at
                FROM strm_identity
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            return cur.fetchone()

    def get_identity_by_webdav(self, webdav_path: str) -> tuple | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, webdav_path, source_a_path, current_b_path, updated_at
                FROM strm_identity
                WHERE webdav_path = ?
                """,
                (webdav_path,),
            )
            return cur.fetchone()

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

            conn.execute(
                "DELETE FROM b_strm_files WHERE local_path = ?",
                (old_local_path,),
            )
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
                    status or "valid",
                    now,
                ),
            )
            conn.commit()
            return True

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

    def get_b_instances_by_fingerprint(self, fingerprint: str) -> list[tuple]:
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
            return cur.fetchall()

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

    def get_b_by_local_full(self, local_path: str) -> tuple | None:
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
            return cur.fetchone()

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
            self, fingerprint: str) -> tuple | None:
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
            return row if row else None

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

    def get_all_b_by_fingerprint(self, fingerprint: str):
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
            return cur.fetchall()

    def get_all_b_records(self) -> list[tuple]:
        """获取所有 B 区记录（用于启动时对比）"""
        with self.lock, self.connection() as conn:
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
            return cur.fetchall()

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
            return cur.fetchone()[0]

    def has_other_b_instance(self, fingerprint: str,
                             exclude_local_path: str) -> bool:
        """检查是否存在同一指纹的其他 B 区实例（排除指定路径）。"""
        with self.connection() as conn:
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
            self, fingerprint: str) -> tuple | None:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE fingerprint = ?
                """,
                (fingerprint,),
            )
            return cur.fetchone()

    def get_media_boundaries_by_source_name(
        self, source_media_name: str, engine_entry_path: str
    ) -> list[tuple]:
        with self.lock, self.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, source_media_name, current_media_name, engine_entry_path, updated_at
                FROM strm_media_boundary
                WHERE source_media_name = ? AND engine_entry_path = ?
                """,
                (source_media_name, engine_entry_path),
            )
            return cur.fetchall()

    def get_media_boundary_by_current_name(
        self, current_media_name: str, engine_entry_path: str
    ) -> tuple | None:
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
            return cur.fetchone()
