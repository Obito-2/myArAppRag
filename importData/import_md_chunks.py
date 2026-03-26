#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
两本营造法式 MD 文件 → 分块 / 元数据抽取 / embedding / 图片上传 → PostgreSQL 入库

用法:
  python import_md_chunks.py --test --dry-run
  python import_md_chunks.py --test
  python import_md_chunks.py --batch
  python import_md_chunks.py --file <path> --book <key>
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

import jieba
import numpy as np
from openai import OpenAI

sys.path.insert(0, str(PROJECT_ROOT))
from tools.bucket_load import client as minio_client
from tools.db_connect import get_connection, release_connection

# ── 书籍配置 ─────────────────────────────────────────────────────

BOOKS = {
    "liangzhu": {
        "book_id": "4ced9d70-e725-4312-9988-d3acd79d878b",
        "book_name": "梁思成注释《营造法式》",
        "md_dir": PROJECT_ROOT / "knowledgeBase" / "liangzhu_unzip" / "OEBPS" / "Markdown_2chunk",
        "images_dir": PROJECT_ROOT / "knowledgeBase" / "liangzhu_unzip" / "OEBPS" / "Images",
        "minio_prefix": "liangzhu",
        "test_file": "Ssc08_0001.md",
    },
    "wangzhu": {
        "book_id": "7a1746cb-bacb-4d7d-b122-ca72ea52c02e",
        "book_name": "王贵祥译注《营造法式》",
        "md_dir": PROJECT_ROOT / "knowledgeBase" / "wangzhu_unzip" / "OEBPS" / "Markdown_Output",
        "images_dir": PROJECT_ROOT / "knowledgeBase" / "wangzhu_unzip" / "OEBPS" / "images",
        "minio_prefix": "wangzhu",
        "test_file": "041.md",
    },
}

# ── 常量 ─────────────────────────────────────────────────────────

MINIO_BUCKET = "q5nnz4bx-yingzaofashi"
MINIO_ENDPOINT = "objectstorageapi.hzh.sealos.run"

EMBEDDING_BASE_URL = "http://10.128.202.100:3010/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_BATCH_SIZE = 10

CUSTOM_DICT_PATH = (
    PROJECT_ROOT / "knowledgeBase" / "pdfParse" / "cleaned_data" / "termsName_forPrompt.text"
)

MAX_CHUNK_SIZE = 300

CONTENT_TYPE_MAP = {
    "【原文】": "original_text",
    "【梁注】": "annotation",
    "【译文】": "modern_translation",
    "【题解】": "interpretation",
    "知识小链接": "others_text",
}

CONTENT_TYPE_LABELS = {
    "original_text": "原文",
    "annotation": "注释",
    "modern_translation": "译文",
    "interpretation": "解读",
    "others_text": "其他文本",
}

# ── 正则 ─────────────────────────────────────────────────────────

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)")
RARE_CHAR_IMG_RE = re.compile(r"!\[生僻字[^\]]*\]\([^)]+\)")
IMAGE_LINE_RE = re.compile(r"^!\[(.*?)\]\(([^)]+)\)(.*)")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)")
FOOTNOTE_REF_RE = re.compile(r"\[\^(\d+)\]")

# ── 数据类 ───────────────────────────────────────────────────────


@dataclass
class TempTextChunk:
    chunk_id: str
    content_type: str
    main_text: str
    closest_title: str
    toc_path: list[str]
    note_refs: list[str] = field(default_factory=list)
    scope_id: int = 0


@dataclass
class TempNoteChunk:
    chunk_id: str
    content_type: str
    main_text: str
    closest_title: str
    toc_path: list[str]
    note_key: str
    scope_id: int = 0


@dataclass
class TempImageChunk:
    image_id: str
    title: str
    local_path: str
    closest_title: str
    toc_path: list[str]
    caption: str = ""


# ── 内容类型检测 ─────────────────────────────────────────────────


def detect_content_type(line: str) -> str | None:
    stripped = line.strip()
    for marker, ct in CONTENT_TYPE_MAP.items():
        if stripped == f"**{marker}**":
            return ct
    return None


