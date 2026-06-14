"""
删除指定 documents 行（仅当 text_chunks / image_chunks 均为 0 时执行）。

当前批次：无 chunk 的术语简要 / 生僻字库 重复文档。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db_connect import get_connection, release_connection  # noqa: E402

DOCUMENT_IDS = (
    "a06d66cf-8a59-4e49-a338-ec99d12b8fb6",
    "43cc062c-99b2-4e7e-990d-5893eabafe9a",
)


def main() -> None:
    ids = list(DOCUMENT_IDS)
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT d.id, d.name,
              (SELECT COUNT(*) FROM text_chunks t WHERE t.book_id = d.id) AS tc,
              (SELECT COUNT(*) FROM image_chunks i WHERE i.book_id = d.id) AS ic
            FROM documents d
            WHERE d.id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        rows = cur.fetchall()
        if len(rows) != len(ids):
            found = {str(r["id"]) for r in rows}
            missing = set(ids) - found
            raise SystemExit(f"以下 id 在 documents 中不存在: {missing}")

        for r in rows:
            if int(r["tc"]) > 0 or int(r["ic"]) > 0:
                raise SystemExit(
                    f"拒绝删除：{r['id']} 仍有 chunk (text={r['tc']}, image={r['ic']})"
                )
            print(f"将删除: {r['id']}  {r['name']!r}")

        cur.execute("DELETE FROM documents WHERE id = ANY(%s::uuid[])", (ids,))
        n = cur.rowcount
        conn.commit()
        print(f"已提交，删除 documents {n} 行。")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_connection(conn)


if __name__ == "__main__":
    main()
