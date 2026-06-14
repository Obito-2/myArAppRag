#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MD 分块与元数据抽取脚本

将 MD 文本按段落/注释/图片分块，抽取元数据字段，
输出 text_chunks.jsonl / image_chunks.jsonl / relations.jsonl

用法: python split_md_to_jsonl.py [md_file_path]
默认处理同目录下 test_insert.md
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ── 常量 ─────────────────────────────────────────────────────────
BOOK_ID = "73bf56ba-2d31-4fa4-8797-da1309e4d215"
BOOK_NAME = "《营造法式》解读(修订版)"
MAX_CHUNK_SIZE = 300

# ── 正则 ─────────────────────────────────────────────────────────
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)")
IMAGE_LINE_RE = re.compile(r"^!\[([^\]]*)\]\(([^)]+)\)")
NOTE_SECTION_RE = re.compile(r"^##\s*第.+章注释\s*$")
FOOTNOTE_START_RE = re.compile(r"^\[\^(ch\d+-\d+)\]:\s*(.*)")
FOOTNOTE_REF_RE = re.compile(r"\[\^(ch\d+-\d+)\]")
IMG_REF_RE = re.compile(
    r"(?:图|表|附图|附表)\s*\d+(?:-\d+)?(?:\s*[、,~～至到]\s*\d+(?:-\d+)?)*"
)
IMAGE_TITLE_ID_RE = re.compile(r"^(图|表|附图|附表)\s*(\d+(?:-\d+)?)")

CN_DIGITS = "一二三四五六七八九十"

# ── 数据类 ───────────────────────────────────────────────────────


@dataclass
class TempTextChunk:
    chunk_id: str
    content_type: str  # "interpretation" | "annotation"
    main_text: str
    closest_title: str
    toc_path: list[str]
    note_refs: list[str] = field(default_factory=list)
    img_refs: list[str] = field(default_factory=list)


@dataclass
class TempImageChunk:
    image_id: str
    title: str
    local_path: str
    closest_title: str
    toc_path: list[str]
    image_key: str  # "图 1-1" 用于映射


@dataclass
class TempNoteChunk:
    chunk_id: str
    content_type: str  # always "annotation"
    main_text: str
    closest_title: str
    toc_path: list[str]
    note_key: str  # "ch1-1" 用于映射


# ── 工具函数 ─────────────────────────────────────────────────────


def cn2arabic(s: str) -> int:
    """字符串转整数，兼容阿拉伯数字和简单中文数字"""
    try:
        return int(s)
    except ValueError:
        if len(s) == 1 and s in CN_DIGITS:
            return CN_DIGITS.index(s) + 1
        raise ValueError(f"无法转换: {s!r}")


def expand_image_indices(img_str: str) -> list[str]:
    """展开图号，支持顿号列表和范围（如 '图 1-2~1-5' -> ['图 1-2', ..., '图 1-5']）"""
    match = re.match(r"([图表]|附[图表])\s*(.+)", img_str.strip())
    if not match:
        return []
    prefix, content = match.groups()
    content = re.sub(r"[，.,和及与]", "、", content)
    content = re.sub(r"[～至到]", "~", content)

    if "、" in content:
        return [f"{prefix} {p.strip()}" for p in content.split("、")]

    if "~" in content:
        start_str, end_str = (s.strip() for s in content.split("~", 1))
        if re.search(r"[A-Za-z]$", start_str) and re.search(
            r"[A-Za-z]$", end_str
        ):
            base = start_str[:-1]
            sc, ec = start_str[-1], end_str[-1]
            return [
                f"{prefix} {base}{chr(i)}"
                for i in range(ord(sc), ord(ec) + 1)
            ]
        prefix_main = ""
        if "-" in start_str:
            parts = start_str.rsplit("-", 1)
            prefix_main = parts[0] + "-"
            start_num = parts[1]
            end_num = (
                end_str.rsplit("-", 1)[-1] if "-" in end_str else end_str
            )
        else:
            start_num, end_num = start_str, end_str
        si, ei = cn2arabic(start_num), cn2arabic(end_num)
        return [f"{prefix} {prefix_main}{i}" for i in range(si, ei + 1)]

    num = (
        cn2arabic(content)
        if "-" not in content and not re.search(r"[A-Za-z]", content)
        else content
    )
    return [f"{prefix} {num}"]


