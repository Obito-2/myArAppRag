"""
导出text_chunks样本数据脚本

功能：
1. 打印documents表的name和id
2. 在text_chunks表中，分别取每个book_id且类别不同的文本块数据，随机10条
3. 输出为excel表格，排除过长的字段（embedding_values, ts_vector等）
"""

import pandas as pd
import random
import sys
sys.path.append('.')
from tools.db_connect import execute_query

# 需要排除的字段（过长的字段）
EXCLUDE_FIELDS = [
    'embedding_values',
    'ts_vector',
    # 'search_text',  # 可能也很长
]


def get_documents():
    """获取documents表的所有记录（name和id）"""
    query = """
    SELECT id, name, authors, created_at
    FROM documents
    ORDER BY created_at DESC;
    """
    results = execute_query(query, fetch_all=True)
    return results


def get_text_chunks_by_book_and_type(book_id, content_type, limit=10):
    """获取指定book_id和content_type的随机记录"""
    # 随机取样，使用ORDER BY RANDOM()可能效率不高，但对于小数据量可以接受
    query = """
    SELECT 
        chunk_id,
        book_id,
        content_type,
        main_text,
        chunk_size,
        closest_title,
        toc_path,
        other_metadata,
        created_at,
        updated_at
    FROM text_chunks
    WHERE book_id = %s AND content_type = %s
    ORDER BY RANDOM()
    LIMIT %s;
    """
    results = execute_query(
        query, 
        params=(book_id, content_type, limit), 
        fetch_all=True
    )
    return results


def get_distinct_book_content_types():
    """获取所有book_id和content_type的组合"""
    query = """
    SELECT DISTINCT book_id, content_type
    FROM text_chunks
    ORDER BY book_id, content_type;
    """
    results = execute_query(query, fetch_all=True)
    return results


def main():
    print("=" * 60)
    print("1. 打印 documents 表的 name 和 id")
    print("=" * 60)
    
    documents = get_documents()
    if not documents:
        print("documents 表为空！")
        return
    
    # 打印documents表
    print(f"{'ID':<40} | {'Name':<30} | Authors")
    print("-" * 100)
    for doc in documents:
        doc_id = str(doc['id'])
        name = doc['name'] or ''
        authors = doc['authors'] if doc['authors'] else ''
        # 截断过长的显示
        if len(name) > 30:
            name = name[:27] + "..."
        print(f"{doc_id:<40} | {name:<30} | {authors}")
    
    print(f"\n共 {len(documents)} 条文档记录\n")
    
    # 步骤2：获取每个book_id和content_type组合的随机10条数据
    print("=" * 60)
    print("2. 获取 text_chunks 样本数据（每个book_id + content_type 随机10条）")
    print("=" * 60)
    
    # 获取所有组合
    combinations = get_distinct_book_content_types()
    print(f"发现 {len(combinations)} 个 book_id + content_type 组合")
    
    all_samples = []
    
    # 为了更高效处理，先获取每个book_id下有哪些content_type
    # 按book_id分组处理
    book_type_map = {}
    for combo in combinations:
        book_id = str(combo['book_id'])
        content_type = combo['content_type']
        if book_id not in book_type_map:
            book_type_map[book_id] = []
        book_type_map[book_id].append(content_type)
    
    # 遍历每个book_id及其content_type
    for book_id, content_types in book_type_map.items():
        print(f"\n处理 book_id: {book_id}")
        
        # 查找对应的文档名称
        doc_name = "未知"
        for doc in documents:
            if str(doc['id']) == book_id:
                doc_name = doc['name'] or "未知"
                break
        
        for content_type in content_types:
            print(f"  - content_type: {content_type}")
            
            samples = get_text_chunks_by_book_and_type(book_id, content_type, limit=10)
            
            for sample in samples:
                # 转换为你需要的数据
                row = {
                    'book_id': book_id,
                    'book_name': doc_name,
                    'content_type': sample['content_type'],
                    'chunk_id': str(sample['chunk_id']) if sample['chunk_id'] else '',
                    'main_text': sample['main_text'][:200] if sample['main_text'] else '',  # 截断显示
                    'chunk_size': sample['chunk_size'],
                    'closest_title': sample['closest_title'] or '',
                    'toc_path': str(sample['toc_path']) if sample['toc_path'] else '',
                    'other_metadata': str(sample['other_metadata']) if sample['other_metadata'] else '',
                    'created_at': sample['created_at'],
                    'updated_at': sample['updated_at'],
                }
                all_samples.append(row)
    
    print(f"\n共收集到 {len(all_samples)} 条样本记录")
    
    if all_samples:
        # 创建DataFrame并导出Excel
        df = pd.DataFrame(all_samples)
        
        # 调整列顺序
        columns_order = [
            'book_id',
            'book_name', 
            'content_type',
            'chunk_id',
            'main_text',
            'chunk_size',
            'closest_title',
            'toc_path',
            'other_metadata',
            'created_at',
            'updated_at'
        ]
        
        # 确保所有列都存在
        for col in columns_order:
            if col not in df.columns:
                df[col] = ''
        
        df = df[columns_order]
        
        output_file = 'importData/output/text_chunks_samples.xlsx'
        df.to_excel(output_file, index=False, engine='openpyxl')
        print(f"\n已导出Excel文件: {output_file}")
    else:
        print("\n没有找到text_chunks数据！")


if __name__ == "__main__":
    main()