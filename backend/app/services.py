import json
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from . import storage
from .config import settings
from .db import audit, transaction
from .packages import menu_path


def ensure_menus(conn, tenant_id, parts):
    parent = None
    for part in menu_path(parts):
        if parent and conn.execute('SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL LIMIT 1',
                                   (tenant_id, parent)).fetchone():
            raise ValueError('此菜单已挂接页面，不能添加子菜单；请让用户提供新的菜单名称，或在后台菜单管理中创建新的末级菜单后重试')
        row = conn.execute('''SELECT id FROM menus WHERE tenant_id=%s AND parent_id IS NOT DISTINCT FROM %s AND name=%s''',
                           (tenant_id, parent, part)).fetchone()
        if not row:
            row = conn.execute('''INSERT INTO menus(tenant_id,parent_id,name,flat_position)
                VALUES (%s,%s,%s,(SELECT coalesce(max(flat_position),-1)+1 FROM menus WHERE tenant_id=%s)) RETURNING id''',
                               (tenant_id, parent, part, tenant_id)).fetchone()
        parent = row['id']
    return parent


def require_leaf_menu(conn, tenant_id, menu_id, excluding_page_id=None, allow_occupied=False):
    menu = conn.execute('SELECT name FROM menus WHERE id=%s AND tenant_id=%s', (menu_id, tenant_id)).fetchone()
    if not menu:
        raise ValueError('菜单不存在')
    if conn.execute('SELECT 1 FROM menus WHERE tenant_id=%s AND parent_id=%s LIMIT 1', (tenant_id, menu_id)).fetchone():
        raise ValueError(f'菜单“{menu["name"]}”已有子菜单，不能挂接页面；请让用户提供新的末级菜单名称，或在后台菜单管理中创建末级菜单后重试')
    if allow_occupied:
        return
    query = 'SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL'
    params = [tenant_id, menu_id]
    if excluding_page_id:
        query += ' AND id<>%s'
        params.append(excluding_page_id)
    if conn.execute(query + ' LIMIT 1', params).fetchone():
        raise ValueError(f'末级菜单“{menu["name"]}”已挂接页面，不能重复挂接；请让用户提供新的菜单名称，或在后台菜单管理中创建空闲末级菜单后重试')


def ensure_upload_menu(conn, tenant_id, parts, title, auto_create_child=False):
    parts = menu_path(parts)
    menu_id = ensure_menus(conn, tenant_id, parts)
    if not auto_create_child:
        return menu_id, parts, False

    state = conn.execute('''SELECT
        EXISTS(SELECT 1 FROM menus child WHERE child.tenant_id=m.tenant_id AND child.parent_id=m.id) AS has_children,
        EXISTS(SELECT 1 FROM pages p WHERE p.tenant_id=m.tenant_id AND p.menu_id=m.id AND p.deleted_at IS NULL) AS has_page
        FROM menus m WHERE m.id=%s AND m.tenant_id=%s''', (menu_id, tenant_id)).fetchone()
    if not state['has_children'] or state['has_page']:
        return menu_id, parts, False
    if len(parts) >= 3:
        raise ValueError('目标菜单已是三级节点，不能自动创建下级菜单；请提供可创建下级菜单的一级或二级路径')

    base = ' '.join(title.split())[:60].strip() or '新页面'
    name = base
    suffix_no = 2
    while True:
        existing = conn.execute('''SELECT
            EXISTS(SELECT 1 FROM menus child WHERE child.tenant_id=m.tenant_id AND child.parent_id=m.id) AS has_children,
            EXISTS(SELECT 1 FROM pages p WHERE p.tenant_id=m.tenant_id AND p.menu_id=m.id AND p.deleted_at IS NULL) AS has_page
            FROM menus m WHERE m.tenant_id=%s AND m.parent_id=%s AND m.name=%s''',
            (tenant_id, menu_id, name)).fetchone()
        if not existing or (not existing['has_children'] and not existing['has_page']):
            break
        suffix = f'（{suffix_no}）'
        name = base[:60 - len(suffix)].rstrip() + suffix
        suffix_no += 1

    child_path = [*parts, name]
    return ensure_menus(conn, tenant_id, child_path), child_path, True


