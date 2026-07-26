#!/usr/bin/env python3
"""扫描并修复历史「假 duplicate」分叉（P3）。

假 duplicate 定义：
  b_strm_files.status = 'duplicate'
  且 local_path 仍以 .strm 结尾（未真正隔离到 .duplicate）
  且磁盘文件仍存在

处理策略（默认 dry-run）：
  - 将 status 恢复为 valid，便于 ensure_single_visible_instance 再次隔离
  - 可选 --apply 写库；可选 --ensure 对每个 fingerprint 调用 ensure_single

用法（在项目根或 src 下）：
  python src/tools/repair_false_duplicates.py --db bridge.db
  python src/tools/repair_false_duplicates.py --db bridge.db --apply
  python src/tools/repair_false_duplicates.py --db bridge.db --apply --ensure
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path


def _find_false_duplicates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT local_path, webdav_path, fingerprint, status, updated_at
        FROM b_strm_files
        WHERE status = 'duplicate'
          AND lower(local_path) LIKE '%.strm'
        """
    )
    rows = []
    for row in cur.fetchall():
        p = Path(row["local_path"])
        if p.exists() and p.suffix.lower() == ".strm":
            # 排除真正已改名但仍以 .strm 中间段出现的极端路径：
            # 标准隔离后缀是 name.strm.duplicate，local_path 不会以 .strm 结尾。
            if ".duplicate" in p.name:
                continue
            rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="修复历史假 duplicate 分叉")
    parser.add_argument("--db", required=True, help="bridge.db 路径")
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写库（将 status 改回 valid）；默认仅 dry-run",
    )
    parser.add_argument(
        "--ensure", action="store_true",
        help="写库后尝试对每个 fingerprint 调用 ensure_single（需完整运行时）",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = _find_false_duplicates(conn)
        print(f"发现假 duplicate 记录: {len(rows)}")
        fingerprints: set[str] = set()
        for row in rows:
            print(
                f"  path={row['local_path']} fp={row['fingerprint']} "
                f"webdav={row['webdav_path']}"
            )
            if row["fingerprint"]:
                fingerprints.add(row["fingerprint"])

        if not rows:
            return 0

        if not args.apply:
            print("dry-run：未写库。加 --apply 以恢复 status=valid。")
            return 0

        now_import = __import__("time")
        now = now_import.time()
        for row in rows:
            conn.execute(
                """
                UPDATE b_strm_files
                SET status = 'valid', updated_at = ?
                WHERE local_path = ? AND status = 'duplicate'
                """,
                (now, row["local_path"]),
            )
        conn.commit()
        print(f"已恢复 {len(rows)} 条 status=valid")

        if args.ensure:
            print("--ensure：请在 AppService 运行环境中另行触发 ensure_single；")
            print("本工具不嵌入完整引擎，避免误连生产 OpenList。")
            print(f"涉及 fingerprint 数: {len(fingerprints)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
