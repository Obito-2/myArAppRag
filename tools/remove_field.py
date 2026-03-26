#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
删除JSONL文件中每一行的"涉及术语"字段
"""
import json

input_file = 'knowledgeBase/pdfParse/cleaned_data/rare_hanzi_integrated.jsonl'
output_file = 'knowledgeBase/pdfParse/cleaned_data/rare_hanzi_integrated.jsonl'

# 读取并处理
processed_count = 0
with open(input_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 处理每一行，删除"涉及术语"字段
processed_lines = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    
    data = json.loads(line)
    # 删除"涉及术语"字段
    data.pop('涉及术语', None)
    processed_lines.append(json.dumps(data, ensure_ascii=False))
    processed_count += 1

# 写回原文件
with open(output_file, 'w', encoding='utf-8') as f:
    for line in processed_lines:
        f.write(line + '\n')

print(f"处理完成！共处理 {processed_count} 行数据，已删除'涉及术语'字段")