#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MD 分块 → 元数据抽取 → embedding / ts_vector / 图片上传 → 入库

用法:
  python import_md_to_db.py [md_file_path] [--dry-run]

默认处理: knowledgeBase/pdfParse/cleaned_data/consolidated_corpus_final.md
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import jieba
import numpy as np
from openai import OpenAI

# ── 让 Python 找到项目根目录下的 tools 包 ────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from split_md_to_jsonl import (
    BOOK_ID,
    BOOK_NAME,
    TempImageChunk,
    TempNoteChunk,
    TempTextChunk,
    build_relations,
    build_search_text,
    parse_md,
)
from tools.bucket_load import client as minio_client
from tools.db_connect import get_connection, release_connection

# ── 常量 ─────────────────────────────────────────────────────────
MINIO_BUCKET = "q5nnz4bx-yingzaofashi"
MINIO_ENDPOINT = "objectstorageapi.hzh.sealos.run"

EMBEDDING_BASE_URL = "http://10.128.202.100:3010/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_BATCH_SIZE = 10
EMBEDDING_DIM = 1024

CUSTOM_DICT_PATH = (
    PROJECT_ROOT
    / "knowledgeBase"
    / "pdfParse"
    / "cleaned_data"
    / "termsName_forPrompt.text"
)

DEFAULT_MD_PATH = (
    PROJECT_ROOT
    / "knowledgeBase"
    / "pdfParse"
    / "cleaned_data"
    / "consolidated_corpus_final.md"
)


# ── Embedding ────────────────────────────────────────────────────
def _make_openai_client() -> OpenAI:
    api_key = os.getenv("SEU_API_KEY")
    if not api_key:
        print("[ERROR] 环境变量 SEU_API_KEY 未设置")
        sys.exit(1)
    return OpenAI(api_key=api_key, base_url=EMBEDDING_BASE_URL)


def _l2_normalize(vec: list[float]) -> list[float]:
    a = np.array(vec, dtype=np.float32)
    norm = np.linalg.norm(a)
    if norm > 0:
        a = a / norm
    return a.tolist()


def batch_embed(texts: list[str], oai: OpenAI) -> list[list[float] | None]:
    """批量调用 embedding API，返回与 texts 等长的向量列表（失败项为 None）。"""
    results: list[list[float] | None] = [None] * len(texts)
    for i in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        batch = texts[i : i + EMBEDDING_BATCH_SIZE]
        try:
            resp = oai.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            for j, item in enumerate(resp.data):
                results[i + j] = _l2_normalize(item.embedding)
        except Exception as e:
            print(f"  [WARN] embedding 批次 {i}~{i+len(batch)-1} 失败: {e}")
        if i + EMBEDDING_BATCH_SIZE < len(texts):
            time.sleep(0.1)
    return results


# ── jieba 分词 → ts_vector 用字符串 ─────────────────────────────
_jieba_loaded = False


def _ensure_jieba():
    global _jieba_loaded
    if _jieba_loaded:
        return
    if CUSTOM_DICT_PATH.exists():
        jieba.load_userdict(str(CUSTOM_DICT_PATH))
        print(f"  jieba 自定义词表已加载: {CUSTOM_DICT_PATH.name}")
    else:
        print(f"  [WARN] 自定义词表不存在: {CUSTOM_DICT_PATH}")
    _jieba_loaded = True


def jieba_tokenize(text: str) -> str:
    """返回空格分隔的分词字符串，供 to_tsvector('simple', ...) 使用。"""
    _ensure_jieba()
    words = jieba.lcut(text)
    return " ".join(w for w in words if w.strip())


# ── Minio 上传 ──────────────────────────────────────────────────
def upload_image(local_path: str, images_base_dir: Path) -> str:
    """上传图片到 Minio，返回公开访问 URL。文件不存在或上传失败返回空字符串。"""
    p = Path(local_path)
    if not p.exists():
        print(f"  [WARN] 图片文件不存在，跳过上传: {p}")
        return ""
    try:
        rel = p.relative_to(images_base_dir)
    except ValueError:
        rel = Path(p.name)
    object_name = f"images/{rel.as_posix()}"
    content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    try:
        minio_client.fput_object(
            MINIO_BUCKET, object_name, str(p), content_type=content_type
        )
        return f"https://{MINIO_ENDPOINT}/{MINIO_BUCKET}/{object_name}"
    except Exception as e:
        print(f"  [WARN] 上传失败 {p.name}: {e}")
        return ""


