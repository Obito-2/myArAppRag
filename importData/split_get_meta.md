# 该文档用于说明如何对md文件进行切分和元数据抽取参考

## 针对不同类型内容的切分方式

| 内容类型 | 切分方式 | 说明 |
| :--- | :--- | :--- |
| **原文/译文/解读/其他文本** | 必须按照段落分块，支持 chunk 块大小参数，默认为 300 字符，无 overlap，| 保持语义连贯，可能具备关联注释、图片关系。写入到 `text_chunks` 的 `main_text` 字段。 |
| **注释** | 根据下述规则识别注释内容，每条注释独立成一个 chunk，不设置字符上线。 | 写入到 `text_chunks` 的 `main_text` 字段，并将关联关系写入到 `relations` 表中。 |
| **图片** | 根据md图片语法识别，每张图片独立 chunk。 | 写入到 `image_chunks`，可能具备原文/译文/解读/其他文本关系，将关联关系写入到 `relations` 表中。 |

## 文本和图像表元数据获取逻辑

### text_Chunk元数据抽取方法：

**main_text (TEXT)**：忽略标题行和内容类型行，按照段落分块，不允许跨标题和内容类型组合块，支持 chunk 块大小参数，默认为 300 字符，如果当前块所包含的段落超出字符上限，则减少块中段落数，而不是截断，无 overlap。保持语义连贯，可能具备关联注释、图片关系。

**content_type**:根据段落所在的内容类型行，向上查找，找到的第一个类型行，如没找到，则归属为other
```
内容类型行和写入类型映射关系：
 **【原文】**=原文、**【梁注】**=注释、**【译文】**=译文
**【题解】** = 解读，**知识小链接** = other
```

**book_id**：

knowledgeBase\liangzhu_unzip\OEBPS\Markdown_2chunk：梁思成注释《营造法式》: 4ced9d70-e725-4312-9988-d3acd79d878b
knowledgeBase\wangzhu_unzip\OEBPS\Markdown_Output：王贵祥译注营造法式: 7a1746cb-bacb-4d7d-b122-ca72ea52c02e

**closet_title**：基于规则方法，向上找对最近的章节标题（以n个# 开始，独立一行），作为closest_title

**toc_path**：从最近子标题向上查找，根据closest_title继续向上查找直到最顶级的章节标题（最顶级不一定是一级，可能是2级），按顺序记录为list

**正文中的生僻字图片处理**：匹配所有alt文字为生僻字、公式、图标等）的图片，统一替换为 [生僻字符]即可，该图片也无需上传存入数据库例如：

```
![生僻字、公式、图标等）](../Images/image_415.svg)在外。方二分五厘。上下桯亦同。

[生僻字符]在外。方二分五厘。上下桯亦同。
```

**常规图及其和文本块的关联逻辑处理**：匹配md图片语法，将该图片作为chunk上传至储存桶，获取url存入image表，正文中不出现md图片语法，不考虑图文关联关系，分别对图片和文本块进行写入即可

**注释关联信息**：基于正则规则，匹配当前main_text是否存在特定注释引用语法，如 [^2]，命中，解析注释引用编号，临时暂存关联关系后续写入关系表。不允许跨标题、跨内容类型关联注释和正文

**search_text (TEXT)**：用于向量化和分词，后续语义和关键字检索。使用元数据增强后的查询文本，用于嵌入和关键字拆分，拼接逻辑：[closest_title]+[CONTENT_TYPE]+[main_text] 

**embedding_values**调用文本嵌入模型进行向量化，search_text

**ts_vector (TSVECTOR)**：加载自定义词表，对search_text文本使用jieba分词，自定义词表路径："D:\postgraduate_study\graduation_thesis\llm_rag\myArAppRag\knowledgeBase\pdfParse\cleaned_data\termsName_forPrompt.text"

**chunk_size**:返回search_text的token数量

other_metadata (JSONB):无

### 代码参考，可考虑直接复用

```python
#文本嵌入用的模型接口
from openai import OpenAI

# 设置阿里云API密钥
API_KEY = os.getenv("SEU_API_KEY")
BASE_URL = "http://10.128.202.100:3010/v1"
MODEL_NAME = "text-embedding-v4"

# 初始化OpenAI客户端
client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
```

### IMAGE_Chunk元数据抽取方法：

所有生僻字、公式、图标等）图不进入chunk库，例如

title (VARCHAR(500))：从md语法提取

image_uri (TEXT)：上传到对象存储内存桶中获取url,同一个书籍的图片放到一个文件夹下

local_path (TEXT)：本地绝对路径

book_id：同文本chunk

closest_title (VARCHAR(500))：同文本chunk

toc_path (TEXT[])：同文本chunk

caption (TEXT)：常规图，紧跟md语法后，用一行的文本，例如
```
![图31附-22 六架椽屋前后乳栿、劄牵用四柱](../images/lwlz1xwc7fvrsiddfgd.jpg)九架梁屋进深四柱，前后檐枓科三彩（跴）。

```
search_text (TEXT)：用于关键字检索，存在拼接逻辑：[closest_title]+[title]

**ts_vector (TSVECTOR)**：加载自定义词表，对search_text文本使用jieba分词，自定义词表路径："D:\postgraduate_study\graduation_thesis\llm_rag\myArAppRag\knowledgeBase\pdfParse\cleaned_data\termsName_forPrompt.text"

embedding_values (vector(1024))：无,图片无需向量嵌入

## 关系表入库逻辑：

关联关系表写入逻辑，在解析阶段：
1.为每个文块创建 TempTextChunk对象，并提取其中的引用 ID（脚注或图注）存入 refs_id 列表。
2.为每个注释块创建 TempNoteChunk对象，记录 note_id;为每个图片创建 TempImageChunk对象，记录 image_id（如 "image 3-2"）;维护一个字典 id_to_temp，将 note_id 或 image_id 映射到对应的临时对象。
3.遍历所有临时对象（原文、注释、图片），分别插入 text_chunks 和 image_assets 表，获得数据库生成的主键（例如自增 ID 或 UUID），并回填到临时对象的 db_id 字段。
4.遍历所有 TempMainTextChunk，对其 refs_id 中的每个引用 ID，从 id_to_temp 中找到对应的临时对象（注释或图片），获取其 db_id，然后构建 (source_type, source_id, target_type, target_id, relation_type) 记录，最后批量插入 resource_relations表。
通过 临时对象 + 内存字典 暂存原文与注释、图片互相之间的引用关系，在 chunk 入库并获取 ID 后，再利用这些临时数据将关联关系批量插入关联表。整个过程需要保证 chunk 表与关联表职责分离，没有在 chunk 表中引入耦合字段，同时确保了数据完整性和插入顺序的正确性。

注意悬空引用（Dangling References）：如果文档里写了 (图 1-35) 但实际上 MD 里没这张图，代码要有异常捕获，避免在 id_to_temp 查找失败时导致整个入库进程崩溃。
