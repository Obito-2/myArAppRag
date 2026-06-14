# RAG 数据库写入参考（数据端）

## 1. 适用范围

本说明仅覆盖 RAG 相关 4 张表：

- `documents`
- `text_chunks`
- `image_chunks`
- `relations`

## 3. 枚举与取值规范（强烈建议遵守）

### 3.1 `content_type`（`text_chunks.content_type`）

建议值：

- `original_text`
- `annotation`
- `modern_translation`
- `interpretation`
- `others_text`

### 3.2 `source_type` / `target_type`（`relations`）

建议值：

- 文本类型：`original_text` / `annotation` / `modern_translation` / `interpretation` / `others_text`
- 图像类型：`image`

### 3.3 `relation_type`（`relations.relation_type`）

建议值：

- `illustrates`
- `annotates`

## 4. 四张表字段口径

说明：Python 模型里主键是 `str`，但迁移后数据库列类型是 `UUID`。实际写入时请传合法 UUID 字符串（如 `a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11`）。

### 4.1 `documents`

必填字段：

- `name` (TEXT)

可选字段：

- `id` (UUID, 默认 `gen_random_uuid()`)
- `authors` (TEXT[])
- `other_metadata` (JSONB)
- `content` (TEXT)
- `created_at` / `updated_at` (BIGINT，默认毫秒时间戳)

### 4.2 `text_chunks`

必填字段：

- `main_text` (TEXT)
- `book_id` (UUID，外键到 `documents.id`)

可选字段：

- `chunk_id` (UUID, 默认 `gen_random_uuid()`)
- `content_type` (VARCHAR(30))
- `chunk_size` (INTEGER)
- `closest_title` (VARCHAR(500))
- `toc_path` (TEXT[])
- `search_text` (TEXT)
- `ts_vector` (TSVECTOR)
- `other_metadata` (JSONB)
- `embedding_values` (vector(1024))
- `created_at` / `updated_at` (BIGINT)

### 4.3 `image_chunks`

必填字段：

- 无硬性必填（建议至少传 `book_id` + `title` 或 `caption` 之一，便于检索）

可选字段：

- `image_id` (UUID, 默认 `gen_random_uuid()`)
- `title` (VARCHAR(500))
- `image_uri` (TEXT)
- `local_path` (TEXT)
- `alt_text` (TEXT)
- `caption` (TEXT)
- `book_id` (UUID，可空，外键到 `documents.id`)
- `closest_title` (VARCHAR(500))
- `toc_path` (TEXT[])
- `search_text` (TEXT)
- `ts_vector` (TSVECTOR)
- `embedding_values` (vector(1024))
- `format` (VARCHAR(20))
- `created_at` / `updated_at` (BIGINT)

### 4.4 `relations`

必填字段：

- `source_type` (VARCHAR(30))
- `source_id` (UUID)
- `target_type` (VARCHAR(30))
- `target_id` (UUID)
- `relation_type` (VARCHAR(30))

可选字段：

- `relation_id` (UUID, 默认 `gen_random_uuid()`)
- `created_at` (BIGINT)

注意：`relations` 表本身没有数据库级外键，请在写入前后自行校验 `source_id/target_id` 是否真实存在。

## 5. 推荐写入顺序（必须）

1. 先写 `documents`
2. 再写 `text_chunks` / `image_chunks`（依赖 `book_id`）
3. 最后写 `relations`（依赖 chunk/image id）

## 7. 参数化写入建议（Python / psycopg2）

配合 `app/connect.py` 的 `execute_query` 使用参数化，避免拼接 SQL：

```python
sql = """
INSERT INTO text_chunks (
  content_type, main_text, book_id, search_text, ts_vector, embedding_values
) VALUES (
  %s, %s, %s, %s, to_tsvector('simple', %s), %s::vector
);
"""
params = (
    "annotation",
    "这里是注释文本",
    "11111111-1111-1111-1111-111111111111",
    "这里是注释文本",
    "这里是注释文本",
    "[" + ",".join(["0"] * 1024) + "]",
)
```

说明：

- `embedding_values` 通过 `%s::vector` 传入字符串形式向量（如 `[0,0,0,...]`）
- 如果暂时没有向量，可先传 `NULL`，后续再补写
