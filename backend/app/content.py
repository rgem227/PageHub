import mimetypes
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

from .config import settings
from .db import pool, transaction
from .packages import safe_path
from .security import verify
from .storage import s3


@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    pool.open(wait=True)
    yield
    pool.close()


app = FastAPI(title='隔离页面内容服务', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get('/view/{capability}/{path:path}')
def content(capability: str, path: str):
    payload = verify(capability)
    platform_preview = payload.get('platform') is True
    try:
        path = safe_path(path)
    except ValueError:
        raise HTTPException(400, '非法路径')
    with transaction(payload.get('tenant'), admin=platform_preview) as conn:
        if payload.get('share'):
            version = conn.execute('''SELECT v.storage_prefix FROM page_shares s
                JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
                JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
                JOIN tenants t ON t.id=s.tenant_id JOIN users u ON u.id=s.created_by AND u.tenant_id=s.tenant_id
                WHERE s.id=%s AND s.tenant_id=%s AND s.page_id=%s AND s.version_id=%s
                    AND s.revoked_at IS NULL AND (s.expires_at IS NULL OR s.expires_at>now()) AND p.deleted_at IS NULL
                    AND v.deleted_at IS NULL AND v.deletion_id IS NULL AND t.active AND t.deleted_at IS NULL AND u.active''',
                (payload['share'], payload.get('tenant'), payload.get('page'), payload.get('version'))).fetchone()
        elif platform_preview:
            version = conn.execute('''SELECT v.storage_prefix FROM page_versions v
                JOIN pages p ON p.id=v.page_id AND p.tenant_id=v.tenant_id
                JOIN tenants t ON t.id=v.tenant_id
                JOIN users admin ON admin.id=%s AND admin.role='superadmin' AND admin.active
                WHERE v.id=%s AND v.page_id=%s AND v.tenant_id=%s
                    AND v.deleted_at IS NULL AND v.deletion_id IS NULL''',
                (payload.get('admin'), payload.get('version'), payload.get('page'), payload.get('tenant'))).fetchone()
        else:
            version = conn.execute('''SELECT v.storage_prefix FROM page_versions v JOIN tenants t ON t.id=v.tenant_id
                JOIN pages p ON p.id=v.page_id AND p.deleted_at IS NULL
                JOIN users u ON u.id=%s AND u.tenant_id=v.tenant_id
                WHERE v.id=%s AND v.page_id=%s AND v.deleted_at IS NULL AND v.deletion_id IS NULL
                    AND t.active AND t.deleted_at IS NULL AND u.active''',
                (payload.get('user'), payload.get('version'), payload.get('page'))).fetchone()
    if not version:
        raise HTTPException(403, '页面已删除、账号已调整或租户已停用')
    key = version['storage_prefix'] + path
    # sandbox 不允许 allow-same-origin；页面只能在不透明源中运行脚本。
    headers = {'Content-Security-Policy': f"sandbox allow-scripts; frame-ancestors {settings.web_origin}; object-src 'none'; base-uri 'none'",
               'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store',
               'Access-Control-Allow-Origin': '*'}
    mime = mimetypes.guess_type(path)[0] or 'application/octet-stream'
    if settings.storage_backend == 'local':
        file = settings.data_dir / 'objects' / key
        if not file.is_file():
            raise HTTPException(404, '资源不存在')
        return FileResponse(file, media_type=mime, headers=headers)
    if settings.nginx_accel:
        signed = s3().generate_presigned_url('get_object', Params={'Bucket': settings.s3_bucket, 'Key': key}, ExpiresIn=60)
        parsed = urlparse(signed)
        headers['X-Accel-Redirect'] = '/_objects' + parsed.path + '?' + parsed.query
        return Response(headers=headers, media_type=mime)
    obj = s3().get_object(Bucket=settings.s3_bucket, Key=key)
    return StreamingResponse(obj['Body'].iter_chunks(), media_type=mime, headers=headers)
