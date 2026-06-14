#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生僻字和术语导入脚本

功能：
1. 导入生僻字库 (rare_hanzi_integrated.jsonl) -> text_chunks
2. 导入术语库 (yingzaofashi_jiedu_v2_term_enhanced.jsonl) -> text_chunks
3. 建立术语与生僻字的关联关系 (relations)

用法：
  python import_hanzi_to_db.py [--dry-run]
  python import_hanzi_to_db.py --hanzi-only
  python import_hanzi_to_db.py --term-only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import jieba
import numpy as np
from openai import OpenAI
import uuid

# -- 项目路径 --
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.db_connect import get_connection, release_connection

# -- 常量 --
# Book IDs
BOOK_ID_HANZI = "e6b410cb-99e9-44c4-8c40-5445779ca8e3"  # 生僻字库
BOOK_ID_TERM = "45a4f3d7-a3da-4b53-959a-72ee245f2f18"    # 术语库

# Book Names
BOOK_NAME_HANZI = "《法式》生僻字库"
BOOK_NAME_TERM = "《法式》术语简要"

# 文件路径
HANZI_FILE = PROJECT_ROOT / "knowledgeBase/pdfParse/cleaned_data/rare_hanzi_integrated.jsonl"
TERM_FILE = PROJECT_ROOT / "knowledgeBase/pdfParse/cleaned_data/yingzaofashi_jiedu_v2_term_enhanced.jsonl"

# Embedding 配置
EMBEDDING_BASE_URL = "http://10.128.202.100:3010/v1"
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_BATCH_SIZE = 10

# jieba 自定义词表
CUSTOM_DICT_PATH = (
    PROJECT_ROOT
    / "knowledgeBase"
    / "pdfParse"
    / "cleaned_data"
    / "termsName_forPrompt.text"
)


# -- 数据模型 --
class HanziRecord:
    """生僻字记录"""
    def __init__(self, data: dict):
        self.汉字 = data.get("汉字", "")
        self.UNICODE = data.get("UNICODE", "")
        self.读音 = data.get("读音", "")
        self.拆字 = data.get("拆字", [])
        self.解释 = data.get("解释", "")
        self.替代字 = data.get("替代字")
        self.字形相似汉字 = data.get("字形相似汉字", [])
        self.数据来源 = data.get("数据来源", "")
        self.出现在术语 = data.get("出现在术语", [])
        # 用于追踪 chunk_id
        self.chunk_id = None

    def build_main_text(self) -> str:
        # 拆字描述
        拆字_str = "、".join(self.拆字) if self.拆字 else "无"
        
        # 出现在术语
        术语_str = "、".join(self.出现在术语) if self.出现在术语 else "无"
        
        # 使用字符串拼接避免引号问题
        result = '汉字"' + self.汉字 + '" 读音是"' + self.读音 + '" 可以拆字描述为"' + 拆字_str + '" 其意思是：' + self.解释
        if 术语_str != "无":
            result = result + "出现在术语" + 术语_str + "中"
        else:
            result = result + "。"
        
        return result

    def build_metadata(self) -> dict:
        return {
            "汉字": self.汉字,  # 添加汉字字段以便后续关联查询
            "UNICODE": self.UNICODE,
            "读音": self.读音,
            "拆字": self.拆字,
            "替代字": self.替代字,
            "字形相似汉字": self.字形相似汉字,
            "数据来源": self.数据来源,
            "出现在术语": self.出现在术语,
        }


class TermRecord:
    """术语记录"""
    def __init__(self, data: dict):
        self.术语 = data.get("术语", "")
        self.解释 = data.get("解释", "")
        self.首出处卷 = data.get("首出处卷", "")
        self.读音 = data.get("读音", [])
        self.少见字注解 = data.get("少见字注解")

    def build_main_text(self) -> str:
        读音_str = "、".join(self.读音) if self.读音 else "无"
        return '"' + self.术语 + '" 其意思是：' + self.解释 + '首次出现在：' + self.首出处卷 + ' 其完整读音为：' + 读音_str 

    def build_metadata(self) -> dict:
        return {
            "首出处卷": self.首出处卷,
            "读音": self.读音,
            "少见字注解": self.少见字注解,
        }

    def extract_rare_chars(self) -> list[str]:
        if not self.少见字注解:
            return []
        
        rare_chars = []
        for annotation in self.少见字注解:
            match = re.match(r'^([\u4e00-\u9fff]+)', annotation)
            if match:
                char = match.group(1)
                if char and char != self.术语:
                    rare_chars.append(char)
        
        return rare_chars


