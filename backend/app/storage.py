import mimetypes
import shutil
from functools import lru_cache
from pathlib import Path

import boto3

from .config import settings


@lru_cache
def s3():
    return boto3.client('s3', endpoint_url=settings.s3_endpoint,
                        aws_access_key_id=settings.s3_access_key,
                        aws_secret_access_key=settings.s3_secret_key,
                        region_name=settings.s3_region)


def put_file(key: str, path: Path):
    if settings.storage_backend == 'local':
        target = settings.data_dir / 'objects' / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    else:
        s3().upload_file(str(path), settings.s3_bucket, key,
                         ExtraArgs={'ContentType': mimetypes.guess_type(key)[0] or 'application/octet-stream'})


def get_file(key: str, target: Path):
    if settings.storage_backend == 'local':
        shutil.copyfile(settings.data_dir / 'objects' / key, target)
    else:
        s3().download_file(settings.s3_bucket, key, str(target))


def remove_prefix(prefix: str, permanent=False):
    if not prefix or prefix.startswith('/') or '..' in prefix.split('/') or '\\' in prefix:
        raise ValueError('非法存储路径')
    if settings.storage_backend == 'local':
        root = (settings.data_dir / 'objects').resolve()
        target = (root / prefix).resolve()
        if root not in target.parents:
            raise ValueError('非法存储路径')
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    else:
        versioned = permanent and s3().get_bucket_versioning(Bucket=settings.s3_bucket).get('Status') in ('Enabled', 'Suspended')
        paginator = 'list_object_versions' if versioned else 'list_objects_v2'
        for page in s3().get_paginator(paginator).paginate(Bucket=settings.s3_bucket, Prefix=prefix):
            if versioned:
                keys = [{'Key': obj['Key'], 'VersionId': obj['VersionId']} for obj in page.get('Versions', []) + page.get('DeleteMarkers', [])]
            else:
                keys = [{'Key': obj['Key']} for obj in page.get('Contents', [])]
            for offset in range(0, len(keys), 1000):
                result = s3().delete_objects(Bucket=settings.s3_bucket, Delete={'Objects': keys[offset:offset + 1000]})
                if result.get('Errors'):
                    raise RuntimeError('部分存储文件删除失败')


def list_files(prefix: str):
    if not prefix.endswith('/') or prefix.startswith('/') or '..' in prefix.split('/') or '\\' in prefix:
        raise ValueError('非法存储路径')
    if settings.storage_backend == 'local':
        root = (settings.data_dir / 'objects').resolve()
        target = (root / prefix).resolve()
        if root not in target.parents:
            raise ValueError('非法存储路径')
        entries = []
        for path in target.rglob('*'):
            if path.is_file():
                if target not in path.resolve().parents:
                    raise ValueError('文件不在版本目录中')
                entries.append({'path': path.relative_to(target).as_posix(), 'size_bytes': path.stat().st_size})
        return sorted(entries, key=lambda entry: entry['path'])
    entries = []
    for page in s3().get_paginator('list_objects_v2').paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            path = obj['Key'][len(prefix):]
            if path and not path.endswith('/'):
                entries.append({'path': path, 'size_bytes': obj['Size']})
    return sorted(entries, key=lambda entry: entry['path'])
