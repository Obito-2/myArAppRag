
# 本文档用于将jsonl格式的生僻字和术语简要导入文本chunk库

## documents信息：
《法式》生僻字库："e6b410cb-99e9-44c4-8c40-5445779ca8e3"

《法式》术语简要："45a4f3d7-a3da-4b53-959a-72ee245f2f18"

## 生僻字库写入逻辑


```json
{"汉字": "搕", "UNICODE": "U+6415", "读音": "kē、è", "拆字": ["扌", "盍"], "解释": "①取。 ②敲击。例如～烟袋。", "替代字": null, "字形相似汉字": ["掩", "摸", "摧", "揽", "擅", "搅", "握", "搭", "撩", "撞"], "数据来源": "汉语字典 https://www.hanyuguoxue.com/zidian/zi-25621", "出现在术语": ["搕鏁柱"]}
```
main_TEXT:拼接逻辑,拼接为连贯的自然语言描述：汉字"x",读音是“”，可以拆字描述为"",其意思是：XX，出现在术语“xx、xx、xx”中。
bookid：e6b410cb-99e9-44c4-8c40-5445779ca8e3
CHUNK_ID: UNICODE码
content_TYEP:注释
closest：无
toc ptah：无
search_TEXT:与main text相同
other metadata：没有进行拼接使用的其余字段
embdding_VALUE:向量化嵌入


## 术语库写入逻辑

```json
{"术语": "万字板", "解释": "石或木单勾栏上刻万字纹的栏板。", "首出处卷": "第3卷 壕寨制度、石作制度", "读音": ["wàn", "zì", "bǎn"], "少见字注解": null}
{"术语": "万字造", "解释": "用万字做图案的做法。", "首出处卷": "第3卷 壕寨制度、石作制度", "读音": ["wàn", "zì", "zào"], "少见字注解": null}
{"术语": "飞子", "解释": "即飞檐椽。", "首出处卷": "第5卷 大木作制度", "读音": ["fēi", "zi"], "少见字注解": ["椽 chuán ①放在檩上架着屋顶的木条。例如～子。～笔。 ②古代房屋间数的代称：“东宇西房数十～。”"]}

```

main_TEXT:拼接逻辑,拼接为连贯的自然语言描述："xxxx(名称)",其意思是：XXxxx，首次出现在：XXXX中，其读音为：XXX。
bookid: 45a4f3d7-a3da-4b53-959a-72ee245f2f18
chunk-id：uuid
content_TYEP:解读
closest：无
toc ptah：无
search_TEXT:与main text相同
other metadata：没有进行拼接使用的其余字段，少见字注解
embdding_VALUE:向量化嵌入
关联关系：根据少见字注解字段查询，如术语飞子，包含少见字 椽，那么需要建立关联关系，并写入关系表，为空则无引用关系

## 关系表入库逻辑：

关联关系表写入逻辑，在解析阶段：
1.为每个文块创建 TempTextChunk对象，并提取其中的引用 ID（脚注或图注）存入 refs_id 列表。
2.为每个注释块创建 TempNoteChunk对象，记录 note_id;为每个图片创建 TempImageChunk对象，记录 image_id（如 "image 3-2"）;维护一个字典 id_to_temp，将 note_id 或 image_id 映射到对应的临时对象。
3.遍历所有临时对象（原文、注释、图片），分别插入 text_chunks 和 image_assets 表，获得数据库生成的主键（例如自增 ID 或 UUID），并回填到临时对象的 db_id 字段。
4.遍历所有 TempMainTextChunk，对其 refs_id 中的每个引用 ID，从 id_to_temp 中找到对应的临时对象（注释或图片），获取其 db_id，然后构建 (source_type, source_id, target_type, target_id, relation_type) 记录，最后批量插入 resource_relations表。
通过 临时对象 + 内存字典 暂存原文与注释、图片互相之间的引用关系，在 chunk 入库并获取 ID 后，再利用这些临时数据将关联关系批量插入关联表。整个过程需要保证 chunk 表与关联表职责分离，没有在 chunk 表中引入耦合字段，同时确保了数据完整性和插入顺序的正确性。

注意悬空引用（Dangling References）：如果文档包含关联，代码要有异常捕获，避免在 id_to_temp 查找失败时导致整个入库进程崩溃。