# -- Embedding --
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
    results = [None] * len(texts)
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


# -- jieba 分词 --
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


# -- 向量转换 --
def vec_to_pg(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"


# -- 数据加载 --
def load_hanzi_records() -> list[HanziRecord]:
    records = []
    with open(HANZI_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                records.append(HanziRecord(data))
    return records


def load_term_records() -> list[TermRecord]:
    records = []
    with open(TERM_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                data = json.loads(line)
                if data.get("术语"):
                    records.append(TermRecord(data))
    return records


# -- 数据库写入 --
SQL_ENSURE_DOC = """
INSERT INTO documents (id, name)
VALUES (%s::uuid, %s)
ON CONFLICT (id) DO NOTHING;
"""

SQL_TEXT_CHUNK = """
INSERT INTO text_chunks (
    chunk_id, content_type, main_text, book_id,
    closest_title, toc_path, search_text, chunk_size,
    ts_vector, embedding_values, other_metadata
) VALUES (
    %s::uuid, %s, %s, %s::uuid,
    %s, %s, %s, %s,
    to_tsvector('simple', %s), %s::vector, %s::jsonb
);
"""

SQL_TEXT_CHUNK_NO_VEC = """
INSERT INTO text_chunks (
    chunk_id, content_type, main_text, book_id,
    closest_title, toc_path, search_text, chunk_size,
    ts_vector, other_metadata
) VALUES (
    %s::uuid, %s, %s, %s::uuid,
    %s, %s, %s, %s,
    to_tsvector('simple', %s), %s::jsonb
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


def write_hanzi_chunks(conn, hanzi_records, embeddings):
    cur = conn.cursor()
    cur.execute(SQL_ENSURE_DOC, (BOOK_ID_HANZI, BOOK_NAME_HANZI))
    
    print(f"  写入 {len(hanzi_records)} 条生僻字 chunks...")
    
    for idx, record in enumerate(hanzi_records):
        main_text = record.build_main_text()
        search_text = main_text
        ts = jieba_tokenize(search_text)
        vec = vec_to_pg(embeddings[idx])
        metadata = record.build_metadata()
        chunk_id = str(uuid.uuid4())
        
        # 保存 chunk_id 到记录对象，供后续关联写入使用
        record.chunk_id = chunk_id
        
        if vec is not None:
            cur.execute(
                SQL_TEXT_CHUNK,
                (
                    chunk_id, "annotation", main_text, BOOK_ID_HANZI,
                    None, None, search_text, len(search_text),
                    ts, vec, json.dumps(metadata, ensure_ascii=False),
                ),
            )
        else:
            cur.execute(
                SQL_TEXT_CHUNK_NO_VEC,
                (
                    chunk_id, "annotation", main_text, BOOK_ID_HANZI,
                    None, None, search_text, len(search_text),
                    ts, json.dumps(metadata, ensure_ascii=False),
                ),
            )
    
    print(f"  生僻字 chunks 写入完成")


def write_term_chunks(conn, term_records, embeddings, offset=0):
    cur = conn.cursor()
    cur.execute(SQL_ENSURE_DOC, (BOOK_ID_TERM, BOOK_NAME_TERM))
    
    print(f"  写入 {len(term_records)} 条术语 chunks...")
    
    for idx, record in enumerate(term_records):
        main_text = record.build_main_text()
        search_text = main_text
        ts = jieba_tokenize(search_text)
        vec = vec_to_pg(embeddings[offset + idx])
        metadata = record.build_metadata()
        chunk_id = str(uuid.uuid4())
        
        # 保存 chunk_id 到记录对象，供后续关联写入使用
        record.chunk_id = chunk_id
        
        if vec is not None:
            cur.execute(
                SQL_TEXT_CHUNK,
                (
                    chunk_id, "interpretation", main_text, BOOK_ID_TERM,
                    None, None, search_text, len(search_text),
                    ts, vec, json.dumps(metadata, ensure_ascii=False),
                ),
            )
        else:
            cur.execute(
                SQL_TEXT_CHUNK_NO_VEC,
                (
                    chunk_id, "interpretation", main_text, BOOK_ID_TERM,
                    None, None, search_text, len(search_text),
                    ts, json.dumps(metadata, ensure_ascii=False),
                ),
            )
    
    print(f"  术语 chunks 写入完成")


def write_relations(conn, term_records, hanzi_records):
    """写入关联关系 - 使用内存中的 chunk_id"""
    cur = conn.cursor()
    
    # 构建生僻字映射表 (汉字 -> chunk_id)
    hanzi_map = {record.汉字: record.chunk_id for record in hanzi_records if record.chunk_id}
    print(f"  生僻字映射表构建完成: {len(hanzi_map)} 条")
    
    relations_count = 0
    for record in term_records:
        if not record.chunk_id:
            continue
            
        rare_chars = record.extract_rare_chars()
        if not rare_chars:
            continue
        
        term_chunk_id = record.chunk_id
        
        for char in rare_chars:
            if char in hanzi_map:
                hanzi_chunk_id = hanzi_map[char]
                relation_id = str(uuid.uuid4())
                cur.execute(
                    SQL_RELATION,
                    (
                        relation_id, "interpretation", term_chunk_id,
                        "annotation", hanzi_chunk_id, "annotates",
                    ),
                )
                relations_count += 1
    
    print(f"  关联关系写入完成: {relations_count} 条")


def build_hanzi_map(conn) -> dict:
    cur = conn.cursor()
    cur.execute(
        "SELECT chunk_id, other_metadata FROM text_chunks WHERE book_id = %s::uuid;",
        (BOOK_ID_HANZI,)
    )
    results = cur.fetchall()
    
    hanzi_map = {}
    for row in results:
        metadata = row["other_metadata"]
        if metadata and "汉字" in metadata:
            hanzi_map[metadata["汉字"]] = row["chunk_id"]
    
    print(f"  生僻字映射表构建完成: {len(hanzi_map)} 条")
    return hanzi_map


# -- 主流程 --
def main():
    parser = argparse.ArgumentParser(description="生僻字和术语导入脚本")
    parser.add_argument("--dry-run", action="store_true", help="仅解析并打印统计，不写入数据库")
    parser.add_argument("--hanzi-only", action="store_true", help="仅导入生僻字")
    parser.add_argument("--term-only", action="store_true", help="仅导入术语")
    args = parser.parse_args()

    # 1. 加载数据
    print("\n[1/5] 加载数据文件...")
    
    hanzi_records = []
    term_records = []
    
    if not args.term_only:
        hanzi_records = load_hanzi_records()
        print(f"  生僻字记录: {len(hanzi_records)} 条")
    
    if not args.hanzi_only:
        term_records = load_term_records()
        print(f"  术语记录: {len(term_records)} 条")
    
    if args.dry_run:
        print("\n[dry-run] 数据加载完成，未写入数据库。")
        # 测试输出几条样例
        if hanzi_records:
            print("\n--- 生僻字样例 ---")
            print(hanzi_records[0].build_main_text())
        if term_records:
            print("\n--- 术语样例 ---")
            print(term_records[0].build_main_text())
        return

    # 2. 生成 embedding
    print("\n[2/5] 生成文本 embedding 向量")
    oai = _make_openai_client()
    
    all_texts = []
    for record in hanzi_records:
        all_texts.append(record.build_main_text())
    for record in term_records:
        all_texts.append(record.build_main_text())
    
    print(f"  总文本数: {len(all_texts)} 条")
    embeddings = batch_embed(all_texts, oai)
    ok_count = sum(1 for v in embeddings if v is not None)
    print(f"  embedding 完成: {ok_count}/{len(all_texts)}")

    # 3. jieba 初始化
    print("\n[3/5] jieba 分词初始化")
    _ensure_jieba()

    # 4. 写入数据库
    print("\n[4/5] 写入 PostgreSQL")
    conn = get_connection()
    cur = conn.cursor()
    
    try:
        if hanzi_records:
            hanzi_embeddings = embeddings[:len(hanzi_records)]
            write_hanzi_chunks(conn, hanzi_records, hanzi_embeddings)
        
        if term_records:
            term_embeddings = embeddings[len(hanzi_records):]
            write_term_chunks(conn, term_records, term_embeddings)
        
        if term_records and hanzi_records:
            conn.commit()
            write_relations(conn, term_records, hanzi_records)
        
        conn.commit()
        print("\n  全部写入成功，事务已提交。")
        
    except Exception as e:
        conn.rollback()
        print(f"\n  [ERROR] 写入失败，已回滚: {e}")
        raise
    finally:
        cur.close()
        release_connection(conn)

    # 5. 汇总
    print("\n===== 入库完成 =====")
    print(f"  生僻字 chunks: {len(hanzi_records)} 条")
    print(f"  术语 chunks:   {len(term_records)} 条")
    
    emb_fail = sum(1 for v in embeddings if v is None)
    if emb_fail:
        print(f"  [WARN] {emb_fail} 条文本 embedding 生成失败")


if __name__ == "__main__":
    main()