def account_storage(user_id):
    # 用户换租户后，历史上传仍占账号配额；只能按已认证的 user_id 查询汇总。
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT u.quota_bytes,coalesce(sum(v.size_bytes),0) AS used_bytes
            FROM users u LEFT JOIN page_versions v ON v.uploaded_by=u.id AND v.deleted_at IS NULL
            WHERE u.id=%s GROUP BY u.id''', (user_id,)).fetchone()


def enqueue(user, filename, content, options):
    if not filename.lower().endswith(('.html', '.htm', '.zip')):
        raise HTTPException(422, '仅支持 .html、.htm 和 .zip')
    if not content or len(content) > settings.max_upload_mb * 1024**2:
        raise HTTPException(413, '文件为空或超过上传大小限制')
    if options.get('menu_path'):
        menu_path(options['menu_path'])
    if options.get('page_id'):
        try:
            options['page_id'] = str(uuid.UUID(options['page_id']))
        except ValueError:
            raise HTTPException(422, '页面 ID 无效')
    upload_id = uuid.uuid4()
    key = f'uploads/{user["tenant_id"]}/{upload_id}/source'
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source'
        source.write_bytes(content)
        storage.put_file(key, source)
    try:
        personal = account_storage(user['id'])
        if personal['used_bytes'] + len(content) > personal['quota_bytes']:
            raise HTTPException(413, '账号存储配额不足，请联系管理员调整')
        with transaction(user['tenant_id']) as conn:
            tenant = conn.execute('SELECT * FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],)).fetchone()
            if not tenant or not tenant['active'] or tenant['deleted_at']:
                raise HTTPException(403, '租户已停用')
            pending = conn.execute("SELECT count(*) AS n FROM uploads WHERE status IN ('queued','processing')").fetchone()['n']
            if pending >= 10:
                raise HTTPException(429, '本租户待处理任务已达上限')
            usage = conn.execute('SELECT coalesce(sum(size_bytes),0) AS n FROM page_versions WHERE deleted_at IS NULL').fetchone()['n']
            if usage + len(content) > tenant['quota_bytes']:
                raise HTTPException(413, '租户存储配额不足')
            if options.get('page_id') and not conn.execute('SELECT id FROM pages WHERE id=%s AND deleted_at IS NULL', (options['page_id'],)).fetchone():
                raise HTTPException(404, '页面不存在')
            conn.execute('INSERT INTO uploads(id,tenant_id,user_id,filename,options,size_bytes) VALUES (%s,%s,%s,%s,%s,%s)',
                         (upload_id, user['tenant_id'], user['id'], filename[:200], json.dumps(options), len(content)))
            audit(conn, user, 'upload.enqueue', {'upload_id': str(upload_id)})
    except Exception:
        storage.remove_prefix(key)
        raise
    return {'id': str(upload_id), 'status': 'queued'}


def page_info(conn, page_id):
    row = conn.execute('''SELECT p.*,v.entry_file,v.size_bytes FROM pages p
        LEFT JOIN page_versions v ON v.id=p.current_version WHERE p.id=%s AND p.deleted_at IS NULL''', (page_id,)).fetchone()
    if not row:
        raise HTTPException(404, '页面不存在')
    row['url'] = settings.web_origin + '/?page=' + str(row['id'])
    return row


def preview_url(user, page_id):
    from .security import sign
    with transaction(user['tenant_id']) as conn:
        row = page_info(conn, page_id)
        tenant = conn.execute('SELECT open_mode FROM tenants WHERE id=%s', (user['tenant_id'],)).fetchone()
        seconds = user['external_preview_minutes'] * 60 if tenant['open_mode'] == 'external' else 300
        value = sign({'tenant': str(user['tenant_id']), 'page': str(page_id), 'version': str(row['current_version']), 'user': str(user['id'])}, seconds)
    return {'url': f'{settings.content_origin}/view/{value}/{quote(row["entry_file"])}', 'expires_in': seconds}
