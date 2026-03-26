"""
删除 text_chunks 与 relations 中「与 count_book_chunks_and_relations 相同条件」的数据，其余表与行不动。

顺序：先 DELETE relations（条件与统计脚本一致），再 DELETE text_chunks（book_id 在给定列表）。

默认仅打印将删除的行数（dry-run）；加 --execute 才真正执行。

用法:
  python tools/delete_book_chunks_and_relations.py           # 仅预览
  python tools/delete_book_chunks_and_relations.py --execute # 执行删除
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connect import execute_query, get_connection, release_connection  # noqa: E402

# 与 tools/count_book_chunks_and_relations.py 保持一致
BOOK_IDS = (
    "45a4f3d7-a3da-4b53-959a-72ee245f2f18",
    "e6b410cb-99e9-44c4-8c40-5445779ca8e3",
)

SQL_DELETE_RELATIONS = """
DELETE FROM relations r
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
  )
"""

SQL_DELETE_TEXT_CHUNKS = """
DELETE FROM text_chunks WHERE book_id = ANY(%s::uuid[])
"""


def preview_counts(ids: list[str]) -> tuple[int, int]:
    row = execute_query(
        """
    SELECT
      (SELECT COUNT(*) FROM text_chunks  WHERE book_id = ANY(%s::uuid[])) AS tc,
      (SELECT COUNT(*) FROM relations r WHERE
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
        )
      ) AS rel
    """,
        params=(ids, ids, ids),
        fetch_one=True,
    )
    assert row is not None
    return int(row["tc"]), int(row["rel"])


def run_delete(ids: list[str]) -> tuple[int, int]:
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(SQL_DELETE_RELATIONS, (ids, ids))
        rel_n = cur.rowcount
        cur.execute(SQL_DELETE_TEXT_CHUNKS, (ids,))
        tc_n = cur.rowcount
        conn.commit()
        return rel_n, tc_n
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_connection(conn)


def main() -> None:
    ap = argparse.ArgumentParser(description="按 book_id 删除 text_chunks 及相关 relations")
    ap.add_argument(
        "--execute",
        action="store_true",
        help="执行删除；省略则只打印当前匹配行数，不写库",
    )
    args = ap.parse_args()

    ids = list(BOOK_IDS)
    tc_cnt, rel_cnt = preview_counts(ids)

    print("=== 匹配条件（与 count_book_chunks_and_relations 一致）===")
    print(f"  将删除 text_chunks 行数: {tc_cnt}")
    print(f"  将删除 relations 行数:   {rel_cnt}")

    if not args.execute:
        print("\n未加 --execute，未修改数据库。确认后执行: python tools/delete_book_chunks_and_relations.py --execute")
        return

    rel_n, tc_n = run_delete(ids)
    print("\n=== 已提交删除 ===")
    print(f"  relations   实际删除: {rel_n} 行")
    print(f"  text_chunks 实际删除: {tc_n} 行")


if __name__ == "__main__":
    main()
