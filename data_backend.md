# 数据端 × 数据库对接文档

> 面向离线数据处理管道，说明如何将清洗/向量化后的数据按照依赖顺序写入 PostgreSQL。

---

## 一、数据库连接（外网地址）

```
psql postgresql://postgres:lchgjt88@dbconn.sealoshzh.site:49145/postgres
```


---

## 二、整体离线链路

```
原始文献
  └─ 数据清洗 (Markdown)
       └─ chunks 切分 + metadata 提取
            ├─ 向量化 (OpenAI Embedding API)
            ├─ jieba 分词 → tsvector 字符串构造
            └─ 写入 PostgreSQL
                 ├─ Step 1: INSERT documents
                 ├─ Step 2: INSERT images
                 ├─ Step 3: INSERT chunks（含 embedding、ts_vector）
                 └─ Step 4: INSERT chunk_images（关联关系）
                 └─ Step 5: UPDATE documents.chunks_count
```

---

## 三、表结构与写入说明

### 3.1 documents（书籍信息表）

**写入时机**：最先写入，每本书一条记录，其他表通过 `document_id` 外键关联。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK，自动生成 | 写入后保存此 ID，供后续 chunks/images 使用 |
| `name` | TEXT | NOT NULL | 书籍全名 |
| `authors` | TEXT[] | 可空 | 作者列表，如 `['李诫', '梁思成']` |
| `publish_info` | TEXT | 可空 | 出版社、出版时间等版权信息，拼成一个字符串即可 |
| `metadata` | JSONB | 可空 | 备用字段，存放其他业务元数据 |
| `chunks_count` | INTEGER | 默认 0 | 所有 chunks 写完后 UPDATE 此字段 |
| `vector_dimensions` | INTEGER | 默认 1024 | 本书使用的 embedding 向量维度，与 chunks.embedding 维度保持一致 |
| `created_at` | BIGINT | 自动填充 | 毫秒时间戳，可不传 |
| `updated_at` | BIGINT | 自动填充 | 毫秒时间戳，可不传 |

**写入示例**：

```python
cur.execute("""
    INSERT INTO documents (name, authors, publish_info, metadata, vector_dimensions)
    VALUES (%s, %s, %s, %s, %s)
    RETURNING id
""", (
    "营造法式",
    ["李诫"],
    "中华书局，1925年",
    json.dumps({"source": "original_scan", "lang": "zh"}),
    1024
))
document_id = cur.fetchone()["id"]
```

---

### 3.2 images（图片信息表）

**写入时机**：在 chunks 之前写入（chunks 写入时需要 `image_id` 确认图片已存在），或与 chunks 同批次写入后再建关联。推荐先写 images。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK，自动生成 | 写入后保存此 ID，供 chunk_images 关联使用 |
| `document_id` | UUID | FK → documents，可空 | 关联所属书籍 |
| `name` | TEXT | NOT NULL | 图片文件名或图名，如 `fig_01_斗拱.png` |
| `description` | TEXT | 可空 | 图片描述，用于发送给 LLM 时补充上下文 |
| `url` | TEXT | 可空 | 图片外部访问地址（OSS/CDN URL） |
| `binary_data` | BYTEA | 可空 | 图片二进制内容，与 url 二选一或同时存 |
| `created_at` | BIGINT | 自动填充 | 可不传 |
| `updated_at` | BIGINT | 自动填充 | 可不传 |

> `url` 与 `binary_data` 至少提供一个，否则图片无法被 LLM 使用。

**写入示例**：

```python
with open("fig_01.png", "rb") as f:
    img_bytes = f.read()

cur.execute("""
    INSERT INTO images (document_id, name, description, url, binary_data)
    VALUES (%s, %s, %s, %s, %s)
    RETURNING id
""", (
    document_id,
    "fig_01_斗拱.png",
    "卷三·大木作制度·斗拱详图",
    "https://cdn.example.com/fig_01.png",
    psycopg2.Binary(img_bytes)   # 若无二进制可传 None
))
image_id = cur.fetchone()["id"]
```

