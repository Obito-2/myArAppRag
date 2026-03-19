from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json


FIELD_MAPPING = {
    "ISBN": "isbn",
    "格式": "format",
    "数据量": "data_size",
}


Book = dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import book metadata into documents table.")
    parser.add_argument("--dsn", required=True, help="PostgreSQL DSN")
    parser.add_argument(
        "--jsonl-path",
        default="book.jsonl",
        help="Path to the source JSONL file",
    )
    return parser.parse_args()


def load_books(jsonl_path: Path) -> list[Book]:
    books: list[Book] = []
    with jsonl_path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            books.append(json.loads(line))
    return books


def parse_authors(authors: str | None) -> list[str] | None:
    if not authors:
        return None
    parts = [part.strip() for part in authors.replace("，", "、").split("、")]
    parts = [part for part in parts if part]
    return parts or None


def build_metadata(book: Book) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for source_key, target_key in FIELD_MAPPING.items():
        value = book.get(source_key)
        if isinstance(value, str) and value:
            metadata[target_key] = value
    return metadata


def main() -> None:
    args = parse_args()
    books = load_books(Path(args.jsonl_path))

    conn = psycopg2.connect(args.dsn)
    try:
        with conn:
            with conn.cursor() as cur:
                for book in books:
                    name = str(book["name"])
                    cur.execute("SELECT id FROM documents WHERE name = %s LIMIT 1", (name,))
                    existing = cur.fetchone()
                    if existing is not None:
                        print(f"SKIP {name} -> {existing[0]}")
                        continue

                    cur.execute(
                        """
                        INSERT INTO documents (name, authors, publish_info, metadata, vector_dimensions)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            name,
                            parse_authors(book.get("authors") if isinstance(book.get("authors"), str) else None),
                            book.get("publish_info") if isinstance(book.get("publish_info"), str) else None,
                            Json(build_metadata(book)),
                            1536,
                        ),
                    )
                    inserted = cur.fetchone()
                    if inserted is None:
                        raise RuntimeError(f"Insert failed for {name}")
                    print(f"INSERT {name} -> {inserted[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
