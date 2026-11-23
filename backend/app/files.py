import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .auth import current_user
from .db import audit, transaction
from .listing import paginated, pattern
from .api import rollback
from . import storage
from .config import settings
from .security import sign

router = APIRouter(prefix='/api/files', tags=['文件管理'])
MAX_DOWNLOAD_ITEMS = 100
MAX_DOWNLOAD_BYTES = 2 * 1024**3


def scope(user, platform, tenant=None):
    if platform:
        if user['role'] != 'superadmin' or user.get('credential_id'):
            raise HTTPException(403, '需要平台超级管理员网页登录权限')
        return tenant, True
    if tenant and tenant != user['tenant_id']:
        raise HTTPException(403, '不能访问其他租户的文件')
    return user['tenant_id'], False


def downloadable_uploads(conn, ids, target):
    rows = conn.execute('''SELECT j.id,j.tenant_id,j.filename,j.size_bytes,j.status,v.deleted_at,v.deletion_id,
        v.storage_prefix,v.entry_file
        FROM uploads j LEFT JOIN page_versions v ON v.id=j.id AND v.tenant_id=j.tenant_id
        WHERE j.id=ANY(%s) AND (%s::uuid IS NULL OR j.tenant_id=%s) FOR UPDATE OF j''',
        (ids, target, target)).fetchall()
    if len(rows) != len(ids):
        raise HTTPException(404, '部分上传文件不存在或无权访问')
    if any(row['status'] != 'success' or not row['storage_prefix'] or row['deleted_at'] or row['deletion_id'] for row in rows):
        raise HTTPException(409, '所选文件尚未发布或已删除，无法下载')
    if len(rows) > 1 and sum(row['size_bytes'] for row in rows) > MAX_DOWNLOAD_BYTES:
        raise HTTPException(413, '批量下载总大小不能超过 2 GB')
    return rows


def prepare_download(row, directory):
    filename = Path(row['filename'].replace('\\', '/')).name or 'upload'
    path = directory / f'{uuid.uuid4().hex}-{filename}'
    prefix = row['storage_prefix']
    if filename.lower().endswith('.zip'):
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for entry in storage.list_files(prefix):
                relative = PurePosixPath(entry['path'])
                if relative.is_absolute() or '..' in relative.parts:
                    raise HTTPException(409, '文件版本包含无效路径，无法打包')
                target = directory / (uuid.uuid4().hex + '-' + relative.name)
                storage.get_file(prefix + entry['path'], target)
                archive.write(target, relative.as_posix())
                target.unlink(missing_ok=True)
    else:
        storage.get_file(prefix + row['entry_file'], path)
    return path


@router.get('/uploads/{upload_id}/download')
def download_upload(upload_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None,
                    user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        row = downloadable_uploads(conn, [upload_id], target)[0]
        audit(conn, user, 'file.download', {'id': str(upload_id), 'filename': row['filename']})
    directory = Path(tempfile.mkdtemp(prefix='pagehub-download-'))
    try:
        path = prepare_download(row, directory)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return FileResponse(path, filename=Path(row['filename'].replace('\\', '/')).name or 'upload',
                        media_type='application/octet-stream', background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))