# ── 段落聚合 → 分块 ─────────────────────────────────────────────


def paragraphs_to_chunks(
    paragraphs: list[str],
    content_type: str,
    closest_title: str,
    toc_path: list[str],
    scope_id: int,
) -> list[TempTextChunk]:
    if not paragraphs:
        return []

    chunks: list[TempTextChunk] = []
    buf: list[str] = []
    buf_len = 0

    def _flush():
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n\n".join(buf)
        refs = [f"s{scope_id}_{n}" for n in FOOTNOTE_REF_RE.findall(text)]
        chunks.append(
            TempTextChunk(
                chunk_id=str(uuid.uuid4()),
                content_type=content_type,
                main_text=text,
                closest_title=closest_title,
                toc_path=list(toc_path),
                note_refs=refs,
                scope_id=scope_id,
            )
        )
        buf = []
        buf_len = 0

    for para in paragraphs:
        plen = len(para)
        new_len = buf_len + (2 if buf else 0) + plen
        if buf and new_len > MAX_CHUNK_SIZE:
            _flush()
            buf = [para]
            buf_len = plen
        else:
            buf.append(para)
            buf_len = new_len

    _flush()
    return chunks


# ── MD 解析（两本书通用） ────────────────────────────────────────


def parse_md_file(
    md_path: Path,
) -> tuple[list[TempTextChunk], list[TempImageChunk], list[TempNoteChunk]]:
    md_text = md_path.read_text(encoding="utf-8")
    md_dir = md_path.parent
    lines = md_text.split("\n")

    text_chunks: list[TempTextChunk] = []
    image_chunks: list[TempImageChunk] = []
    note_chunks: list[TempNoteChunk] = []

    heading_stack: list[tuple[int, str]] = []
    content_type = "others_text"
    in_note_mode = False
    scope_id = 0
    was_in_note_or_translation = False

    para_lines: list[str] = []
    section_paras: list[str] = []
    sec_title = ""
    sec_toc: list[str] = []

    cur_note_key: str | None = None
    cur_note_lines: list[str] = []

    def _closest():
        return heading_stack[-1][1] if heading_stack else ""

    def _toc():
        return [h[1] for h in heading_stack]

    def _update_stack(level: int, title: str):
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))

    def _finish_para():
        nonlocal para_lines
        if para_lines:
            t = "\n".join(para_lines).strip()
            if t:
                section_paras.append(t)
            para_lines = []

    def _flush_section():
        nonlocal section_paras
        if section_paras:
            text_chunks.extend(
                paragraphs_to_chunks(
                    section_paras, content_type, sec_title, sec_toc, scope_id
                )
            )
            section_paras = []

    def _finish_note():
        nonlocal cur_note_key, cur_note_lines
        if cur_note_key and cur_note_lines:
            body = "\n".join(cur_note_lines).strip()
            if body:
                note_chunks.append(
                    TempNoteChunk(
                        chunk_id=str(uuid.uuid4()),
                        content_type="annotation",
                        main_text=body,
                        closest_title=_closest(),
                        toc_path=_toc(),
                        note_key=f"s{scope_id}_{cur_note_key}",
                        scope_id=scope_id,
                    )
                )
        cur_note_key = None
        cur_note_lines = []

    for raw in lines:
        line = RARE_CHAR_IMG_RE.sub("[生僻字符]", raw)
        stripped = line.strip()

        # ① 标题行
        hm = HEADING_RE.match(stripped)
        if hm:
            level = len(hm.group(1))
            title_text = hm.group(2).strip()

            if in_note_mode:
                _finish_note()
                in_note_mode = False

            _finish_para()
            _flush_section()

            if was_in_note_or_translation:
                scope_id += 1
                was_in_note_or_translation = False

            _update_stack(level, title_text)
            sec_title = _closest()
            sec_toc = _toc()
            content_type = "others_text"
            continue

        # ② 内容类型标记行
        ct = detect_content_type(stripped)
        if ct is not None:
            if in_note_mode:
                _finish_note()
                in_note_mode = False

            _finish_para()
            _flush_section()

            content_type = ct

            if ct == "annotation":
                in_note_mode = True
                was_in_note_or_translation = True
            elif ct == "modern_translation":
                was_in_note_or_translation = True
            else:
                in_note_mode = False
            continue

        # ③ 注释模式：解析脚注定义
        if in_note_mode:
            fn = FOOTNOTE_DEF_RE.match(stripped)
            if fn:
                _finish_note()
                cur_note_key = fn.group(1)
                first_line = fn.group(2).strip()
                cur_note_lines = [first_line] if first_line else []
            elif stripped and cur_note_key:
                cur_note_lines.append(stripped)
            continue

        # ④ 图片行
        im = IMAGE_LINE_RE.match(stripped)
        if im:
            alt_text = im.group(1)
            img_path = im.group(2)
            caption = im.group(3).strip()

            _finish_para()

            abs_path = str((md_dir / img_path).resolve()) if img_path else ""
            image_chunks.append(
                TempImageChunk(
                    image_id=str(uuid.uuid4()),
                    title=alt_text,
                    local_path=abs_path,
                    closest_title=sec_title or _closest(),
                    toc_path=sec_toc or _toc(),
                    caption=caption,
                )
            )
            continue

        # ⑤ 空行 → 段落边界
        if not stripped:
            _finish_para()
            continue

        # ⑥ 普通文本行
        para_lines.append(stripped)

    # ── 收尾 ──────────────────────────────────────────────────
    if in_note_mode:
        _finish_note()
    else:
        _finish_para()
        _flush_section()

    return text_chunks, image_chunks, note_chunks


