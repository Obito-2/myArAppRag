#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
将指定目录的图片上传到 Minio 存储桶（纯上传，不操作数据库）

用法:
  python tools/upload_images_to_bucket.py
  python tools/upload_images_to_bucket.py --dry-run
"""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
from pathlib import Path

# 让 Python 找到项目根目录下的 tools 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.bucket_load import client as minio_client

# ── 配置 ─────────────────────────────────────────────────────────

MINIO_BUCKET = "q5nnz4bx-yingzaofashi"
MINIO_ENDPOINT = "objectstorageapi.hzh.sealos.run"

# 支持的图片扩展名
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp"}

# 上传任务
# - 有 md_dir：扫描 MD 文件中 ![]() 引用的图片
# - 无 md_dir：直接扫描 images_dir 下全部图片
UPLOAD_TASKS = [
    # {
    #     "md_dir": PROJECT_ROOT / "knowledgeBase" / "md_to_chunk" / "liangzhu_Markdown_2chunk",
    #     "images_dir": PROJECT_ROOT / "knowledgeBase" / "liangzhu_unzip" / "OEBPS" / "Images",
    #     "minio_prefix": "liangzhu",
    # },
    # {
    #     "md_dir": PROJECT_ROOT / "knowledgeBase" / "md_to_chunk" / "wangzhu_Markdown_Output",
    #     "images_dir": PROJECT_ROOT / "knowledgeBase" / "wangzhu_unzip" / "OEBPS" / "images",
    #     "minio_prefix": "wangzhu",
    # },
    {
        "images_dir": PROJECT_ROOT / "knowledgeBase" / "pdfParse" / "cleaned_data" / "images_all",
        "minio_prefix": "images",
    },
]

# 匹配 MD 中 ![]( ) 图片引用，提取括号内的路径
IMAGE_REF_RE = re.compile(r"!\[.*?\]\(([^)]+)\)")


def collect_files(local_dir: Path) -> list[Path]:
    """递归扫描目录下所有图片文件（含子目录），按相对路径排序"""
    files = []
    for f in local_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            files.append(f)
    files.sort(key=lambda x: x.relative_to(local_dir).as_posix())
    return files


def collect_md_images(md_dir: Path, images_dir: Path) -> list[Path]:
    """扫描 md_dir 下所有 MD 文件中 ![]() 引用的图片，返回在 images_dir 中实际存在的文件路径"""
    image_names: set[str] = set()

    md_files = sorted(md_dir.glob("*.md"))
    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        for m in IMAGE_REF_RE.finditer(content):
            img_name = Path(m.group(1)).name
            if img_name:
                image_names.add(img_name)

    files: list[Path] = []
    for name in sorted(image_names):
        candidate = images_dir / name
        if candidate.is_file():
            files.append(candidate)
        else:
            print(f"  [WARN] MD 引用图片在 images_dir 中不存在: {name}")

    return files


def upload_file(local_path: Path, minio_prefix: str, images_dir: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    上传单个文件到 Minio，返回 (成功与否, 对象名)
    object_name 保留相对于 images_dir 的子目录路径
    """
    rel_path = local_path.relative_to(images_dir)
    object_name = f"{minio_prefix}/{rel_path.as_posix()}"
    content_type = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"

    if dry_run:
        print(f"  [DRY-RUN] 将上传: {local_path} -> {MINIO_BUCKET}/{object_name}")
        return True, object_name

    try:
        minio_client.fput_object(
            MINIO_BUCKET,
            object_name,
            str(local_path),
            content_type=content_type,
        )
        return True, object_name
    except Exception as e:
        return False, str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="上传图片到 Minio 存储桶")
    parser.add_argument("--dry-run", action="store_true", help="仅列出将上传的文件，不实际上传")
    args = parser.parse_args()

    total_success = 0
    total_fail = 0

    for idx, task in enumerate(UPLOAD_TASKS, 1):
        md_dir = task.get("md_dir")
        images_dir = task["images_dir"]
        minio_prefix = task["minio_prefix"]

        print(f"\n[{idx}/{len(UPLOAD_TASKS)}] 上传 {minio_prefix} 图片")
        print(f"  图片目录: {images_dir}")

        if md_dir:
            print(f"  MD 目录: {md_dir}")
            if not md_dir.is_dir():
                print(f"  [WARN] MD 目录不存在，跳过")
                continue

        if not images_dir.is_dir():
            print(f"  [WARN] 图片目录不存在，跳过")
            continue

        if md_dir:
            files = collect_md_images(md_dir, images_dir)
            print(f"  共 {len(files)} 个图片文件（来自 MD 引用）")
        else:
            files = collect_files(images_dir)
            print(f"  共 {len(files)} 个图片文件（含子目录递归扫描）")

        success = 0
        fail = 0

        for f in files:
            ok, info = upload_file(f, minio_prefix, images_dir, dry_run=args.dry_run)
            if ok:
                status = "DRY-RUN" if args.dry_run else "OK"
                print(f"  [{status}] {info}")
                success += 1
            else:
                print(f"  [FAIL] {f.relative_to(images_dir).as_posix()} - {info}")
                fail += 1

        print(f"  {minio_prefix}: 成功 {success}, 失败 {fail}")
        total_success += success
        total_fail += fail

    print(f"\n===== 合计: 成功 {total_success}, 失败 {total_fail} =====")

    if not args.dry_run and total_success > 0:
        print(f"\nBucket: {MINIO_BUCKET}")
        print(f"URL 前缀: https://{MINIO_ENDPOINT}/{MINIO_BUCKET}/")


if __name__ == "__main__":
    main()