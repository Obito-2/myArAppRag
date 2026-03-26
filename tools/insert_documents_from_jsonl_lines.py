from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 让脚本可从任意 cwd 运行
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from tools.db_connect import get_connection, release_connection


def main():
    parser = argparse.ArgumentParser(description="从 book_documents.jsonl 按行插入 documents 表")
    parser.add_argument(
        "--start-line",
        type=int,
        default=8,
        metavar="N",
        help="起始行号（从 1 计，含该行）",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=2,
        metavar="K",
        help="连续读取多少行（每行一条 JSON）",
    )
    args = parser.parse_args()

    jsonl_path = PROJECT_ROOT / "importData" / "book_documents.jsonl"
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()

    start_idx = args.start_line - 1
    end_idx = start_idx + args.count
    if start_idx < 0 or end_idx > len(lines):
        raise SystemExit(
            f"行范围越界：文件共 {len(lines)} 行，无法读取第 {args.start_line} 行起的 {args.count} 行"
        )
    items = [json.loads(lines[i]) for i in range(start_idx, end_idx)]
    conn = get_connection()
    cur = conn.cursor()
    ids: list[str] = []
    try:
        for it in items:
            cur.execute(
                """
                INSERT INTO documents (name, authors, content)
                VALUES (%s, %s, %s)
                RETURNING id;
                """,
                (it.get("name"), it.get("authors"), it.get("content")),
            )
            ids.append(str(cur.fetchone()["id"]))
        conn.commit()
    finally:
        cur.close()
        release_connection(conn)
    print(json.dumps(ids, ensure_ascii=False))


if __name__ == "__main__":
    main()