# app/connect.py
import os
import sys
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor  # 返回字典格式的结果
from urllib.parse import urlparse

# 直接粘贴最新连接地址到这里即可，或通过环境变量 DB_URL 覆盖
_DEFAULT_URL = "postgresql://postgres:lchgjt88@dbconn.sealoshzh.site:37083/postgres"

_url = urlparse(os.getenv("DB_URL", _DEFAULT_URL))
DB_HOST     = _url.hostname
DB_PORT     = str(_url.port or 5432)
DB_NAME     = _url.path.lstrip("/") or "postgres"
DB_USER     = _url.username
DB_PASSWORD = _url.password

# 创建连接池（最小1个连接，最大10个连接）
connection_pool = psycopg2.pool.SimpleConnectionPool(
    1, 10,
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASSWORD,
    cursor_factory=RealDictCursor  # 让查询结果以字典形式返回
)

def get_connection():
    """从连接池获取一个连接"""
    return connection_pool.getconn()
def release_connection(conn):
    """释放连接回连接池"""
    connection_pool.putconn(conn)

def execute_query(query, params=None, fetch_one=False, fetch_all=False):
    """
    执行 SQL 并自动管理连接和游标
    fetch_one: 返回单条记录（字典）
    fetch_all: 返回所有记录（字典列表）
    否则执行 INSERT/UPDATE/DELETE 并提交
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        if fetch_one:
            result = cur.fetchone()
        elif fetch_all:
            result = cur.fetchall()
        else:
            conn.commit()
            result = None
        return result
    except Exception as e:
        conn.rollback()  # 出错时回滚
        raise e
    finally:
        cur.close()
        release_connection(conn)


def parse_table_name(table_name: str):
    """
    解析表名，支持:
    - users -> public.users
    - public.users -> public.users
    """
    if "." in table_name:
        schema_name, pure_table_name = table_name.split(".", 1)
    else:
        schema_name, pure_table_name = "public", table_name
    return schema_name, pure_table_name


def fetch_table_schema(table_name: str):
    """查询指定表的结构信息。"""
    schema_name, pure_table_name = parse_table_name(table_name)
    sql = """
    SELECT
        ordinal_position,
        column_name,
        data_type,
        is_nullable,
        column_default
    FROM information_schema.columns
    WHERE table_schema = %s
      AND table_name = %s
    ORDER BY ordinal_position;
    """
    return execute_query(sql, params=(schema_name, pure_table_name), fetch_all=True)


def fetch_table_preview(table_name: str, limit: int = 10):
    """查询指定表前 N 条数据。"""
    schema_name, pure_table_name = parse_table_name(table_name)
    sql = f'SELECT * FROM "{schema_name}"."{pure_table_name}" LIMIT %s;'
    return execute_query(sql, params=(limit,), fetch_all=True)


def print_table_info(table_name: str):
    """打印表结构与前10条数据。"""
    schema = fetch_table_schema(table_name)
    if not schema:
        print(f"表不存在或无可读列: {table_name}")
        return

    preview_rows = fetch_table_preview(table_name, limit=4)

    print(f"===== 表结构: {table_name} =====")
    for col in schema:
        print(
            f"{col['ordinal_position']:>2}. "
            f"{col['column_name']} "
            f"({col['data_type']}) "
            f"NULLABLE={col['is_nullable']} "
            f"DEFAULT={col['column_default']}"
        )

    print(f"\n===== 前 {len(preview_rows)} 条数据: {table_name} =====")
    for i, row in enumerate(preview_rows, start=1):
        print(f"{i:>2}. {row}")


if __name__ == "__main__":
    # if len(sys.argv) < 2:
    #     print("用法: python app/connect.py <table_name>")
    #     print("示例: python app/connect.py users")
    #     print("示例: python app/connect.py public.users")
    #     sys.exit(1)

    # input_table_name = sys.argv[1]
    print_table_info("image_chunks")