# ── 向量转 Postgres 字符串 ───────────────────────────────────────
def vec_to_pg(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


# ── DB 写入 ──────────────────────────────────────────────────────
SQL_ENSURE_DOC = """
INSERT INTO documents (id, name)
VALUES (%s::uuid, %s)
ON CONFLICT (id) DO NOTHING;
"""

SQL_TEXT_CHUNK = """
INSERT INTO text_chunks (
    chunk_id, content_type, main_text, book_id,
    closest_title, toc_path, search_text, chunk_size,
    ts_vector, embedding_values
) VALUES (
    %s::uuid, %s, %s, %s::uuid,
    %s, %s, %s, %s,
    to_tsvector('simple', %s), %s::vector
);
"""

SQL_TEXT_CHUNK_NO_VEC = """
INSERT INTO text_chunks (
    chunk_id, content_type, main_text, book_id,
    closest_title, toc_path, search_text, chunk_size,
    ts_vector
) VALUES (
    %s::uuid, %s, %s, %s::uuid,
    %s, %s, %s, %s,
    to_tsvector('simple', %s)
);
"""

SQL_IMAGE_CHUNK_NO_VEC = """
INSERT INTO image_chunks (
    image_id, title, image_uri, local_path,
    book_id, closest_title, toc_path, search_text,
    ts_vector
) VALUES (
    %s::uuid, %s, %s, %s,
    %s::uuid, %s, %s, %s,
    to_tsvector('simple', %s)
);
"""

SQL_RELATION = """
INSERT INTO relations (
    relation_id, source_type, source_id,
    target_type, target_id, relation_type
) VALUES (
    %s::uuid, %s, %s::uuid, %s, %s::uuid, %s
);
"""


def _write_text_chunks(
    cur,
    text_chunks: list[TempTextChunk],
    note_chunks: list[TempNoteChunk],
    embeddings: list[list[float] | None],
):
    """将正文 + 注释 chunk 写入 text_chunks 表。"""
    all_chunks: list[TempTextChunk | TempNoteChunk] = list(text_chunks) + list(note_chunks)
    for idx, c in enumerate(all_chunks):
        st = build_search_text(c.toc_path, c.main_text)
        ts = jieba_tokenize(st)
        vec = vec_to_pg(embeddings[idx])
        if vec is not None:
            cur.execute(
                SQL_TEXT_CHUNK,
                (
                    c.chunk_id, c.content_type, c.main_text, BOOK_ID,
                    c.closest_title, c.toc_path, st, len(st),
                    ts, vec,
                ),
            )
        else:
            cur.execute(
                SQL_TEXT_CHUNK_NO_VEC,
                (
                    c.chunk_id, c.content_type, c.main_text, BOOK_ID,
                    c.closest_title, c.toc_path, st, len(st),
                    ts,
                ),
            )


def _write_image_chunks(
    cur,
    image_chunks: list[TempImageChunk],
    image_uris: list[str],
):
    """将图片 chunk 写入 image_chunks 表（不写入 embedding）。"""
    for idx, c in enumerate(image_chunks):
        st = c.title
        ts = jieba_tokenize(st)
        uri = image_uris[idx]
        cur.execute(
            SQL_IMAGE_CHUNK_NO_VEC,
            (
                c.image_id, c.title, uri, c.local_path,
                BOOK_ID, c.closest_title, c.toc_path, st,
                ts,
            ),
        )


def _write_relations(cur, relations: list[dict]):
    for r in relations:
        cur.execute(
            SQL_RELATION,
            (
                r["relation_id"], r["source_type"], r["source_id"],
                r["target_type"], r["target_id"], r["relation_type"],
            ),
        )


# ── 主流程 ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MD 分块入库脚本")
    parser.add_argument("md_file", nargs="?", default=str(DEFAULT_MD_PATH),
                        help="待处理的 MD 文件路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅解析并打印统计，不写入数据库和对象存储")
    args = parser.parse_args()

    md_path = Path(args.md_file)
    if not md_path.exists():
        print(f"[ERROR] 文件不存在: {md_path}")
        sys.exit(1)

    # ── 1. 解析 MD ────────────────────────────────────────────
    print(f"[1/4] 解析 MD 文件: {md_path.name}")
    md_text = md_path.read_text(encoding="utf-8")
    md_dir = md_path.parent
    text_chunks, image_chunks, note_chunks = parse_md(md_text, md_dir)
    relations, warnings = build_relations(text_chunks, note_chunks, image_chunks)

    print(f"  正文 chunks: {len(text_chunks)}")
    print(f"  注释 chunks: {len(note_chunks)}")
    print(f"  图片 chunks: {len(image_chunks)}")
    print(f"  关系记录:    {len(relations)}")
    for w in warnings:
        print(f"  [WARN] {w}")

    if args.dry_run:
        print("\n[dry-run] 解析完成，未写入数据库。")
        return

    # ── 2. 生成文本 embedding（图片不再生成 embedding）────────
    print("\n[2/4] 生成文本 embedding 向量")
    oai = _make_openai_client()

    all_text_objs: list[TempTextChunk | TempNoteChunk] = list(text_chunks) + list(note_chunks)
    text_search_texts = [build_search_text(c.toc_path, c.main_text) for c in all_text_objs]

    print(f"  文本 embedding: {len(text_search_texts)} 条 ...")
    text_embeddings = batch_embed(text_search_texts, oai)
    ok_text = sum(1 for v in text_embeddings if v is not None)
    print(f"  文本 embedding 完成: {ok_text}/{len(text_search_texts)}")

    # ── 3. 上传图片到 Minio ───────────────────────────────────
    print(f"\n[3/4] 上传图片到 Minio ({MINIO_BUCKET})")
    images_base_dir = md_dir / "images_all"
    image_uris: list[str] = []
    for i, ic in enumerate(image_chunks):
        uri = upload_image(ic.local_path, images_base_dir)
        image_uris.append(uri)
        status = "OK" if uri else "SKIP"
        print(f"  [{i+1}/{len(image_chunks)}] {status} {Path(ic.local_path).name}")

    # ── 4. 写入数据库 ────────────────────────────────────────
    print("\n[4/4] jieba 分词初始化 & 写入 PostgreSQL")
    _ensure_jieba()
    conn = get_connection()
    cur = conn.cursor()
    try:
        # 5a. 确保 documents 记录存在
        print("  确认 documents 记录 ...")
        cur.execute(SQL_ENSURE_DOC, (BOOK_ID, BOOK_NAME))

        # 5b. 写入 text_chunks
        print(f"  写入 text_chunks ({len(all_text_objs)} 条) ...")
        _write_text_chunks(cur, text_chunks, note_chunks, text_embeddings)

        # 5c. 写入 image_chunks
        print(f"  写入 image_chunks ({len(image_chunks)} 条) ...")
        _write_image_chunks(cur, image_chunks, image_uris)

        # 5d. 写入 relations
        print(f"  写入 relations ({len(relations)} 条) ...")
        _write_relations(cur, relations)

        conn.commit()
        print("\n  全部写入成功，事务已提交。")
    except Exception as e:
        conn.rollback()
        print(f"\n  [ERROR] 写入失败，已回滚: {e}")
        raise
    finally:
        cur.close()
        release_connection(conn)

    # ── 汇总 ──────────────────────────────────────────────────
    print("\n===== 入库完成 =====")
    print(f"  text_chunks  : {len(all_text_objs)} 条 (正文 {len(text_chunks)}, 注释 {len(note_chunks)})")
    print(f"  image_chunks : {len(image_chunks)} 条")
    print(f"  relations    : {len(relations)} 条")
    emb_fail = sum(1 for v in text_embeddings if v is None)
    if emb_fail:
        print(f"  [WARN] {emb_fail} 条文本 embedding 生成失败，对应字段为 NULL")


if __name__ == "__main__":
    main()