# ── 构建 relations ───────────────────────────────────────────────


def build_relations(
    text_chunks: list[TempTextChunk],
    note_chunks: list[TempNoteChunk],
) -> tuple[list[dict], list[str]]:
    note_map = {n.note_key: n.chunk_id for n in note_chunks}

    relations: list[dict] = []
    warnings: list[str] = []

    for ch in text_chunks:
        seen: set[str] = set()
        for nk in ch.note_refs:
            tid = note_map.get(nk)
            if tid and tid not in seen:
                seen.add(tid)
                relations.append(
                    {
                        "relation_id": str(uuid.uuid4()),
                        "source_type": ch.content_type,
                        "source_id": ch.chunk_id,
                        "target_type": "annotation",
                        "target_id": tid,
                        "relation_type": "annotates",
                    }
                )
            elif not tid:
                warnings.append(
                    f"悬空注释引用: {nk} (chunk {ch.chunk_id[:8]}…)"
                )

    return relations, warnings


# ── search_text ──────────────────────────────────────────────────


def build_search_text(closest_title: str, content_type: str, main_text: str) -> str:
    label = CONTENT_TYPE_LABELS.get(content_type, "")
    parts = [p for p in [closest_title, label, main_text] if p]
    return " ".join(parts)


def build_image_search_text(closest_title: str, title: str) -> str:
    parts = [p for p in [closest_title, title] if p]
    return " ".join(parts)


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


# ── jieba 分词 ───────────────────────────────────────────────────

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
    _ensure_jieba()
    words = jieba.lcut(text)
    return " ".join(w for w in words if w.strip())


# ── Minio 上传 ──────────────────────────────────────────────────


def upload_image(local_path: str, minio_prefix: str) -> str:
    p = Path(local_path)
    if not p.exists():
        print(f"  [WARN] 图片文件不存在，跳过: {p.name}")
        return ""
    object_name = f"{minio_prefix}/{p.name}"
    content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    try:
        minio_client.fput_object(
            MINIO_BUCKET, object_name, str(p), content_type=content_type
        )
        return f"https://{MINIO_ENDPOINT}/{MINIO_BUCKET}/{object_name}"
    except Exception as e:
        print(f"  [WARN] 上传失败 {p.name}: {e}")
        return ""


# ── 向量 → PG 字符串 ────────────────────────────────────────────


