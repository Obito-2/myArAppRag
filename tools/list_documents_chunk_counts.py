"""
列出 documents 全表字段，并统计每本书（id = book_id）关联的 text_chunks / image_chunks 数量。

用法: 在项目根目录执行
  python tools/list_documents_chunk_counts.py

Windows 若书名仍乱码，可配合: set PYTHONUTF8=1 或使用 UTF-8 终端。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connect import execute_query  # noqa: E402


def _utf8_stdio() -> None:
    """Windows 控制台默认编码易导致中文书名乱码，尽量改为 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8")
            except (OSError, ValueError, AttributeError):
                pass


def main() -> None:
    _utf8_stdio()

    docs = execute_query(
        "SELECT * FROM documents ORDER BY name NULLS LAST, id;",
        fetch_all=True,
    )
    if not docs:
        print("documents 表为空")
        return

    print(f"=== documents 共 {len(docs)} 条 ===\n")
    for i, d in enumerate(docs, 1):
        row = dict(d)
        # RealDictRow 可能含不可直接 json 序列化类型
        for k, v in list(row.items()):
            if hasattr(v, "isoformat"):
                row[k] = str(v)
        print(f"--- [{i}] ---")
        print(json.dumps(row, ensure_ascii=False, indent=2))

    counts = execute_query(
        """
        SELECT
          d.id AS book_id,
          d.name AS book_name,
          COALESCE(tc.cnt, 0) AS text_chunks_cnt,
          COALESCE(ic.cnt, 0) AS image_chunks_cnt,
          COALESCE(tc.cnt, 0) + COALESCE(ic.cnt, 0) AS chunks_total
        FROM documents d
        LEFT JOIN (
          SELECT book_id, COUNT(*)::bigint AS cnt
          FROM text_chunks
          GROUP BY book_id
        ) tc ON tc.book_id = d.id
        LEFT JOIN (
          SELECT book_id, COUNT(*)::bigint AS cnt
          FROM image_chunks
          WHERE book_id IS NOT NULL
          GROUP BY book_id
        ) ic ON ic.book_id = d.id
        ORDER BY chunks_total DESC, d.name NULLS LAST, d.id;
        """,
        fetch_all=True,
    )
    assert counts is not None

    print("\n=== 各 book_id 关联 chunk 数量 ===\n")
    for r in counts:
        bid = str(r["book_id"])
        name = r["book_name"] or ""
        print(f"book_id : {bid}")
        print(f"book_name: {name}")
        print(
            f"  text_chunks={r['text_chunks_cnt']}, "
            f"image_chunks={r['image_chunks_cnt']}, "
            f"total={r['chunks_total']}"
        )
        print()


if __name__ == "__main__":
    main()