def extract_refs(text: str) -> tuple[list[str], list[str]]:
    """从文本中提取脚注引用 ID 和展开后的图引用 ID"""
    note_refs = FOOTNOTE_REF_RE.findall(text)
    img_refs: list[str] = []
    for m in IMG_REF_RE.finditer(text):
        img_refs.extend(expand_image_indices(m.group()))
    return note_refs, img_refs


def build_search_text(toc_path: list[str], body: str) -> str:
    toc = " > ".join(toc_path) if toc_path else ""
    parts = [p for p in [BOOK_NAME, toc, body] if p]
    return " ".join(parts)


# ── 段落聚合 → 分块 ─────────────────────────────────────────────


def paragraphs_to_chunks(
    paragraphs: list[str],
    closest_title: str,
    toc_path: list[str],
) -> list[TempTextChunk]:
    """将段落列表按 MAX_CHUNK_SIZE 分块，不截断单个段落"""
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
        nrefs, irefs = extract_refs(text)
        chunks.append(
            TempTextChunk(
                chunk_id=str(uuid.uuid4()),
                content_type="interpretation",
                main_text=text,
                closest_title=closest_title,
                toc_path=list(toc_path),
                note_refs=nrefs,
                img_refs=irefs,
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


# ── MD 主解析 ────────────────────────────────────────────────────


def parse_md(
    md_text: str, md_dir: Path
) -> tuple[list[TempTextChunk], list[TempImageChunk], list[TempNoteChunk]]:
    lines = md_text.split("\n")

    text_chunks: list[TempTextChunk] = []
    image_chunks: list[TempImageChunk] = []
    note_chunks: list[TempNoteChunk] = []

    heading_stack: list[tuple[int, str]] = []  # (level, title)
    in_notes = False

    para_lines: list[str] = []  # 当前段落行缓冲
    section_paras: list[str] = []  # 当前 heading 下的段落集合
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
                paragraphs_to_chunks(section_paras, sec_title, sec_toc)
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
                        note_key=cur_note_key,
                    )
                )
        cur_note_key = None
        cur_note_lines = []

    # ── 逐行扫描 ─────────────────────────────────────────────
    for raw in lines:
        stripped = raw.strip()

        # ① 标题行
        hm = HEADING_RE.match(stripped)
        if hm:
            level = len(hm.group(1))
            title_text = hm.group(2).strip()

            if in_notes:
                _finish_note()
                if NOTE_SECTION_RE.match(stripped):
                    _update_stack(level, title_text)
                    continue
                in_notes = False

            _finish_para()
            _flush_section()

            if NOTE_SECTION_RE.match(stripped):
                in_notes = True
                _update_stack(level, title_text)
                continue

            _update_stack(level, title_text)
            sec_title = _closest()
            sec_toc = _toc()
            continue

        # ② 注释区内容
        if in_notes:
            fn = FOOTNOTE_START_RE.match(stripped)
            if fn:
                _finish_note()
                cur_note_key = fn.group(1)
                cur_note_lines = [fn.group(2)]
            elif stripped and cur_note_key:
                cur_note_lines.append(stripped)
            continue

        # ③ 图片行
        im = IMAGE_LINE_RE.match(stripped)
        if im:
            _finish_para()
            alt, path = im.group(1), im.group(2)
            id_m = IMAGE_TITLE_ID_RE.match(alt)
            img_key = (
                f"{id_m.group(1)} {id_m.group(2)}" if id_m else ""
            )
            abs_path = str((md_dir / path).resolve()) if path else ""
            image_chunks.append(
                TempImageChunk(
                    image_id=str(uuid.uuid4()),
                    title=alt,
                    local_path=abs_path,
                    closest_title=sec_title or _closest(),
                    toc_path=sec_toc or _toc(),
                    image_key=img_key,
                )
            )
            continue

        # ④ 空行 → 段落边界
        if not stripped:
            _finish_para()
            continue

        # ⑤ 普通文本行
        para_lines.append(stripped)

    # ── 收尾 ──────────────────────────────────────────────────
    if in_notes:
        _finish_note()
    else:
        _finish_para()
        _flush_section()

    return text_chunks, image_chunks, note_chunks