def vec_to_pg(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


# ── SQL ──────────────────────────────────────────────────────────

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

SQL_IMAGE_CHUNK = """
INSERT INTO image_chunks (
    image_id, title, image_uri, local_path,
    book_id, closest_title, toc_path, search_text,
    ts_vector, caption
) VALUES (
    %s::uuid, %s, %s, %s,
    %s::uuid, %s, %s, %s,
    to_tsvector('simple', %s), %s
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

# ── DB 写入 ──────────────────────────────────────────────────────


def write_to_db(
    book_id: str,
    book_name: str,
    text_chunks: list[TempTextChunk],
    note_chunks: list[TempNoteChunk],
    image_chunks: list[TempImageChunk],
    relations: list[dict],
    text_embeddings: list[list[float] | None],
    image_uris: list[str],
):
    _ensure_jieba()
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(SQL_ENSURE_DOC, (book_id, book_name))

        all_text: list[TempTextChunk | TempNoteChunk] = list(text_chunks) + list(
            note_chunks
        )
        for idx, c in enumerate(all_text):
            st = build_search_text(c.closest_title, c.content_type, c.main_text)
            ts = jieba_tokenize(st)
            vec = vec_to_pg(text_embeddings[idx])
            if vec is not None:
                cur.execute(
                    SQL_TEXT_CHUNK,
                    (
                        c.chunk_id, c.content_type, c.main_text, book_id,
                        c.closest_title, c.toc_path, st, len(st),
                        ts, vec,
                    ),
                )
            else:
                cur.execute(
                    SQL_TEXT_CHUNK_NO_VEC,
                    (
                        c.chunk_id, c.content_type, c.main_text, book_id,
                        c.closest_title, c.toc_path, st, len(st),
                        ts,
                    ),
                )

        for idx, c in enumerate(image_chunks):
            st = build_image_search_text(c.closest_title, c.title)
            ts = jieba_tokenize(st)
            uri = image_uris[idx]
            cur.execute(
                SQL_IMAGE_CHUNK,
                (
                    c.image_id, c.title, uri, c.local_path,
                    book_id, c.closest_title, c.toc_path, st,
                    ts, c.caption,
                ),
            )

        for r in relations:
            cur.execute(
                SQL_RELATION,
                (
                    r["relation_id"], r["source_type"], r["source_id"],
                    r["target_type"], r["target_id"], r["relation_type"],
                ),
            )

        conn.commit()
        print("  事务提交成功")
    except Exception as e:
        conn.rollback()
        print(f"  [ERROR] 写入失败，已回滚: {e}")
        raise
    finally:
        cur.close()
        release_connection(conn)


# ── 处理单个文件 ─────────────────────────────────────────────────


def process_file(md_path: Path, book_config: dict, dry_run: bool = False):
    book_id = book_config["book_id"]
    book_name = book_config["book_name"]
    minio_prefix = book_config["minio_prefix"]

    print(f"\n{'=' * 60}")
    print(f"处理文件: {md_path.name} ({book_name})")
    print(f"{'=' * 60}")

    # ── 1. 解析 MD ────────────────────────────────────────────
    print("[1/4] 解析 MD 文件")
    text_chunks, image_chunks, note_chunks = parse_md_file(md_path)
    relations, warnings = build_relations(text_chunks, note_chunks)

    all_text_objs: list[TempTextChunk | TempNoteChunk] = list(text_chunks) + list(
        note_chunks
    )

    print(f"  正文 chunks: {len(text_chunks)}")
    print(f"  注释 chunks: {len(note_chunks)}")
    print(f"  图片 chunks: {len(image_chunks)}")
    print(f"  关系记录:    {len(relations)}")
    for w in warnings:
        print(f"  [WARN] {w}")

    if dry_run:
        if text_chunks:
            c = text_chunks[0]
            print(f"\n  --- 示例 text_chunk ---")
            print(f"  content_type : {c.content_type}")
            print(f"  closest_title: {c.closest_title}")
            print(f"  toc_path     : {c.toc_path}")
            print(f"  scope_id     : {c.scope_id}")
            preview = c.main_text[:120].replace("\n", "\\n")
            print(f"  main_text    : {preview}…")
        if note_chunks:
            c = note_chunks[0]
            print(f"\n  --- 示例 note_chunk ---")
            print(f"  note_key     : {c.note_key}")
            print(f"  scope_id     : {c.scope_id}")
            preview = c.main_text[:120].replace("\n", "\\n")
            print(f"  main_text    : {preview}…")
        if image_chunks:
            c = image_chunks[0]
            print(f"\n  --- 示例 image_chunk ---")
            print(f"  title        : {c.title}")
            print(f"  caption      : {c.caption or '(无)'}")
            print(f"  local_path   : {c.local_path}")
        if relations:
            r = relations[0]
            print(f"\n  --- 示例 relation ---")
            print(f"  {r['source_type']}({r['source_id'][:8]}…) → "
                  f"{r['target_type']}({r['target_id'][:8]}…)  [{r['relation_type']}]")
        print("\n[dry-run] 解析完成，未写入数据库。")
        return

    # ── 2. Embedding ──────────────────────────────────────────
    print("\n[2/4] 生成文本 embedding")
    oai = _make_openai_client()
    search_texts = [
        build_search_text(c.closest_title, c.content_type, c.main_text)
        for c in all_text_objs
    ]
    print(f"  共 {len(search_texts)} 条文本 ...")
    text_embeddings = batch_embed(search_texts, oai)
    ok = sum(1 for v in text_embeddings if v is not None)
    print(f"  embedding 完成: {ok}/{len(search_texts)}")

    # ── 3. 上传图片 ──────────────────────────────────────────
    print(f"\n[3/4] 上传图片到 Minio ({minio_prefix}/)")
    image_uris: list[str] = []
    for i, ic in enumerate(image_chunks):
        uri = upload_image(ic.local_path, minio_prefix)
        image_uris.append(uri)
        status = "OK" if uri else "SKIP"
        print(f"  [{i + 1}/{len(image_chunks)}] {status} {Path(ic.local_path).name}")

    # ── 4. 写入数据库 ────────────────────────────────────────
    print(f"\n[4/4] 写入 PostgreSQL")
    write_to_db(
        book_id, book_name,
        text_chunks, note_chunks, image_chunks,
        relations, text_embeddings, image_uris,
    )

    print(f"\n===== 入库完成 =====")
    print(f"  text_chunks : {len(all_text_objs)} (正文 {len(text_chunks)}, 注释 {len(note_chunks)})")
    print(f"  image_chunks: {len(image_chunks)}")
    print(f"  relations   : {len(relations)}")
    emb_fail = sum(1 for v in text_embeddings if v is None)
    if emb_fail:
        print(f"  [WARN] {emb_fail} 条 embedding 生成失败（字段为 NULL）")


# ── 主入口 ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="营造法式 MD 分块入库")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--test", action="store_true",
                       help="测试模式：每本书处理一个文件")
    group.add_argument("--batch", action="store_true",
                       help="批量模式：处理所有文件")
    group.add_argument("--file", type=str,
                       help="指定单个 MD 文件路径")
    parser.add_argument("--book", type=str, choices=list(BOOKS.keys()),
                        help="指定书籍 key（与 --file 配合）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅解析不写入")
    args = parser.parse_args()

    if args.file:
        if not args.book:
            print("[ERROR] --file 需要搭配 --book 使用")
            sys.exit(1)
        md_path = Path(args.file)
        if not md_path.exists():
            print(f"[ERROR] 文件不存在: {md_path}")
            sys.exit(1)
        process_file(md_path, BOOKS[args.book], dry_run=args.dry_run)
    elif args.test:
        for key, cfg in BOOKS.items():
            md_path = cfg["md_dir"] / cfg["test_file"]
            if not md_path.exists():
                print(f"[WARN] 测试文件不存在: {md_path}")
                continue
            process_file(md_path, cfg, dry_run=args.dry_run)
    elif args.batch:
        for key, cfg in BOOKS.items():
            md_dir = cfg["md_dir"]
            md_files = sorted(md_dir.glob("*.md"))
            print(f"\n{'#' * 60}")
            print(f"批量处理: {cfg['book_name']} ({len(md_files)} 个文件)")
            print(f"{'#' * 60}")
            for md_path in md_files:
                process_file(md_path, cfg, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