---

### 3.3 chunks（文本块表）

**写入时机**：在 documents 写入之后，chunk_images 写入之前。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK，自动生成 | 写入后保存，供 chunk_images 关联 |
| `document_id` | UUID | FK → documents，NOT NULL | 关联书籍 |
| `content` | TEXT | NOT NULL | chunk 正文，清洗后的原始文本 |
| `metadata` | JSONB | 可空 | 原始 metadata 完整备份（章节、图片列表、注释等） |
| `embedding` | vector(1024) | 可空 | OpenAI Embedding 向量，**见写入说明** |
| `ts_vector` | TSVECTOR | 可空 | jieba 分词结果，**见写入说明** |
| `content_type` | content_type_enum | 可空 | 枚举：`原文` / `注解` / `译文` / `其他` |
| `toc_path` | TEXT[] | 可空 | 目录层级，如 `['卷三', '大木作制度', '斗拱']` |
| `has_images` | BOOLEAN | NOT NULL，默认 false | 该 chunk 是否关联图片 |
| `has_annotation` | BOOLEAN | NOT NULL，默认 false | 该 chunk 是否含注释 |
| `annotation` | TEXT[] | 可空 | 注释内容列表，取自 metadata |
| `created_at` | BIGINT | 自动填充 | 可不传 |
| `updated_at` | BIGINT | 自动填充 | 可不传 |

#### embedding 写入说明

`embedding` 列的数据库类型是 `vector(1024)`（pgvector），**不能直接传 Python list**，需在 SQL 中强制转型：

```python
import openai

# 调用 embedding 接口
resp = openai.embeddings.create(
    model="text-embedding-3-small",   # 或自定义模型名
    input=chunk_content
)
embedding_vector = resp.data[0].embedding   # List[float]，长度 1024

# 写入时转成字符串后用 ::vector 转型
embedding_str = "[" + ",".join(map(str, embedding_vector)) + "]"

cur.execute("""
    INSERT INTO chunks (document_id, content, metadata, embedding, ...)
    VALUES (%s, %s, %s, %s::vector, ...)
""", (document_id, content, json.dumps(metadata), embedding_str, ...))
```

#### ts_vector 写入说明

`ts_vector` 列存储 jieba 分词后的词位字符串，格式需符合 PostgreSQL `to_tsvector` 输出规范。数据端用 jieba 分词后手动拼装，存入字符串再用 `::tsvector` 转型：

```python
import jieba

def build_tsvector_str(text: str) -> str:
    """将文本 jieba 分词后转为 tsvector 格式字符串"""
    words = [w.strip() for w in jieba.cut(text) if w.strip()]
    # 去重，按词位格式拼装（词:位置，简化版直接去重即可）
    unique_words = list(dict.fromkeys(words))
    return " ".join(unique_words)

ts_str = build_tsvector_str(chunk_content)

cur.execute("""
    INSERT INTO chunks (..., ts_vector)
    VALUES (..., %s::tsvector)
""", (..., ts_str))
```

> 查询时同样对用户输入用 jieba 分词，再构造 `tsquery`：
> ```sql
> SELECT * FROM chunks WHERE ts_vector @@ to_tsquery('word1 & word2');
> ```

**完整 chunk 写入示例**：

```python
cur.execute("""
    INSERT INTO chunks (
        document_id, content, metadata, embedding, ts_vector,
        content_type, toc_path, has_images, has_annotation, annotation
    )
    VALUES (
        %s, %s, %s,
        %s::vector,
        %s::tsvector,
        %s, %s, %s, %s, %s
    )
    RETURNING id
""", (
    document_id,
    chunk["content"],
    json.dumps(chunk["metadata"]),
    embedding_str,
    ts_str,
    chunk["content_type"],           # '原文' / '注解' / '译文' / '其他'
    chunk.get("toc_path", []),
    bool(chunk.get("images")),
    bool(chunk.get("annotations")),
    chunk.get("annotations", [])
))
chunk_id = cur.fetchone()["id"]
```

