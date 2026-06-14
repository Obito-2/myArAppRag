from minio import Minio

client = Minio(
    "objectstorageapi.hzh.sealos.run",
    access_key="q5nnz4bx",
    secret_key="gl6n8qpr2jtl5rzh",
    secure=True
)