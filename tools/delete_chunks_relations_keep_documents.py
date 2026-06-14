"""
按 book_id 删除 text_chunks、image_chunks，以及任一端指向这些 chunk 的 relations。
不删除 documents。

王贵祥译注《营造法式》、梁思成注释《营造法式》两本。

用法:
  python tools/delete_chunks_relations_keep_documents.py           # 预览
  python tools/delete_chunks_relations_keep_documents.py --execute # 执行
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connect import execute_query, get_connection, release_connection  # noqa: E402

BOOK_IDS = (
    "7a1746cb-bacb-4d7d-b122-ca72ea52c02e",
    "4ced9d70-e725-4312-9988-d3acd79d878b",
)

# 任一端为该书下的 text_chunk 或 image_chunk
SQL_DELETE_RELATIONS = """
DELETE FROM relations r
WHERE
  (
    r.source_type = 'image'
    AND EXISTS (
      SELECT 1 FROM image_chunks i
      WHERE i.image_id = r.source_id AND i.book_id = ANY(%s::uuid[])
    )
  )
  OR
  (
    r.source_type <> 'image'
    AND EXISTS (
      SELECT 1 FROM text_chunks t
      WHERE t.chunk_id = r.source_id AND t.book_id = ANY(%s::uuid[])
    )
  )
  OR
  (
    r.target_type = 'image'
    AND EXISTS (
      SELECT 1 FROM image_chunks i
      WHERE i.image_id = r.target_id AND i.book_id = ANY(%s::uuid[])
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

SQL_DELETE_TEXT = "DELETE FROM text_chunks WHERE book_id = ANY(%s::uuid[])"
SQL_DELETE_IMAGE = "DELETE FROM image_chunks WHERE book_id = ANY(%s::uuid[])"


def preview(ids: list[str]) -> tuple[int, int, int]:
    row = execute_query(
        """
    SELECT
      (SELECT COUNT(*) FROM text_chunks WHERE book_id = ANY(%s::uuid[])) AS tc,
      (SELECT COUNT(*) FROM image_chunks WHERE book_id = ANY(%s::uuid[])) AS ic,
      (SELECT COUNT(*) FROM relations r WHERE
        (
          r.source_type = 'image'
          AND EXISTS (
            SELECT 1 FROM image_chunks i
            WHERE i.image_id = r.source_id AND i.book_id = ANY(%s::uuid[])
          )
        )
        OR
        (
          r.source_type <> 'image'
          AND EXISTS (
            SELECT 1 FROM text_chunks t
            WHERE t.chunk_id = r.source_id AND t.book_id = ANY(%s::uuid[])
          )
        )
        OR
        (
          r.target_type = 'image'
          AND EXISTS (
            SELECT 1 FROM image_chunks i
            WHERE i.image_id = r.target_id AND i.book_id = ANY(%s::uuid[])
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
        params=(ids, ids, ids, ids, ids, ids),
        fetch_one=True,
    )
    assert row is not None
    return int(row["tc"]), int(row["ic"]), int(row["rel"])


def run_delete(ids: list[str]) -> tuple[int, int, int]:
    p4 = (ids, ids, ids, ids)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(SQL_DELETE_RELATIONS, p4)
        rel_n = cur.rowcount
        cur.execute(SQL_DELETE_TEXT, (ids,))
        tc_n = cur.rowcount
        cur.execute(SQL_DELETE_IMAGE, (ids,))
        ic_n = cur.rowcount
        conn.commit()
        return rel_n, tc_n, ic_n
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_connection(conn)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    ids = list(BOOK_IDS)
    tc, ic, rel = preview(ids)
    print("=== book_id 为以下两本 documents（保留 documents 行）===")
    for u in ids:
        print(f"  {u}")
    print(f"  将删除 text_chunks:  {tc}")
    print(f"  将删除 image_chunks: {ic}")
    print(f"  将删除 relations:    {rel}")

    if not args.execute:
        print("\n未加 --execute，未改库。")
        return

    rel_n, tc_n, ic_n = run_delete(ids)
    print("\n=== 已提交 ===")
    print(f"  relations:    {rel_n}")
    print(f"  text_chunks:  {tc_n}")
    print(f"  image_chunks: {ic_n}")


if __name__ == "__main__":
    main()
