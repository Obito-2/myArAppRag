#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
两本营造法式 MD 文件 → 分块 / 元数据抽取 / embedding / 图片上传 → PostgreSQL 入库

用法:
  python import_md_chunks.py --test --dry-run
  python import_md_chunks.py --test --dry-run --sample-seed 42
  python import_md_chunks.py --test --test-output-dir D:/out
  python import_md_chunks.py --test
  python import_md_chunks.py --batch
  python import_md_chunks.py --file <path> --book <key>
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import random
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

# 注意：不在此处 import db_connect。该模块会在加载时创建连接池并立即连库；
# 延迟到 write_to_db 再导入，这样 --dry-run 仅解析时无需数据库可用。

# ── 书籍配置 ─────────────────────────────────────────────────────

BOOKS = {
    "liangzhu": {
        "book_id": "4ced9d70-e725-4312-9988-d3acd79d878b",
        "book_name": "梁思成注释《营造法式》",
        "md_dir": PROJECT_ROOT / "knowledgeBase" / "md_to_chunk" / "liangzhu_Markdown_2chunk",
        "images_dir": PROJECT_ROOT / "knowledgeBase" / "liangzhu_unzip" / "OEBPS" / "Images",
        "minio_prefix": "liangzhu",
        "test_file": "Ssc08_0001.md",
    },
    "wangzhu": {
        "book_id": "7a1746cb-bacb-4d7d-b122-ca72ea52c02e",
        "book_name": "王贵祥译注《营造法式》",
        "md_dir": PROJECT_ROOT / "knowledgeBase" / "md_to_chunk" / "wangzhu_Markdown_Output",
        "images_dir": PROJECT_ROOT / "knowledgeBase" / "wangzhu_unzip" / "OEBPS" / "images",
        "minio_prefix": "wangzhu",
        "test_file": "025.md",
    },
}

# ── 常量 ─────────────────────────────────────────────────────────

MINIO_BUCKET = "q5nnz4bx-yingzaofashi-rag"
MINIO_ENDPOINT = "https://objectstorageapi.hzh.sealos.run"

EMBEDDING_BASE_URL = "http://10.128.202.100:3010/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_BATCH_SIZE = 10

CUSTOM_DICT_PATH = (
    PROJECT_ROOT / "knowledgeBase" / "pdfParse" / "cleaned_data" / "termsName_forPrompt.text"
)

MAX_CHUNK_SIZE = 300

CONTENT_TYPE_MAP = {
    #梁思成注释《营造法式》
    "【原文】": "original_text",
    "【梁注】": "annotation",
    "【译文】": "modern_translation",
    "知识小链接": "others_text",

    #王贵祥译注《营造法式》
    "【题解】": "interpretation",
    "【注释】": "annotation",

}

CONTENT_TYPE_LABELS = {
    "original_text": "原文",
    "annotation": "注释",
    "modern_translation": "译文",
    "interpretation": "解读",
    "others_text": "其他文本",
}

# ── 正则 ─────────────────────────────────────────────────────────

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)")  # 标题行
# 生僻字插图统一成占位，避免被 IMAGE_LINE_RE 当成普通图片解析
RARE_CHAR_IMG_RE = re.compile(r"!\[生僻字[^\]]*\]\([^)]+\)")
IMAGE_LINE_RE = re.compile(r"^!\[(.*?)\]\(([^)]+)\)(.*)")  # 图片行
FOOTNOTE_DEF_RE = re.compile(r"^\[\^(\d+)\]:\s*(.*)") # 注释定义
FOOTNOTE_REF_RE = re.compile(r"\[\^(\d+)\]") # 注释引用

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


@dataclass
class TestBookParse:
    """--test 模式下单本书的解析结果，用于汇总写入 Excel。"""

    book_key: str
    book_name: str
    md_path: Path
    text_chunks: list[TempTextChunk]
    image_chunks: list[TempImageChunk]
    note_chunks: list[TempNoteChunk]
    relations: list[dict]
    warnings: list[str]


