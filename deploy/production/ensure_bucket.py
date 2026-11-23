from botocore.exceptions import ClientError

from app.config import settings
from app.storage import s3


client = s3()
try:
    client.head_bucket(Bucket=settings.s3_bucket)
except ClientError as exc:
    if exc.response['Error']['Code'] not in ('404', 'NoSuchBucket'):
        raise
    client.create_bucket(Bucket=settings.s3_bucket)
