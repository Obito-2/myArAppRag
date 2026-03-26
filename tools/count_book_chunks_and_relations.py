"""
按指定 documents.id（book_id）统计 text_chunks / image_chunks 行数，
以及 relations 中任一端为「属于该书的文本 chunk」的关系条数。

用法（在项目根目录）:
  python tools/count_book_chunks_and_relations.py

可通过环境变量 DB_URL 覆盖数据库连接（与 db_connect 一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 保证可导入同目录下的 db_connect
sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connect import execute_query  # noqa: E402

BOOK_IDS = (
    "45a4f3d7-a3da-4b53-959a-72ee245f2f18",
    "e6b410cb-99e9-44c4-8c40-5445779ca8e3",
)


def main() -> None:
    ids = list(BOOK_IDS)

    # 说明：库表名为 text_chunks / image_chunks，无单独 chunks 表
    sql_chunks = """
    SELECT
      (SELECT COUNT(*) FROM text_chunks  WHERE book_id = ANY(%s::uuid[])) AS text_chunks_cnt,
      (SELECT COUNT(*) FROM image_chunks WHERE book_id = ANY(%s::uuid[])) AS image_chunks_cnt;
    """
    row = execute_query(sql_chunks, params=(ids, ids), fetch_one=True)
    assert row is not None
    print("=== 按 book_id ∈ 给定两本 documents.id ===")
    print(f"  text_chunks  行数: {row['text_chunks_cnt']}")
    print(f"  image_chunks 行数: {row['image_chunks_cnt']}")

    # 任一端为文本类型且对应 text_chunks.book_id 在列表中
    sql_rel = """
    SELECT COUNT(*) AS cnt
    FROM relations r
    WHERE
      (
        r.source_type <> 'image'
        AND EXISTS (
          SELECT 1 FROM text_chunks t
          WHERE t.chunk_id = r.source_id AND t.book_id = ANY(%s::uuid[])
        )
      )
      OR
      (
        r.target_type <> 'image'
        AND EXISTS (
          SELECT 1 FROM text_chunks t
          WHERE t.chunk_id = r.target_id AND t.book_id = ANY(%s::uuid[])
        )
      );
    """
    rel_row = execute_query(sql_rel, params=(ids, ids), fetch_one=True)
    assert rel_row is not None
    print(
        "=== relations：source 或 target 为文本 chunk，且该 chunk 的 book_id 为上述之一 ==="
    )
    print(f"  关系条数: {rel_row['cnt']}")


if __name__ == "__main__":
    main()
