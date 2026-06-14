# myArAppRag

基于 RAG（检索增强生成）的智能问答应用——数据端，负责将原始书籍 Markdown 文件处理为规范化语料并入库。

## 项目结构

```
myArAppRag/
├── .env                              # 环境变量配置
├── .gitignore                        # Git 忽略规则
├── data_processing/                  # 数据处理（核心管道 + 过程性尝试）
├── docs/                             # 项目文档与说明
├── output/                           # 数据产出文件
├── tools/                            # 数据库与存储工具脚本
└── knowledgeBase/                    # 原始知识库数据（书籍 MD 文件）
```

## 目录说明

### 📁 data_processing/ — 数据处理

核心 Python 管道脚本与过程性 Jupyter Notebook，涵盖从原始 MD 清洗到最终入库的完整流程。

#### 核心管道脚本（.py）

| 文件 | 说明 |
|------|------|
| `corpus_normalize.py` | 语料清洗：去噪、规范化章节编号、图片语法简化、注释块转换 |
| `split_md_to_jsonl.py` | MD 分块与元数据抽取：按章节切分，输出 `book_documents.jsonl`、`corpus.jsonl`、`image_analysis.jsonl` |
| `import_md_chunks.py` | Chunk 导入：将语料信息导入 `documents` / `text_chunks` / `image_chunks` 表，并填充 embedding 向量 |
| `import_md_to_db.py` | Documents 入库：将书籍文档信息写入 `documents` 表 |
| `import_hanzi_to_db.py` | 汉字术语数据入库 |
| `import_hanzi_chunks.py` | 汉字 Chunk 导入 |

#### 过程性 Notebook（.ipynb）

| 文件 | 说明 |
|------|------|
| `strip_rare_char_md_images.ipynb` | MD 数据清洗：去除生僻字符、分离图片 |
| `md2chunks_v2.ipynb` | 文本分割成 Chunk（v2 版本，当前推荐） |
| `md2chunks.ipynb` | 文本分割成 Chunk（v1 版本） |
| `image_analysis.ipynb` | 图片信息处理：提取元数据、调用阿里云 OCR 识别图片文本 |
| `dataCleaning.ipynb` | 数据清洗（旧方法） |
| `extract_md_headings.ipynb` | 提取 MD 文件标题层级 |
| `check_md_paragraph_breaks.ipynb` | 检查 MD 段落切分结果 |
| `hanzi_import_db.ipynb` | 汉字术语数据入库（Notebook 版本） |
| `professional_terminology_db.ipynb` | 专业术语数据库整理分析 |
| `jiansuo.ipynb` | 检索功能测试 |
| `langChain.ipynb` | LangChain 框架使用示例 |
| `use_marker_rag.ipynb` | Marker RAG 框架使用示例 |
| `SEU_aliyunOCR.ipynb` | 阿里云 OCR 接口调用 |
| `statisticTermClean.ipynb` | 专业术语清洗统计 |

### 📁 docs/ — 项目文档

| 文件 | 说明 |
|------|------|
| `data_backend.md` | 数据后端流程总览与文件说明 |
| `table_shcema.md` | 数据库表结构说明 |
| `split_get_meta.md` | Marker 模型拆分 MD 文件的格式建议 |
| `import_hanzi_term.md` | 汉字术语导入说明 |
| `book_documents.jsonl` | 书籍文档信息示例 |

### 📁 output/ — 数据产出

加工后的中间/最终数据文件：

| 文件 | 说明 |
|------|------|
| `text_chunks.jsonl` | 文本 Chunk 数据 |
| `image_chunks.jsonl` | 图片 Chunk 数据 |
| `image_analysis.jsonl` | 图片分析结果（含 OCR 识别文本、元数据） |
| `relations.jsonl` | Chunk 关系数据 |
| `text_chunks_samples.xlsx` | 文本 Chunk 抽样导出 |
| `test_parse_*.xlsx` | 解析测试结果 |
| `db_snapshot_*/` | 数据库快照备份 |

### 📁 tools/ — 工具脚本

数据库管理与云存储操作工具：

| 文件 | 说明 |
|------|------|
| `db_connect.py` | 数据库连接池工具 |
| `upload_images_to_bucket.py` | 上传图片到阿里云 OSS |
| `bucket_load.py` | Minio 客户端（云存储交互） |
| `clean_DB.py` | 数据库重置/清空 |
| `fill_image_uri_from_local_path.py` | 根据本地路径填充 `image_analysis.jsonl` 中的 `image_uri` |
| `insert_documents_from_jsonl_lines.py` | 指定行插入 documents 表 |
| `count_book_chunks_and_relations.py` | 统计指定书籍的 chunk 与 relation 数量 |
| `delete_book_chunks_and_relations.py` | 删除指定书籍的 chunk 与 relation 数据 |
| `delete_chunks_relations_keep_documents.py` | 删除 chunks + relations，保留 documents |
| `delete_documents_no_chunks.py` | 删除无 chunk 的空文档记录 |
| `export_text_chunks_sample.py` | 导出文本 chunk 样本用于检查 |
| `list_documents_chunk_counts.py` | 列出所有 documents 的 chunk 数量 |
| `remove_field.py` | 批量移除 JSONL 中指定字段 |

## 数据处理流程

```
原始 MD 文件
    │
    ▼
① 语料清洗 (corpus_normalize.py / strip_rare_char_md_images.ipynb)
    ├── 去除异常字符，保留规范文本
    ├── 分离图片，提取图片元数据
    └── 规范化章节编号、注释格式
    │
    ▼
② 分块与元数据抽取 (split_md_to_jsonl.py / md2chunks_v2.ipynb)
    ├── 按章节/段落切分为 chunk
    ├── 输出 corpus.jsonl（文本 chunk）
    ├── 输出 image_analysis.jsonl（图片信息）
    └── 输出 book_documents.jsonl（文档信息）
    │
    ▼
③ 图片处理 (upload_images_to_bucket.py + image_analysis.ipynb)
    ├── 上传图片到阿里云 OSS
    ├── 调用 OCR 识别图片文字
    └── 填充 image_uri 字段
    │
    ▼
④ 数据入库 (import_md_chunks.py / import_md_to_db.py)
    ├── 写入 documents 表
    ├── 写入 text_chunks / image_chunks 表
    ├── 填充 embedding 向量
    └── 写入 relations 表
```

## 技术栈

- **语言**: Python 3.11
- **框架**: FastAPI 0.135.1
- **数据库**: PostgreSQL 16.4（含 pgvector 扩展）
- **LLM**: LangChain + OpenAI Client
- **OCR**: 阿里云 OCR / Marker（阿里达摩院开源模型）
- **云存储**: 阿里云 OSS / Minio

## 数据库表

涉及 4 张核心表：

| 表名 | 说明 |
|------|------|
| `documents` | 书籍文档信息 |
| `text_chunks` | 文本片段（含 embedding 向量） |
| `image_chunks` | 图片片段（含 OCR 文本、图像 URI） |
| `relations` | Chunk 关系（如插图关系） |

详细字段说明见 `docs/data_backend.md` 和 `docs/table_shcema.md`。