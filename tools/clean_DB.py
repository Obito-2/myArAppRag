from db_connect import execute_query
from db_connect import get_connection, release_connection, execute_query

tables = ["documents", "text_chunks", "image_chunks", "relations"]
print("===== 当前数据量 =====")
for t in tables:
    try:
        result = execute_query(f"SELECT COUNT(*) as cnt FROM {t}", fetch_one=True)
        print(f"  {t}: {result['cnt']} 条")
    except Exception as e:
        print(f"  {t}: 查询失败 - {e}")


# delete_order = ["relations", "text_chunks", "image_chunks"]
# print("===== 删除前数据量 =====")
# for t in delete_order:
#     result = execute_query(f"SELECT COUNT(*) as cnt FROM {t}", fetch_one=True)
#     print(f"  {t}: {result['cnt']} 条")
# print("\n===== 开始删除 =====")
# conn = get_connection()
# cur = conn.cursor()
# try:
#     for t in delete_order:
#         cur.execute(f"DELETE FROM {t}")
#         print(f"  已清空 {t}")
#     conn.commit()
#     print("\n  事务已提交。")
# except Exception as e:
#     conn.rollback()
#     print(f"\n  删除失败，已回滚: {e}")
# finally:
#     cur.close()
#     release_connection(conn)
# print("\n===== 删除后数据量 =====")
# for t in delete_order:
#     result = execute_query(f"SELECT COUNT(*) as cnt FROM {t}", fetch_one=True)
#     print(f"  {t}: {result['cnt']} 条")