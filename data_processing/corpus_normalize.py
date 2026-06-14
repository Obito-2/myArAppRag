from __future__ import annotations

import re
from pathlib import Path

CN_DIGITS = "一二三四五六七八九十"

H1_CHAPTER = re.compile(r"^# 第([一二三四五六七八九十]+)章\s", re.MULTILINE)
FIG_TABLE_LINE = re.compile(r"^(图|附图|附表|表)\s?\d+")
ITEM_NOTE = re.compile(r"^(\s*)-\s*\((\d+)\)\s*(.*)$")
SUB_ITEM_DASH = re.compile(r"^\s*-\s+")


def chinese_to_int(cn: str) -> int:
    if len(cn) == 1 and cn in CN_DIGITS:
        return CN_DIGITS.index(cn) + 1
    raise ValueError(f"unsupported chapter numeral: {cn!r}")


def int_to_chinese(n: int) -> str:
    if 1 <= n <= 10:
        return CN_DIGITS[n - 1]
    raise ValueError(f"unsupported chapter num: {n}")


def replace_sup_in_body(text: str, ch_num: int) -> str:
    t = re.sub(
        r"<sup>\((\d+)\)</sup>",
        lambda m: f"[^ch{ch_num}-{m.group(1)}]",
        text,
    )
    t = re.sub(
        r"<sup>(\d+)</sup>",
        lambda m: f"[^ch{ch_num}-{m.group(1)}]",
        t,
    )
    return t


def notes_heading_pattern(ch_num: int) -> re.Pattern:
    cn = int_to_chinese(ch_num)
    return re.compile(rf"^##\s*第{cn}章注释\s*$", re.MULTILINE)


def split_body_and_notes(chapter_text: str, ch_num: int) -> tuple[str, str | None]:
    pat = notes_heading_pattern(ch_num)
    m = pat.search(chapter_text)
    if not m:
        return chapter_text, None
    return chapter_text[: m.start()], chapter_text[m.end() :]


def transform_notes_block(notes_text: str, ch_num: int) -> str:
    lines = notes_text.split("\n")
    chunks: list[tuple[int, list[str]]] = []
    current_k: int | None = None
    buf: list[str] = []
    prefix: list[str] = []

    def flush() -> None:
        nonlocal current_k, buf
        if current_k is not None:
            chunks.append((current_k, buf))
        current_k = None
        buf = []

    for line in lines:
        im = ITEM_NOTE.match(line)
        if im:
            flush()
            current_k = int(im.group(2))
            buf = [im.group(3)]
            continue
        if current_k is not None:
            if SUB_ITEM_DASH.match(line) and not ITEM_NOTE.match(line):
                line = SUB_ITEM_DASH.sub("", line, count=1)
            buf.append(line)
        else:
            prefix.append(line)

    flush()

    if not chunks:
        return notes_text

    foot_blocks = []
    for k, parts in chunks:
        body = "\n".join(parts).strip()
        foot_blocks.append(f"[^ch{ch_num}-{k}]: {body}")

    mid = "\n\n".join(foot_blocks)
    if prefix:
        pre = "\n".join(prefix).rstrip()
        return pre + "\n\n" + mid + "\n"
    return mid + "\n"


def process_chapter_segment(segment: str, ch_num: int) -> str:
    body, notes = split_body_and_notes(segment, ch_num)
    if notes is None:
        return segment
    body2 = replace_sup_in_body(body, ch_num)
    notes2 = transform_notes_block(notes, ch_num)
    return body2 + f"## 第{int_to_chinese(ch_num)}章注释\n\n" + notes2


def split_document(text: str) -> list[tuple[str | int, str]]:
    matches = list(H1_CHAPTER.finditer(text))
    if not matches:
        return [("preamble", text)]

    out: list[tuple[str | int, str]] = []
    if matches[0].start() > 0:
        out.append(("preamble", text[: matches[0].start()]))

    for i, m in enumerate(matches):
        cn = m.group(1)
        ch_num = chinese_to_int(cn)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((ch_num, text[start:end]))
    return out


def _image_inner_to_path(inner: str) -> str:
    """清理路径：移除末尾的 "title" 部分，保留路径。"""
    inner = inner.strip()
    # 移除被双引号包裹的标题部分
    inner = re.sub(r'\s+"[^"]*"\s*$', "", inner)
    return inner


def _find_image_close_paren(s: str, open_idx: int) -> int:
    """从 s[open_idx]=='(' 起，找到与 Markdown 图片语法配对的 ')'（忽略双引号内字符）。"""
    i = open_idx + 1
    in_dquote = False
    while i < len(s):
        c = s[i]
        if c == "\\" and in_dquote:
            i += 2
            continue
        if c == '"':
            in_dquote = not in_dquote
        elif c == ")" and not in_dquote:
            return i
        i += 1
    return -1

def simplify_image_markdown(text: str) -> str:
    out: list[str] = []
    pos = 0
    while pos < len(text):
        start = text.find("![", pos)
        if start < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:start])
        mid = text.find("](", start)
        if mid < 0:
            out.append(text[start:])
            break
        # 提取 alt 文本（位于 "![" 和 "](" 之间）
        alt = text[start+2:mid]
        open_paren = mid + 1
        close_paren = _find_image_close_paren(text, open_paren)
        if close_paren < 0:
            out.append(text[start:])
            break
        inner = text[open_paren + 1 : close_paren]
        path = _image_inner_to_path(inner)
        # 保留 alt，去除 title
        out.append(f"![{alt}]({path})")
        pos = close_paren + 1
    return "".join(out)


def strip_figure_caption_lines(text: str) -> str:
    """删除图片前独立的图题行。"""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # 如果当前行是以“图/表”开头的
        if stripped and FIG_TABLE_LINE.match(stripped):
            j = i + 1
            # 跳过空行查找下一行非空行
            while j < len(lines) and not lines[j].strip():
                j += 1
            # 如果下一行是非空的图片标签，则跳过当前“图/表”行（即删除它）
            if j < len(lines) and lines[j].strip().startswith("!["):
                i = j
                continue
        out.append(line)
        i += 1
    return "\n".join(out)


def normalize_corpus(text: str) -> str:
    parts = split_document(text)
    rebuilt: list[str] = []
    for key, seg in parts:
        if key == "preamble":
            rebuilt.append(seg)
        else:
            assert isinstance(key, int)
            rebuilt.append(process_chapter_segment(seg, key))
    text2 = "".join(rebuilt)
    
    # 1. 先删除独立的图题行
    text2 = strip_figure_caption_lines(text2)
    # 2. 再简化图片语法（保留 Alt 标题，去 title）
    text2 = simplify_image_markdown(text2)
    return text2


def run_file(input_path: Path, output_path: Path) -> None:
    text = input_path.read_text(encoding="utf-8")
    out = normalize_corpus(text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(out, encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    # 根据你的实际路径调整
    inp = root / "knowledgeBase" / "pdfParse" / "cleaned_data" / "consolidated_corpus_3.md"
    outp = root / "knowledgeBase" / "pdfParse" / "cleaned_data" / "consolidated_corpus_3.3.md"
    
    if inp.exists():
        run_file(inp, outp)
        print(f"成功处理并写入: {outp}")
    else:
        print(f"找不到输入文件: {inp}")