---

### 3.4 chunk_images（chunk 与图片关联表）

**写入时机**：chunks 和 images 均写入完成后，建立多对多关联。

| 列名 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `chunk_id` | UUID | PK + FK → chunks | 关联 chunk |
| `image_id` | UUID | PK + FK → images | 关联图片 |

**写入示例**：

```python
for image_id in chunk_image_ids:
    cur.execute("""
        INSERT INTO chunk_images (chunk_id, image_id)
        VALUES (%s, %s)
        ON CONFLICT DO NOTHING
    """, (chunk_id, image_id))
```

---

## 四、写入依赖顺序

```
documents  ──┐
             ├──► chunks ──► chunk_images
images     ──┘
```

必须严格按以下顺序写入，违反外键约束会报错：

```
Step 1  INSERT documents          → 获得 document_id
Step 2  INSERT images             → 获得 image_id（依赖 document_id）
Step 3  INSERT chunks             → 获得 chunk_id（依赖 document_id）
Step 4  INSERT chunk_images       → （依赖 chunk_id 和 image_id）
Step 5  UPDATE documents SET chunks_count = <实际数量>
```

---

## 五、完整写入流程（伪代码）

```python
conn = psycopg2.connect(...)
cur = conn.cursor()

for book in books:
    # Step 1: 写入书籍
    cur.execute("INSERT INTO documents (...) RETURNING id", (...))
    document_id = cur.fetchone()["id"]

    # Step 2: 写入该书所有图片，建立 name -> image_id 映射
    image_id_map = {}   # { image_name: image_id }
    for img in book.images:
        cur.execute("INSERT INTO images (...) RETURNING id", (...))
        image_id_map[img.name] = cur.fetchone()["id"]

    # Step 3: 写入 chunks
    chunk_count = 0
    for chunk in book.chunks:
        embedding_str = get_embedding(chunk.content)   # 调用 OpenAI API
        ts_str = build_tsvector_str(chunk.content)     # jieba 分词

        cur.execute("INSERT INTO chunks (...) RETURNING id", (...))
        chunk_id = cur.fetchone()["id"]
        chunk_count += 1

        # Step 4: 建立 chunk ↔ image 关联
        for img_name in chunk.metadata.get("images", []):
            if img_name in image_id_map:
                cur.execute(
                    "INSERT INTO chunk_images VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (chunk_id, image_id_map[img_name])
                )

    # Step 5: 更新书籍 chunks 数量
    cur.execute(
        "UPDATE documents SET chunks_count = %s WHERE id = %s",
        (chunk_count, document_id)
    )

conn.commit()
cur.close()
conn.close()
```

---

## 六、content_type 枚举取值

| 枚举值 | 含义 |
|---|---|
| `原文` | 书籍原始正文 |
| `注解` | 编者/译者注解 |
| `译文` | 白话文翻译 |
| `其他` | 图表说明、附录等其他内容 |

写入时传字符串字面值即可，PostgreSQL 自动校验枚举合法性。

---

## 七、注意事项

1. **embedding 维度一致性**：`documents.vector_dimensions` 记录的维度必须与实际写入 `chunks.embedding` 的维度相同（默认 1024）。切换 embedding 模型前需更新此字段并重建所有 chunks 的 embedding。

2. **ts_vector 格式**：`::tsvector` 转型要求词之间用空格分隔，词本身不含空格。jieba 分词后需过滤空字符串和纯标点。

3. **图片二进制 vs URL**：`binary_data` 和 `url` 均可空，但 LLM 组装回答时后端会优先使用 `url`，建议两者都填。

4. **批量写入性能**：chunk 数量预计 <10k，可使用 `psycopg2.extras.execute_values` 批量插入，显著提升写入速度：
    ```python
    from psycopg2.extras import execute_values
    execute_values(cur, "INSERT INTO chunks (document_id, content) VALUES %s", rows)
    ```

5. **事务管理**：建议每本书的所有写入操作在同一事务内完成，出错时整体回滚，避免脏数据。