# ── 构建 relations ───────────────────────────────────────────────


def build_relations(
    text_chunks: list[TempTextChunk],
    note_chunks: list[TempNoteChunk],
    image_chunks: list[TempImageChunk],
) -> tuple[list[dict], list[str]]:
    note_map = {n.note_key: n.chunk_id for n in note_chunks}
    img_map = {
        i.image_key: i.image_id for i in image_chunks if i.image_key
    }

    relations: list[dict] = []
    warnings: list[str] = []

    for ch in text_chunks:
        seen_targets: set[str] = set()

        for nk in ch.note_refs:
            tid = note_map.get(nk)
            if tid and tid not in seen_targets:
                seen_targets.add(tid)
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
                    f"悬空注释引用: [^{nk}] (chunk {ch.chunk_id[:8]}…)"
                )

        for ik in ch.img_refs:
            tid = img_map.get(ik)
            if tid and tid not in seen_targets:
                seen_targets.add(tid)
                relations.append(
                    {
                        "relation_id": str(uuid.uuid4()),
                        "source_type": ch.content_type,
                        "source_id": ch.chunk_id,
                        "target_type": "image",
                        "target_id": tid,
                        "relation_type": "illustrates",
                    }
                )
            elif not tid:
                warnings.append(
                    f"悬空图片引用: [{ik}] (chunk {ch.chunk_id[:8]}…)"
                )

    return relations, warnings


# ── JSONL 序列化与输出 ───────────────────────────────────────────


def text_to_dict(c: TempTextChunk | TempNoteChunk) -> dict:
    st = build_search_text(c.toc_path, c.main_text)
    return {
        "chunk_id": c.chunk_id,
        "content_type": c.content_type,
        "main_text": c.main_text,
        "book_id": BOOK_ID,
        "closest_title": c.closest_title,
        "toc_path": c.toc_path,
        "search_text": st,
        "chunk_size": len(st),
    }


def image_to_dict(c: TempImageChunk) -> dict:
    st = build_search_text(c.toc_path, c.title)
    return {
        "image_id": c.image_id,
        "title": c.title,
        "local_path": c.local_path,
        "image_uri": "",
        "book_id": BOOK_ID,
        "closest_title": c.closest_title,
        "toc_path": c.toc_path,
        "search_text": st,
    }


def write_jsonl(path: Path, records: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── 入口 ─────────────────────────────────────────────────────────


def main():
    script_dir = Path(__file__).resolve().parent

    md_path = Path(sys.argv[1]) if len(sys.argv) > 1 else script_dir / "test_insert.md"
    if not md_path.exists():
        print(f"文件不存在: {md_path}")
        sys.exit(1)

    md_text = md_path.read_text(encoding="utf-8")
    md_dir = md_path.parent

    print(f"处理文件: {md_path}")
    print(f"分块上限: {MAX_CHUNK_SIZE} 字符\n")

    text_chunks, image_chunks, note_chunks = parse_md(md_text, md_dir)
    relations, warnings = build_relations(text_chunks, note_chunks, image_chunks)

    for w in warnings:
        print(f"  [WARN] {w}")
    if warnings:
        print()

    all_text = [text_to_dict(c) for c in text_chunks]
    all_text.extend(text_to_dict(c) for c in note_chunks)
    img_records = [image_to_dict(c) for c in image_chunks]

    out_dir = script_dir / "output"
    write_jsonl(out_dir / "text_chunks.jsonl", all_text)
    write_jsonl(out_dir / "image_chunks.jsonl", img_records)
    write_jsonl(out_dir / "relations.jsonl", relations)

    print(f"  text_chunks  : {len(all_text)} 条 (正文 {len(text_chunks)}, 注释 {len(note_chunks)})")
    print(f"  image_chunks : {len(img_records)} 条")
    print(f"  relations    : {len(relations)} 条")
    print(f"\n输出目录: {out_dir}")


if __name__ == "__main__":
    main()