# parse_md_file + build_relations 的一次性结果，供 process_file 复用避免重复解析
PreparsedBundle = tuple[
    list[TempTextChunk],
    list[TempImageChunk],
    list[TempNoteChunk],
    list[dict],
    list[str],
]


# ── 内容类型检测 ─────────────────────────────────────────────────

def detect_content_type(line: str) -> str | None:
    """
    根据行内容，检测内容类型
    """
    stripped = line.strip()
    for marker, ct in CONTENT_TYPE_MAP.items():
        if stripped == f"**{marker}**":
            return ct
    # 整行形如 **标签** 但不在映射中：按 others_text 处理并提示（避免普通正文误触发）
    m = re.match(r"^\*\*(.+)\*\*$", stripped)
    if m and "**" not in m.group(1):
        print(
            f"[import_md_chunks] 未识别的内容类型标记，已按 others_text 处理: {m.group(1)!r}",
            file=sys.stderr,
        )
        return "others_text"
    return None


# ── 段落聚合 → 分块 ─────────────────────────────────────────────


def paragraphs_to_chunks(
    paragraphs: list[str],
    content_type: str,
    closest_title: str,
    toc_path: list[str],
    scope_id: int,
) -> list[TempTextChunk]:
    """
    将段落列表按 MAX_CHUNK_SIZE（字符数）合并成若干 text chunk。

    策略：顺序向缓冲区追加段落，段落之间用双换行（join 时等价于 \\n\\n）连接；若加入
    下一段会超过上限则先输出当前缓冲区，再从该段重新开始。单个段落长于上限时不截断，
    整段独占一块。

    参数与 parse_md_file 中当前「小节」一致：content_type / closest_title / toc_path /
    scope_id 会原样写入 TempTextChunk；note_refs 由正文中的 [^n] 与 scope_id 拼出。
    """
    if not paragraphs:
        return []

    chunks: list[TempTextChunk] = []
    buf: list[str] = []  # 待合并为一条 chunk 的段落序列
    buf_len = 0  # 与 "\n\n".join(buf) 的 len 一致，供与 new_len 比较

    def _flush():
        """将 buf 打成一条 TempTextChunk，并清空缓冲区。"""
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n\n".join(buf)
        # 与 build_relations 中 note_key 规则一致：s{scope_id}_{脚注号}
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
        # 非首段时多加 2 个字符：上一段与当前段之间的 "\\n\\n"
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
    """
    单行扫描解析 MD，产出三类块：正文 text、插图 image、脚注定义 note。

    结构层次：
    - 标题（#…）：维护 heading_stack，得到最近标题与目录路径；并结束当前段落、将已收集的
      正文段落交给 paragraphs_to_chunks。若上一区块为注释/译文，则 scope_id 自增，用于区分
      不同章节的 [^1] 等同号脚注。
    - 整行 **【…】**（见 CONTENT_TYPE_MAP）：切换当前小节的 content_type；若为「注释」则进入
      in_note_mode，后续行按脚注定义解析，不进入 section_paras。
    - 空行：结束一个「段落」（para），多个段落聚成 section_paras，在换标题/换类型时 flush。
    - 普通行：在注释模式下写入脚注正文；否则累积到 para_lines，遇空行或边界再落成段落。

    sec_title / sec_toc 在每次遇到标题时与栈同步，供其后正文与图片块挂 closest_title、tocs。
    """
    md_text = md_path.read_text(encoding="utf-8")
    md_dir = md_path.parent
    lines = md_text.split("\n")

    text_chunks: list[TempTextChunk] = []
    image_chunks: list[TempImageChunk] = []
    note_chunks: list[TempNoteChunk] = []

    # 标题层级栈：(level, 标题文本)，用于 closest_title / toc_path
    heading_stack: list[tuple[int, str]] = []
    # 当前小节类型（**【原文】** 等映射结果，见 detect_content_type）
    content_type = "others_text"
    # True：正在读 **【注释】** 后的脚注定义区 [^n]: …
    in_note_mode = False
    # 脚注命名空间：与正文中的 s{scope_id}_n 对应，换「注释/译文」后的标题时递增
    scope_id = 0
    # 刚从注释块或译文块出来，下一标题需 bump scope_id
    was_in_note_or_translation = False

    # 当前段落（空行闭合）；多段组成一个小节，再交给 paragraphs_to_chunks
    para_lines: list[str] = []
    section_paras: list[str] = []
    # 最近一次标题行更新后的「最近标题」与目录路径（与栈一致）
    sec_title = ""
    sec_toc: list[str] = []

    # 脚注定义 [^n]: 第一行正文 + 续行
    cur_note_key: str | None = None
    cur_note_lines: list[str] = []

    def _closest():
        """当前最近一级标题文本（栈顶）。"""
        return heading_stack[-1][1] if heading_stack else ""

    def _toc():
        """从根到当前的标题路径，用于 toc_path。"""
        return [h[1] for h in heading_stack]

    def _update_stack(level: int, title: str):
        """Markdown 标题：弹出同级及更深层级，再压入本标题。"""
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))

    def _finish_para():
        """将 para_lines 合成一条段落追加到 section_paras（空行触发或边界前调用）。"""
        nonlocal para_lines
        if para_lines:
            t = "\n".join(para_lines).strip()
            if t:
                section_paras.append(t)
            para_lines = []

    def _flush_section():
        """把当前小节已收集的段落交给 paragraphs_to_chunks，追加到 text_chunks。"""
        nonlocal section_paras
        if section_paras:
            text_chunks.extend(
                paragraphs_to_chunks(
                    section_paras, content_type, sec_title, sec_toc, scope_id
                )
            )
            section_paras = []

    def _finish_note():
        """结束一条脚注定义，写入 note_chunks（note_key = s{scope_id}_{n}）。"""
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

        # ① 标题行：先收尾脚注/段落/小节，再 bump scope、更新栈与 sec_*
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

        # ② 内容类型标记行（**【原文】** 等）：切换 content_type，注释则进入脚注解析分支
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

        # ③ 注释模式：仅识别 [^n]: 与续行，不参与正文段落
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

        # ④ 图片行：单独成块，不写入 section_paras
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
                    # 图片可能夹在段落中间：优先用本小节冻结的 sec_*，否则回退栈
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

        # ⑥ 普通文本行（多行合并为一个段落，直到空行）
        para_lines.append(stripped)

    # 文件末尾：若在脚注区只收尾脚注；否则把最后一个段落与小节 flush 进 text_chunks
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


