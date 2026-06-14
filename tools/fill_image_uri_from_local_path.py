"""
按 local_path 的文件名回填 image_uri（数据库列名，口语中有时称 image_url）。

对映射表中的每个 book_id：查询 image_uri 为空的 image_chunks，将
  完整 URL = URL 前缀 + Path(local_path).name
写回 image_uri。兼容 Windows 路径。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 项目根目录，便于任意 cwd 下运行
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.db_connect import get_connection, release_connection

# book_id -> 对象存储 URL 前缀（须以 / 结尾）
BOOK_ID_TO_IMAGE_URI_PREFIX: dict[str, str] = {
    "4ced9d70-e725-4312-9988-d3acd79d878b": (
        "https://objectstorageapi.hzh.sealos.run/q5nnz4bx-yingzaofashi/liangzhu/"
    ),
    "7a1746cb-bacb-4d7d-b122-ca72ea52c02e": (
        "https://objectstorageapi.hzh.sealos.run/q5nnz4bx-yingzaofashi/wangzhu/"
    ),
}


def _normalize_prefix(prefix: str) -> str:
    return prefix.rstrip("/") + "/"


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    total_updated = 0
    total_skipped = 0
    try:
        for book_id, prefix in BOOK_ID_TO_IMAGE_URI_PREFIX.items():
            base = _normalize_prefix(prefix)
            cur.execute(
                """
                SELECT image_id, local_path
                FROM image_chunks
                WHERE book_id = %s::uuid
                  AND (image_uri IS NULL OR TRIM(image_uri) = '')
                """,
                (book_id,),
            )
            rows = cur.fetchall()
            updated = 0
            skipped = 0
            for row in rows:
                image_id = row["image_id"]
                local_path = row["local_path"]
                if local_path is None or not str(local_path).strip():
                    print(f"[WARN] 跳过 image_id={image_id}：local_path 为空")
                    skipped += 1
                    continue
                name = Path(str(local_path)).name
                if not name:
                    print(f"[WARN] 跳过 image_id={image_id}：无法解析文件名 {local_path!r}")
                    skipped += 1
                    continue
                uri = base + name
                cur.execute(
                    """
                    UPDATE image_chunks
                    SET image_uri = %s
                    WHERE image_id = %s::uuid
                    """,
                    (uri, str(image_id)),
                )
                updated += 1
            print(f"  book_id={book_id}  更新 {updated} 条, 跳过 {skipped} 条")
            total_updated += updated
            total_skipped += skipped
        conn.commit()
        print(f"===== 合计 更新 {total_updated} 条, 跳过 {total_skipped} 条 =====")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        release_connection(conn)


if __name__ == "__main__":
    main()
