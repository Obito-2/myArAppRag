from minio import Minio

client = Minio(
    "objectstorageapi.hzh.sealos.run",
    access_key="q5nnz4bx",
    secret_key="kqd6c874d8mbztqn",
    secure=True
)