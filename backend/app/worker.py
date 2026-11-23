"""独立运行：python -m app.worker；通过 SKIP LOCKED 竞争任务。"""
import json
import logging
import tempfile
import time
import uuid
from pathlib import Path

from . import storage
from .config import settings
from .db import audit, pool, transaction
from .packages import extract
from .services import ensure_upload_menu, require_leaf_menu

log = logging.getLogger(__name__)


def process_one():
    # 事务持有上传行锁，worker 崩溃时自动释放；不把任务状态依赖于进程内存。
    with transaction(admin=True) as conn:
        job = conn.execute("SELECT * FROM uploads WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if not job:
            return False
        failed_prefix = None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                storage.get_file(f'uploads/{job["tenant_id"]}/{job["id"]}/source', root / 'source')
                info = extract(root / 'source', job['filename'], root / 'package', job['options'])
                # 在文件发布之前锁定租户，串行检查配额与菜单，避免并发上传突破配额。
                with conn.transaction():
                    tenant = conn.execute('SELECT * FROM tenants WHERE id=%s FOR UPDATE', (job['tenant_id'],)).fetchone()
                    uploader = conn.execute('SELECT * FROM users WHERE id=%s AND tenant_id=%s AND active FOR UPDATE', (job['user_id'], job['tenant_id'])).fetchone()
                    if not tenant or not tenant['active'] or tenant['deleted_at'] or not uploader:
                        raise ValueError('租户或上传者不可用')
                    usage = conn.execute('SELECT coalesce(sum(size_bytes),0) AS n FROM page_versions WHERE tenant_id=%s AND deleted_at IS NULL', (job['tenant_id'],)).fetchone()['n']
                    if usage + info['size'] > tenant['quota_bytes']:
                        raise ValueError('解压后大小超出剩余存储配额')
                    personal_usage = conn.execute('SELECT coalesce(sum(size_bytes),0) AS n FROM page_versions WHERE uploaded_by=%s AND deleted_at IS NULL', (job['user_id'],)).fetchone()['n']
                    if personal_usage + info['size'] > uploader['quota_bytes']:
                        raise ValueError('解压后大小超出账号剩余存储配额，请联系管理员调整')
                    menu_id, info['menus'], menu_path_auto_extended = ensure_upload_menu(
                        conn, job['tenant_id'], info['menus'], info['title'],
                        auto_create_child=job['options'].get('auto_create_child', False),
                    )
                    page_id = job['options'].get('page_id')
                    allow_occupied = False
                    if page_id:
                        existing = conn.execute('SELECT id,menu_id FROM pages WHERE id=%s AND tenant_id=%s AND deleted_at IS NULL', (page_id, job['tenant_id'])).fetchone()
                        if not existing:
                            raise ValueError('待更新页面不存在')
                        allow_occupied = existing['menu_id'] == menu_id
                    else:
                        page_id = str(job['id'])
                    require_leaf_menu(conn, job['tenant_id'], menu_id, page_id, allow_occupied)
                    if not job['options'].get('page_id'):
                        conn.execute('INSERT INTO pages(id,tenant_id,menu_id,title,created_by) VALUES (%s,%s,%s,%s,%s)',
                                     (page_id, job['tenant_id'], menu_id, info['title'], job['user_id']))
                    version_id = job['id']
                    prefix = f'tenants/{job["tenant_id"]}/pages/{page_id}/versions/{version_id}/'
                    failed_prefix = prefix
                    manifest = []
                    for path in (root / 'package').rglob('*'):
                        if path.is_file():
                            relative = path.relative_to(root / 'package').as_posix()
                            storage.put_file(prefix + relative, path)
                            manifest.append({'path': relative, 'size_bytes': path.stat().st_size})
                    number = conn.execute('SELECT coalesce(max(version_no),0)+1 AS n FROM page_versions WHERE page_id=%s', (page_id,)).fetchone()['n']
                    conn.execute('''INSERT INTO page_versions(id,tenant_id,page_id,uploaded_by,entry_file,storage_prefix,size_bytes,file_count,file_manifest,version_no)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                        (version_id, job['tenant_id'], page_id, job['user_id'], info['entry'], prefix, info['size'], info['file_count'], json.dumps(manifest), number))
                    conn.execute('UPDATE pages SET current_version=%s,title=%s,menu_id=%s WHERE id=%s', (version_id, info['title'], menu_id, page_id))
                    result = {'page_id': str(page_id), 'version_id': str(version_id), 'title': info['title'], 'menu_path': info['menus'],
                              'menu_path_auto_extended': menu_path_auto_extended,
                              'url': settings.web_origin + '/?page=' + str(page_id), 'size_bytes': info['size']}
                    conn.execute("UPDATE uploads SET status='success',page_id=%s,expanded_bytes=%s,result=%s WHERE id=%s",
                                 (page_id, info['size'], json.dumps(result), job['id']))
                    audit(conn, uploader, 'upload.published', result)
                    failed_prefix = None
            except Exception as exc:
                log.exception('上传任务处理失败: %s', job['id'])
                conn.execute("UPDATE uploads SET status='failed',error=%s WHERE id=%s", (str(exc)[:500], job['id']))
    for prefix in [f'uploads/{job["tenant_id"]}/{job["id"]}/', failed_prefix]:
        if prefix:
            try:
                storage.remove_prefix(prefix)
            except Exception:
                log.exception('暂存文件清理失败: %s', prefix)
    return True


def process_deletion():
    with transaction(admin=True) as conn:
        # 崩溃后过期租约可接管；目录删除幂等，文件路径不会被新版本复用。
        job = conn.execute("""SELECT * FROM file_deletions WHERE status='queued'
            OR (status='processing' AND claimed_at < now()-interval '5 minutes')
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
        if not job:
            return False
        job = conn.execute("UPDATE file_deletions SET status='processing',claimed_at=clock_timestamp(),error=NULL WHERE id=%s RETURNING *", (job['id'],)).fetchone()
        versions = conn.execute('SELECT id,storage_prefix FROM page_versions WHERE deletion_id=%s AND deleted_at IS NULL', (job['id'],)).fetchall()
    try:
        if {version['id'] for version in versions} != set(job['version_ids']):
            raise ValueError('清理任务的版本清单不匹配，已停止删除')
        for version in versions:
            expected = f'tenants/{job["tenant_id"]}/pages/{job["page_id"]}/versions/{version["id"]}/'
            if version['storage_prefix'] != expected:
                raise ValueError('版本存储目录不匹配，已停止删除')
            storage.remove_prefix(expected, permanent=True)
        with transaction(admin=True) as conn:
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (job['tenant_id'],))
            current = conn.execute('SELECT * FROM file_deletions WHERE id=%s FOR UPDATE', (job['id'],)).fetchone()
            if current['status'] != 'processing' or current['claimed_at'] != job['claimed_at']:
                return True
            conn.execute('UPDATE page_versions SET deleted_at=now(),file_manifest=NULL WHERE deletion_id=%s AND deleted_at IS NULL', (job['id'],))
            if job['kind'] == 'page':
                conn.execute('UPDATE pages SET purged_at=now(),menu_id=NULL WHERE id=%s AND purge_id=%s', (job['page_id'], job['id']))
            conn.execute("UPDATE file_deletions SET status='success',completed_at=now() WHERE id=%s", (job['id'],))
            audit(conn, {'id': job['user_id'], 'tenant_id': job['tenant_id']}, 'files.delete.complete', {'job_id': str(job['id']), 'released_bytes': job['size_bytes']})
    except Exception as exc:
        log.exception('文件删除失败: %s', job['id'])
        with transaction(admin=True) as conn:
            conn.execute("UPDATE file_deletions SET status='failed',error=%s WHERE id=%s AND status='processing' AND claimed_at=%s", (str(exc)[:500], job['id'], job['claimed_at']))
    return True


def index_manifest():
    with transaction(admin=True) as conn:
        version = conn.execute('''SELECT * FROM page_versions WHERE file_manifest IS NULL AND deleted_at IS NULL
            AND deletion_id IS NULL AND index_error IS NULL ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1''').fetchone()
        if not version:
            return False
        try:
            expected = f'tenants/{version["tenant_id"]}/pages/{version["page_id"]}/versions/{version["id"]}/'
            if version['storage_prefix'] != expected:
                raise ValueError('版本存储目录不匹配')
            entries = storage.list_files(expected)
            if len(entries) != version['file_count'] or sum(entry['size_bytes'] for entry in entries) != version['size_bytes']:
                raise ValueError('实际文件数量或大小与版本记录不一致，请检查存储')
            conn.execute('UPDATE page_versions SET file_manifest=%s WHERE id=%s', (json.dumps(entries), version['id']))
        except Exception as exc:
            log.exception('文件清单扫描失败: %s', version['id'])
            conn.execute('UPDATE page_versions SET index_error=%s WHERE id=%s', (str(exc)[:500], version['id']))
    return True


def main():
    settings.validate_runtime()
    logging.basicConfig(level=logging.INFO)
    pool.open(wait=True)
    try:
        while True:
            if not process_deletion() and not process_one() and not index_manifest():
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        pool.close()


if __name__ == '__main__':
    main()
