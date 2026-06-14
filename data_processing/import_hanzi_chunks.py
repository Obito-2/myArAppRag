"""将 rare_hanzi_integrated.jsonl 中的生僻字数据批量写入 PostgreSQL chunks 表。

向量从本地已保存的 vectors.npy / doc_ids.npy 中加载（源自 FAISS 索引），
无需重复调用 embedding API。
"""

from __future__ import annotations

import json
import re

import jieba
import numpy as np
import psycopg2
from psycopg2.extras import execute_values

DSN = "postgresql://postgres:lchgjt88@dbconn.sealoshzh.site:41987/postgres"
DOCUMENT_ID = "8387d4b1-75cf-4a23-94d3-9533bb5be758"

JSONL_PATH = r"knowledgeBase\pdfParse\cleaned_data\rare_hanzi_integrated.jsonl"
VECTORS_PATH = r"knowledgeBase\chunks\vectors.npy"
DOC_IDS_PATH = r"knowledgeBase\chunks\doc_ids.npy"
USERDICT_PATH = r"knowledgeBase\pdfParse\cleaned_data\termsName_forPrompt.text"


def load_jsonl(path: str) -> list[dict]:
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def build_content(obj: dict) -> str:
    hanzi = obj.get("汉字", "")
    duyin = obj.get("读音", "")
    chaizi = obj.get("拆字", [])
    jieshi = obj.get("解释", "")
    chuxian_shuyu = obj.get("出现在术语", [])

    chaizi_str = "、".join(chaizi) if chaizi else ""
    chuxian_shuyu_str = "、".join(chuxian_shuyu) if chuxian_shuyu else ""

    parts: list[str] = []
    if hanzi:
        parts.append(f"'{hanzi}'字")
    if duyin:
        parts.append(f"其为读音：{duyin}")
    if chaizi_str:
        parts.append(f"可以拆字表示为：{chaizi_str}")
    if jieshi:
        parts.append(f"意思是：{jieshi}")
    if chuxian_shuyu_str:
        parts.append(f"出现在术语：{chuxian_shuyu_str}")

    return "；".join(parts)


def build_metadata(obj: dict) -> dict:
    metadata: dict = {}
    for key in ("UNICODE", "替代字", "字形相似汉字"):
        if key in obj:
            metadata[key] = obj[key]
    return metadata


def extract_url(data_source: str | None) -> str | None:
    if not data_source:
        return None
    match = re.search(r"https?://\S+", data_source)
    return match.group(0) if match else None


def build_tsvector_str(text: str) -> str:
    cleaned = re.sub(r"['\\]", "", text)
    words = [w.strip() for w in jieba.cut(cleaned) if w.strip()]
    unique_words = list(dict.fromkeys(words))
    return " ".join(unique_words)


def build_embedding_str(vector: np.ndarray) -> str:
    return "[" + ",".join(map(str, vector.tolist())) + "]"


def main() -> None:
    jieba.load_userdict(USERDICT_PATH)
    print(f"已加载自定义词典: {USERDICT_PATH}")

    records = load_jsonl(JSONL_PATH)
    print(f"JSONL 记录数: {len(records)}")

    vectors = np.load(VECTORS_PATH)
    doc_ids = np.load(DOC_IDS_PATH, allow_pickle=True)
    unicode_to_vector: dict[str, np.ndarray] = {
        doc_ids[i]: vectors[i] for i in range(len(doc_ids))
    }
    print(f"向量数: {len(unicode_to_vector)}，维度: {vectors.shape[1]}")

    rows: list[tuple] = []
    skipped: list[str] = []

    for obj in records:
        unicode_val = obj.get("UNICODE", "")

        if unicode_val not in unicode_to_vector:
            skipped.append(f"{obj.get('汉字', '?')} ({unicode_val})")
            continue

        content = build_content(obj)
        metadata = build_metadata(obj)
        embedding_str = build_embedding_str(unicode_to_vector[unicode_val])
        ts_str = build_tsvector_str(content)

        url = extract_url(obj.get("数据来源"))
        annotation = [url] if url else None
        has_annotation = annotation is not None

        rows.append((
            DOCUMENT_ID,
            content,
            json.dumps(metadata, ensure_ascii=False),
            embedding_str,
            ts_str,
            "注解",
            None,
            False,
            has_annotation,
            annotation,
        ))

    if skipped:
        print(f"\n跳过 {len(skipped)} 条（无对应向量）:")
        for s in skipped:
            print(f"  - {s}")

    print(f"\n准备写入 {len(rows)} 条 chunks ...")

    conn = psycopg2.connect(DSN)
    try:
        with conn:
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO chunks (
                        document_id, content, metadata, embedding, ts_vector,
                        content_type, toc_path, has_images, has_annotation, annotation
                    ) VALUES %s
                    """,
                    rows,
                    template=(
                        "(%s, %s, %s::jsonb, %s::vector, %s::tsvector,"
                        " %s, %s, %s, %s, %s)"
                    ),
                )
                print(f"成功写入 {len(rows)} 条 chunks")

                cur.execute(
                    "UPDATE documents SET chunks_count = %s WHERE id = %s",
                    (len(rows), DOCUMENT_ID),
                )
                print(f"已更新 documents.chunks_count = {len(rows)}")

                cur.execute(
                    "SELECT count(*) FROM chunks WHERE document_id = %s",
                    (DOCUMENT_ID,),
                )
                actual = cur.fetchone()[0]
                print(f"验证: chunks 表中 document_id={DOCUMENT_ID} 共 {actual} 条")
    finally:
        conn.close()

    print("\n导入完成!")


if __name__ == "__main__":
    main()