def _toc_join(toc: list[str]) -> str:
    return " > ".join(toc)


def _truncate_excel_cell(s: str, max_len: int = 30000) -> str:
    """Excel 单格长度有限，过长字段截断。"""
    if len(s) <= max_len:
        return s
    return s[: max_len - 24] + "\n...(内容过长已截断)"


def write_test_parse_excel(
    output_dir: Path,
    books: list[TestBookParse],
    filename: str | None = None,
) -> Path | None:
    """
    将测试 MD 的解析结果写入 xlsx，仅三张表（与入库一致的三类数据）：
    - 文本块：全部正文 TempTextChunk + 全部脚注块 TempNoteChunk（chunk_role 区分）
    - 图片：全部 TempImageChunk
    - 关系：全部 relations

    依赖 openpyxl：pip install openpyxl
    """
    if not books:
        print("[WARN] 无有效测试解析结果，跳过 Excel 导出")
        return None

    try:
        from openpyxl import Workbook
    except ImportError:
        print("[ERROR] 导出 Excel 需要安装 openpyxl：pip install openpyxl")
        return None

    from datetime import datetime

    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not filename:
        filename = f"test_parse_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    out_path = output_dir / filename

    text_rows: list[dict] = []
    image_rows: list[dict] = []
    rel_rows: list[dict] = []

    for b in books:
        base = {
            "book_key": b.book_key,
            "book_name": b.book_name,
            "md_file": b.md_path.name,
        }
        for c in b.text_chunks:
            text_rows.append(
                {
                    **base,
                    "chunk_role": "body",
                    "chunk_id": c.chunk_id,
                    "content_type": c.content_type,
                    "closest_title": c.closest_title,
                    "toc_path": _toc_join(c.toc_path),
                    "scope_id": c.scope_id,
                    "note_refs": ", ".join(c.note_refs),
                    "note_key": "",
                    "main_text": _truncate_excel_cell(c.main_text),
                }
            )
        for c in b.note_chunks:
            text_rows.append(
                {
                    **base,
                    "chunk_role": "footnote",
                    "chunk_id": c.chunk_id,
                    "content_type": c.content_type,
                    "closest_title": c.closest_title,
                    "toc_path": _toc_join(c.toc_path),
                    "scope_id": c.scope_id,
                    "note_refs": "",
                    "note_key": c.note_key,
                    "main_text": _truncate_excel_cell(c.main_text),
                }
            )
        for c in b.image_chunks:
            image_rows.append(
                {
                    **base,
                    "image_id": c.image_id,
                    "title": c.title,
                    "caption": c.caption,
                    "closest_title": c.closest_title,
                    "toc_path": _toc_join(c.toc_path),
                    "local_path": c.local_path,
                }
            )
        for r in b.relations:
            rel_rows.append(
                {
                    **base,
                    "relation_id": r["relation_id"],
                    "source_type": r["source_type"],
                    "source_id": r["source_id"],
                    "target_type": r["target_type"],
                    "target_id": r["target_id"],
                    "relation_type": r["relation_type"],
                }
            )

    wb = Workbook()
    h_text = [
        "book_key",
        "book_name",
        "md_file",
        "chunk_role",
        "chunk_id",
        "content_type",
        "closest_title",
        "toc_path",
        "scope_id",
        "note_refs",
        "note_key",
        "main_text",
    ]
    h_image = [
        "book_key",
        "book_name",
        "md_file",
        "image_id",
        "title",
        "caption",
        "closest_title",
        "toc_path",
        "local_path",
    ]
    h_rel = [
        "book_key",
        "book_name",
        "md_file",
        "relation_id",
        "source_type",
        "source_id",
        "target_type",
        "target_id",
        "relation_type",
    ]

    sheets: list[tuple[str, list[str], list[dict]]] = [
        ("text_chunks", h_text, text_rows),
        ("image_chunks", h_image, image_rows),
        ("relations", h_rel, rel_rows),
    ]
    for i, (name, headers, rows) in enumerate(sheets):
        if i == 0:
            ws = wb.active
            assert ws is not None
            ws.title = name
        else:
            ws = wb.create_sheet(title=name)
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h, "") for h in headers])

    wb.save(out_path)
    return out_path