@router.get('/download/check')
def check_download(ids: list[uuid.UUID] = Query(min_length=1, max_length=MAX_DOWNLOAD_ITEMS),
                   platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_DOWNLOAD_ITEMS:
        raise HTTPException(422, '每次最多批量下载 100 个文件')
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        rows = downloadable_uploads(conn, ids, target)
    size_bytes = sum(row['size_bytes'] for row in rows)
    if len(rows) > 1 and size_bytes > MAX_DOWNLOAD_BYTES:
        raise HTTPException(413, '批量下载总大小不能超过 2 GB')
    return {'count': len(rows), 'size_bytes': size_bytes}


@router.get('/download')
def download_uploads(ids: list[uuid.UUID] = Query(min_length=1, max_length=MAX_DOWNLOAD_ITEMS),
                     platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    ids = list(dict.fromkeys(ids))
    if len(ids) > MAX_DOWNLOAD_ITEMS:
        raise HTTPException(422, '每次最多批量下载 100 个文件')
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        rows = downloadable_uploads(conn, ids, target)
        audit(conn, user, 'file.download.batch', {'ids': [str(row['id']) for row in rows]})
    directory = Path(tempfile.mkdtemp(prefix='pagehub-download-'))
    archive_path = directory / 'files.zip'
    used_names = set()
    try:
        with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for index, row in enumerate(rows):
                source = prepare_download(row, directory)
                source_name = Path(row['filename'].replace('\\', '/')).name or f'upload-{index + 1}'
                stem, suffix = Path(source_name).stem, Path(source_name).suffix
                name, duplicate = source_name, 1
                while name.casefold() in used_names:
                    duplicate += 1
                    name = f'{stem}_{duplicate}{suffix}'
                used_names.add(name.casefold())
                archive.write(source, name)
                shutil.rmtree(source.parent, ignore_errors=True)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return FileResponse(archive_path, filename='PageHub-上传文件.zip', media_type='application/zip',
                        background=BackgroundTask(shutil.rmtree, directory, ignore_errors=True))


TREE = '''WITH RECURSIVE tree AS (
    SELECT id,parent_id,tenant_id,name,name AS path,ARRAY[id] AS path_ids FROM menus WHERE parent_id IS NULL
        AND (%s::uuid IS NULL OR tenant_id=%s)
    UNION ALL SELECT m.id,m.parent_id,m.tenant_id,m.name,t.path || ' / ' || m.name,t.path_ids || m.id
        FROM menus m JOIN tree t ON m.parent_id=t.id AND m.tenant_id=t.tenant_id
) '''


@router.get('')
def inventory(q: str = Query('', max_length=200), platform: bool = False, tenant: uuid.UUID | None = None,
              owner: uuid.UUID | None = None, menu: uuid.UUID | None = None, page_id: uuid.UUID | None = None,
              state: Literal['all', 'current', 'history', 'withdrawn', 'unbound', 'deleted', 'pending', 'failed'] = 'all',
              sort: Literal['newest', 'oldest', 'size'] = 'newest',
              page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100), user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    fields = '''j.id,j.filename,j.size_bytes AS source_bytes,j.status AS upload_status,j.error AS upload_error,
        j.created_at,v.id AS version_id,v.version_no,v.file_count,v.size_bytes AS original_bytes,
        CASE WHEN v.deleted_at IS NULL THEN coalesce(v.size_bytes,0) ELSE 0 END AS size_bytes,
        p.id AS page_id,p.title,p.current_version,p.menu_id,t.path AS menu_path,
        owner.username,owner.id AS uploaded_by,j.tenant_id,tenant.name AS tenant_name,
        CASE WHEN p.deleted_at IS NOT NULL THEN 'withdrawn' WHEN t.id IS NULL THEN 'unbound' ELSE 'attached' END AS binding,
        coalesce(p.current_version=v.id,false) AS is_current,
        CASE WHEN v.deleted_at IS NOT NULL THEN 'deleted' WHEN d.id IS NOT NULL THEN d.status
            WHEN v.id IS NOT NULL THEN 'active' WHEN j.status IN ('queued','processing') THEN 'temporary' ELSE 'unpublished' END AS file_state,
        d.id AS deletion_id,d.error AS deletion_error,p.purge_id'''
    source = '''FROM uploads j JOIN users owner ON owner.id=j.user_id JOIN tenants tenant ON tenant.id=j.tenant_id
        LEFT JOIN page_versions v ON v.id=j.id AND v.tenant_id=j.tenant_id
        LEFT JOIN pages p ON p.id=coalesce(v.page_id,j.page_id) AND p.tenant_id=j.tenant_id
        LEFT JOIN tree t ON t.id=p.menu_id AND t.tenant_id=p.tenant_id
        LEFT JOIN file_deletions d ON d.id=v.deletion_id
        WHERE (%s::uuid IS NULL OR j.tenant_id=%s) AND (%s::uuid IS NULL OR j.user_id=%s)
        AND (%s::uuid IS NULL OR p.id=%s) AND (j.filename ILIKE %s OR p.title ILIKE %s)
        AND (%s::uuid IS NULL OR p.menu_id IN (SELECT id FROM tree WHERE id=%s OR %s=ANY(path_ids)))'''
    filters = {
        'all': '', 'current': ' AND p.current_version=v.id AND v.deleted_at IS NULL',
        'history': ' AND p.current_version IS DISTINCT FROM v.id AND p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.id IS NOT NULL',
        'withdrawn': ' AND p.deleted_at IS NOT NULL AND v.deleted_at IS NULL AND v.id IS NOT NULL',
        'unbound': ' AND t.id IS NULL AND p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.id IS NOT NULL',
        'deleted': ' AND v.deleted_at IS NOT NULL',
        'pending': " AND d.status IN ('queued','processing') AND v.deleted_at IS NULL",
        'failed': " AND (d.status='failed' OR j.status='failed')",
    }
    params = (target, target, target, target, owner, owner, page_id, page_id, pattern(q), pattern(q), menu, menu, menu)
    order = {'newest': 'j.created_at DESC,j.id', 'oldest': 'j.created_at,j.id', 'size': 'size_bytes DESC,j.id'}[sort]
    with transaction(target, admin=admin) as conn:
        return paginated(conn, fields, source + filters[state], params, order, page, size, TREE)


@router.get('/summary')
def summary(platform: bool = False, tenant: uuid.UUID | None = None, owner: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        totals = conn.execute('''SELECT coalesce(sum(v.size_bytes),0) AS used_bytes,count(*) AS versions,
            coalesce(sum(v.size_bytes) FILTER (WHERE p.deleted_at IS NULL AND p.current_version=v.id),0) AS current_bytes,
            coalesce(sum(v.size_bytes) FILTER (WHERE p.deleted_at IS NULL AND p.current_version IS DISTINCT FROM v.id),0) AS history_bytes,
            coalesce(sum(v.size_bytes) FILTER (WHERE p.deleted_at IS NOT NULL),0) AS withdrawn_bytes,
            coalesce(sum(v.size_bytes) FILTER (WHERE v.deletion_id IS NOT NULL),0) AS pending_bytes
            FROM page_versions v JOIN pages p ON p.id=v.page_id AND p.tenant_id=v.tenant_id
            WHERE v.deleted_at IS NULL AND (%s::uuid IS NULL OR v.tenant_id=%s) AND (%s::uuid IS NULL OR v.uploaded_by=%s)''',
            (target, target, owner, owner)).fetchone()
        totals['quota_bytes'] = conn.execute('SELECT coalesce(sum(quota_bytes),0) AS n FROM tenants WHERE (%s::uuid IS NULL OR id=%s)', (target, target)).fetchone()['n']
        return totals


@router.get('/folders')
def folders(platform: bool = False, tenant: uuid.UUID | None = None, owner: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        return conn.execute('''SELECT m.id,m.parent_id,m.tenant_id,m.name,t.name AS tenant_name,
            coalesce(sum(v.size_bytes) FILTER (WHERE v.deleted_at IS NULL AND (%s::uuid IS NULL OR v.uploaded_by=%s)),0) AS direct_bytes
            FROM menus m JOIN tenants t ON t.id=m.tenant_id LEFT JOIN pages p ON p.menu_id=m.id AND p.tenant_id=m.tenant_id
            LEFT JOIN page_versions v ON v.page_id=p.id AND v.tenant_id=p.tenant_id
            WHERE (%s::uuid IS NULL OR m.tenant_id=%s)
            GROUP BY m.id,t.name ORDER BY m.position,m.name,m.id''', (owner, owner, target, target)).fetchall()


@router.get('/owners')
def owners(q: str = Query('', max_length=100), platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        return conn.execute('''SELECT u.id,u.username FROM users u WHERE (%s::uuid IS NULL OR u.tenant_id=%s
            OR EXISTS(SELECT 1 FROM uploads j WHERE j.user_id=u.id AND j.tenant_id=%s))
            AND u.username ILIKE %s ORDER BY u.username,u.id LIMIT 50''', (target, target, target, pattern(q))).fetchall()


@router.get('/versions/{version_id}/entries')
def entries(version_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None,
            path: str = Query('', max_length=1000), q: str = Query('', max_length=200),
            page: int = Query(1, ge=1), size: int = Query(100, ge=1, le=200), user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        version = conn.execute('''SELECT file_manifest,deleted_at,deletion_id,index_error FROM page_versions
            WHERE id=%s AND (%s::uuid IS NULL OR tenant_id=%s)''', (version_id, target, target)).fetchone()
    if not version:
        raise HTTPException(404, '版本不存在')
    if version['deleted_at'] or version['deletion_id']:
        raise HTTPException(409, '文件已删除或正在清理，不能查看文件清单')
    if version['file_manifest'] is None:
        return {'ready': False, 'error': version['index_error'], 'items': [], 'folders': [], 'total': 0, 'page': 1}
    files = version['file_manifest']
    directories = {}
    for entry in files:
        parts = entry['path'].split('/')[:-1]
        for i in range(len(parts)):
            folder = '/'.join(parts[:i + 1])
            directories[folder] = directories.get(folder, 0) + entry['size_bytes']
    prefix = path.strip('/')
    matching = [entry for entry in files if (not prefix or entry['path'].startswith(prefix + '/')) and q.strip().casefold() in entry['path'].casefold()]
    matching.sort(key=lambda entry: entry['path'])
    page = min(page, max(1, (len(matching) + size - 1) // size))
    return {'ready': True, 'items': matching[(page - 1) * size:page * size], 'total': len(matching), 'page': page,
            'folders': [{'path': name, 'size_bytes': amount} for name, amount in sorted(directories.items())]}


@router.get('/versions/{version_id}/preview')
def preview_version(version_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None,
                    user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    if not admin:
        raise HTTPException(403, '仅平台超级管理员可跨租户预览')
    with transaction(target, admin=True) as conn:
        row = conn.execute('''SELECT v.tenant_id,v.page_id,v.entry_file FROM page_versions v
            JOIN uploads j ON j.id=v.id AND j.tenant_id=v.tenant_id
            WHERE v.id=%s AND (%s::uuid IS NULL OR v.tenant_id=%s) AND v.deleted_at IS NULL
                AND v.deletion_id IS NULL AND j.status='success' ''',
            (version_id, target, target)).fetchone()
        if not row:
            raise HTTPException(404, '页面版本不存在或文件不可预览')
        audit(conn, user, 'file.preview', {'tenant_id': str(row['tenant_id']), 'page_id': str(row['page_id']),
                                           'version_id': str(version_id)})
    capability = sign({'tenant': str(row['tenant_id']), 'page': str(row['page_id']),
                       'version': str(version_id), 'platform': True, 'admin': str(user['id'])}, 300)
    return {'url': f'{settings.content_origin}/view/{capability}/{quote(row["entry_file"])}', 'expires_in': 300}


@router.post('/versions/{version_id}/rescan')
def rescan(version_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        row = conn.execute('''UPDATE page_versions SET file_manifest=NULL,index_error=NULL WHERE id=%s
            AND deleted_at IS NULL AND deletion_id IS NULL AND (%s::uuid IS NULL OR tenant_id=%s) RETURNING id''',
            (version_id, target, target)).fetchone()
        if not row:
            raise HTTPException(404, '没有可扫描的版本文件')
        return {'queued': True}


def create_deletion(conn, user, page, versions, kind):
    ids = [version['id'] for version in versions]
    job = conn.execute('''INSERT INTO file_deletions(tenant_id,user_id,page_id,kind,version_ids,size_bytes,file_count)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (page['tenant_id'], user['id'], page['id'], kind, ids,
        sum(v['size_bytes'] for v in versions), sum(v['file_count'] for v in versions))).fetchone()
    conn.execute('UPDATE page_versions SET deletion_id=%s WHERE id=ANY(%s)', (job['id'], ids))
    audit(conn, {'id': user['id'], 'tenant_id': page['tenant_id']}, 'files.delete.request',
          {'job_id': str(job['id']), 'kind': kind, 'page_id': str(page['id']), 'version_ids': [str(i) for i in ids], 'expected_bytes': job['size_bytes']})
    return job


class VersionDelete(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


@router.post('/versions/{version_id}/activate')
def activate(version_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        version = conn.execute('SELECT page_id,tenant_id FROM page_versions WHERE id=%s AND (%s::uuid IS NULL OR tenant_id=%s)', (version_id, target, target)).fetchone()
    if not version:
        raise HTTPException(404, '版本不存在')
    return rollback(version['page_id'], version_id, {**user, 'tenant_id': version['tenant_id']})


@router.post('/versions/delete', status_code=202)
def delete_versions(data: VersionDelete, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    ids = list(dict.fromkeys(data.ids))
    with transaction(target, admin=admin) as conn:
        rows = conn.execute('SELECT id,tenant_id,page_id FROM page_versions WHERE id=ANY(%s) AND (%s::uuid IS NULL OR tenant_id=%s)', (ids, target, target)).fetchall()
        if len(rows) != len(ids):
            raise HTTPException(404, '部分版本不存在或无权访问')
        for tenant_id in sorted({row['tenant_id'] for row in rows}):
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (tenant_id,))
        versions = conn.execute('SELECT * FROM page_versions WHERE id=ANY(%s) ORDER BY id FOR UPDATE', (ids,)).fetchall()
        jobs = []
        for version in versions:
            page = conn.execute('SELECT * FROM pages WHERE id=%s FOR UPDATE', (version['page_id'],)).fetchone()
            if version['deleted_at'] or version['deletion_id'] or page['purge_id']:
                raise HTTPException(409, '所选文件已删除或已有清理任务，请刷新查看')
            if page['current_version'] == version['id']:
                raise HTTPException(409, '当前版本不能单独删除，请先切换版本，或彻底删除整个页面')
            jobs.append(create_deletion(conn, user, page, [version], 'version'))
        return {'jobs': jobs}


class PagePurge(BaseModel):
    expected_version_ids: list[uuid.UUID] = Field(max_length=10000)


@router.post('/pages/{page_id}/delete', status_code=202)
def purge_page(page_id: uuid.UUID, data: PagePurge | None = None, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        page = conn.execute('SELECT * FROM pages WHERE id=%s AND (%s::uuid IS NULL OR tenant_id=%s)', (page_id, target, target)).fetchone()
        if not page:
            raise HTTPException(404, '页面不存在')
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (page['tenant_id'],))
        page = conn.execute('SELECT * FROM pages WHERE id=%s FOR UPDATE', (page_id,)).fetchone()
        if page['purge_id']:
            return conn.execute('SELECT * FROM file_deletions WHERE id=%s', (page['purge_id'],)).fetchone()
        versions = conn.execute('SELECT * FROM page_versions WHERE page_id=%s AND deleted_at IS NULL ORDER BY id FOR UPDATE', (page_id,)).fetchall()
        if data is not None and set(data.expected_version_ids) != {version['id'] for version in versions}:
            raise HTTPException(409, '页面版本已发生变化，请重新确认删除范围')
        if any(version['deletion_id'] for version in versions):
            raise HTTPException(409, '此页面存在未完成的版本清理任务，请完成或重试后再删除整页')
        job = create_deletion(conn, user, page, versions, 'page')
        conn.execute('UPDATE pages SET current_version=NULL,deleted_at=coalesce(deleted_at,now()),purge_id=%s WHERE id=%s', (job['id'], page_id))
        return job


@router.get('/pages/{page_id}/impact')
def impact(page_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        if not conn.execute('SELECT id FROM pages WHERE id=%s AND (%s::uuid IS NULL OR tenant_id=%s)', (page_id, target, target)).fetchone():
            raise HTTPException(404, '页面不存在')
        return conn.execute('''SELECT count(*) AS versions,coalesce(array_agg(id),ARRAY[]::uuid[]) AS version_ids,coalesce(sum(size_bytes),0) AS size_bytes,
            coalesce(sum(file_count),0) AS file_count FROM page_versions WHERE page_id=%s AND deleted_at IS NULL''', (page_id,)).fetchone()


@router.get('/deletions')
def deletions(platform: bool = False, tenant: uuid.UUID | None = None, owner: uuid.UUID | None = None,
              page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        return paginated(conn, 'd.*,p.title,u.username,t.name AS tenant_name', '''FROM file_deletions d
            JOIN pages p ON p.id=d.page_id JOIN users u ON u.id=d.user_id JOIN tenants t ON t.id=d.tenant_id
            WHERE (%s::uuid IS NULL OR d.tenant_id=%s) AND (%s::uuid IS NULL OR EXISTS(
                SELECT 1 FROM page_versions v WHERE v.deletion_id=d.id AND v.uploaded_by=%s))''',
            (target, target, owner, owner), 'd.created_at DESC,d.id', page, size)


@router.post('/deletions/{job_id}/retry')
def retry(job_id: uuid.UUID, platform: bool = False, tenant: uuid.UUID | None = None, user=Depends(current_user)):
    target, admin = scope(user, platform, tenant)
    with transaction(target, admin=admin) as conn:
        job = conn.execute('''UPDATE file_deletions SET status='queued',error=NULL,claimed_at=NULL
            WHERE id=%s AND status='failed' AND (%s::uuid IS NULL OR tenant_id=%s) RETURNING *''', (job_id, target, target)).fetchone()
        if not job:
            raise HTTPException(409, '仅可重试有权限访问的失败任务')
        audit(conn, {'id': user['id'], 'tenant_id': job['tenant_id']}, 'files.delete.retry', {'job_id': str(job_id)})
        return job