# ── search_text ──────────────────────────────────────────────────


def build_search_text(closest_title: str, content_type: str, main_text: str) -> str:
    """
    构建 search_text, 拼接逻辑：[closest_title]+[CONTENT_TYPE]+[main_text] 
    """
    label = CONTENT_TYPE_LABELS.get(content_type, "")
    parts = [p for p in [closest_title, label, main_text] if p]
    return " ".join(parts)


def build_image_search_text(closest_title: str, title: str) -> str:
    """
    构建 image_search_text, 拼接逻辑：[closest_title]+[title] 
    """
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
    from tools.db_connect import get_connection, release_connection

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


def process_file(
    md_path: Path,
    book_config: dict,
    dry_run: bool = False,
    sample_seed: int | None = None,
    preparsed: PreparsedBundle | None = None,
):
    book_id = book_config["book_id"]
    book_name = book_config["book_name"]
    minio_prefix = book_config["minio_prefix"]

    print(f"\n{'=' * 60}")
    print(f"处理文件: {md_path.name} ({book_name})")
    print(f"{'=' * 60}")

    # ── 1. 解析 MD ────────────────────────────────────────────
    print("[1/4] 解析 MD 文件")
    if preparsed is not None:
        text_chunks, image_chunks, note_chunks, relations, warnings = preparsed
    else:
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
        seed = sample_seed if sample_seed is not None else random.randrange(2**31)
        random.seed(seed)
        print(f"\n  [dry-run] 示例随机种子: {seed}（复现请加 --sample-seed {seed}）")

        if text_chunks:
            c = random.choice(text_chunks)
            print(f"\n  --- 示例 text_chunk ---")
            print(f"  content_type : {c.content_type}")
            print(f"  closest_title: {c.closest_title}")
            print(f"  toc_path     : {c.toc_path}")
            print(f"  scope_id     : {c.scope_id}")
            preview = c.main_text.replace("\n", "\\n")
            print(f"  main_text    : {preview}")
        if note_chunks:
            c = random.choice(note_chunks)
            print(f"\n  --- 示例 note_chunk ---")
            print(f"  note_key     : {c.note_key}")
            print(f"  scope_id     : {c.scope_id}")
            preview = c.main_text.replace("\n", "\\n")
            print(f"  main_text    : {preview}")
        if image_chunks:
            c = random.choice(image_chunks)
            print(f"\n  --- 示例 image_chunk ---")
            print(f"  title        : {c.title}")
            print(f"  caption      : {c.caption or '(无)'}")
            print(f"  local_path   : {c.local_path}")
        if relations:
            r = random.choice(relations)
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
    parser.add_argument(
        "--sample-seed",
        type=int,
        default=None,
        metavar="N",
        help="dry-run 时各列表示例的随机种子（默认每次随机；用于复现同一条示例）",
    )
    parser.add_argument(
        "--test-output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="仅 --test：解析结果 xlsx 输出目录（默认项目下 importData/output）",
    )
    parser.add_argument(
        "--test-excel-name",
        type=str,
        default=None,
        metavar="NAME",
        help="仅 --test：xlsx 文件名（默认 test_parse_时间戳.xlsx）",
    )
    args = parser.parse_args()

    if args.file:
        if not args.book:
            print("[ERROR] --file 需要搭配 --book 使用")
            sys.exit(1)
        md_path = Path(args.file)
        if not md_path.exists():
            print(f"[ERROR] 文件不存在: {md_path}")
            sys.exit(1)
        process_file(
            md_path, BOOKS[args.book],
            dry_run=args.dry_run,
            sample_seed=args.sample_seed,
        )
    elif args.test:
        out_dir = args.test_output_dir
        if out_dir is None:
            out_dir = PROJECT_ROOT / "importData" / "output"
        else:
            out_dir = Path(out_dir).expanduser()

        test_runs: list[tuple[dict, Path, PreparsedBundle]] = []
        parsed_books: list[TestBookParse] = []

        for key, cfg in BOOKS.items():
            md_path = cfg["md_dir"] / cfg["test_file"]
            if not md_path.exists():
                print(f"[WARN] 测试文件不存在: {md_path}")
                continue
            text_chunks, image_chunks, note_chunks = parse_md_file(md_path)
            relations, warnings = build_relations(text_chunks, note_chunks)
            bundle: PreparsedBundle = (
                text_chunks,
                image_chunks,
                note_chunks,
                relations,
                warnings,
            )
            parsed_books.append(
                TestBookParse(
                    book_key=key,
                    book_name=cfg["book_name"],
                    md_path=md_path,
                    text_chunks=text_chunks,
                    image_chunks=image_chunks,
                    note_chunks=note_chunks,
                    relations=relations,
                    warnings=warnings,
                )
            )
            test_runs.append((cfg, md_path, bundle))

        excel_path = write_test_parse_excel(
            out_dir,
            parsed_books,
            filename=args.test_excel_name,
        )
        if excel_path is not None:
            print(f"\n[--test] 解析结果已写入: {excel_path}")

        for cfg, md_path, bundle in test_runs:
            process_file(
                md_path,
                cfg,
                dry_run=args.dry_run,
                sample_seed=args.sample_seed,
                preparsed=bundle,
            )
    elif args.batch:
        for key, cfg in BOOKS.items():
            md_dir = cfg["md_dir"]
            md_files = sorted(md_dir.glob("*.md"))
            print(f"\n{'#' * 60}")
            print(f"批量处理: {cfg['book_name']} ({len(md_files)} 个文件)")
            print(f"{'#' * 60}")
            for md_path in md_files:
                process_file(
                    md_path, cfg,
                    dry_run=args.dry_run,
                    sample_seed=args.sample_seed,
                )


if __name__ == "__main__":
    main()
