import json
import secrets
import uuid
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from .auth import current_user
from .config import settings
from .db import audit, transaction
from .security import digest, token
from .services import account_storage, enqueue, ensure_menus, page_info, preview_url, require_leaf_menu

router = APIRouter(prefix='/api', tags=['工作空间'])


@router.get('/workspace')
def workspace(user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        tenant = conn.execute('SELECT * FROM tenants WHERE id=%s', (user['tenant_id'],)).fetchone()
        stats = conn.execute('''SELECT (SELECT count(*) FROM pages WHERE deleted_at IS NULL) AS pages,
            (SELECT count(*) FROM uploads) AS uploads,
            (SELECT count(*) FROM page_versions WHERE deleted_at IS NULL) AS versions,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE deleted_at IS NULL) AS storage_bytes''').fetchone()
    personal = account_storage(user['id'])
    personal['remaining_bytes'] = max(0, personal['quota_bytes'] - personal['used_bytes'])
    return {'tenant': tenant, 'stats': stats, 'storage': personal, 'limits': {'max_upload_mb': settings.max_upload_mb},
            'external_preview_minutes': user['external_preview_minutes']}


@router.get('/mcp/config')
def mcp_config(user=Depends(current_user)):
    parsed = urlsplit(settings.mcp_url)
    issuer = f'{parsed.scheme}://{parsed.netloc}'
    return {'url': settings.mcp_url,
            'authorization_server_metadata_url': issuer + '/.well-known/oauth-authorization-server',
            'protected_resource_metadata_url': issuer + '/.well-known/oauth-protected-resource/mcp',
            'token_endpoint_url': issuer + '/api/oauth/token'}


class Preferences(BaseModel):
    layout: Literal['side', 'top', 'flat'] | None = None
    open_mode: Literal['embedded', 'external'] | None = None
    theme: Literal['light', 'dark'] | None = None
    directory_view: Literal['compact', 'list'] | None = None
    external_preview_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)


@router.patch('/preferences')
def preferences(data: Preferences, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        if data.layout:
            conn.execute('UPDATE tenants SET layout=%s WHERE id=%s', (data.layout, user['tenant_id']))
        if data.open_mode:
            conn.execute('UPDATE tenants SET open_mode=%s WHERE id=%s', (data.open_mode, user['tenant_id']))
        if data.theme:
            conn.execute('UPDATE users SET theme=%s WHERE id=%s', (data.theme, user['id']))
        if data.directory_view:
            conn.execute('UPDATE users SET directory_view=%s WHERE id=%s', (data.directory_view, user['id']))
        if data.external_preview_minutes is not None:
            conn.execute('UPDATE users SET external_preview_minutes=%s WHERE id=%s', (data.external_preview_minutes, user['id']))
        audit(conn, user, 'preferences.update', data.model_dump(exclude_none=True))
    return {'ok': True}


@router.get('/menus')
def menus(user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return conn.execute('''WITH RECURSIVE menu_tree AS (
            SELECT id,parent_id,1 AS depth FROM menus WHERE parent_id IS NULL
            UNION ALL
            SELECT child.id,child.parent_id,parent.depth+1 FROM menus child
                JOIN menu_tree parent ON child.parent_id=parent.id
        )
            SELECT m.*,menu_tree.depth,count(p.id) AS page_count,min(p.id::text) AS single_page_id,
            min(p.title) AS single_page_title,
            coalesce(jsonb_agg(jsonb_build_object('title',p.title,'path',v.entry_file)
                ORDER BY p.position,p.title) FILTER (WHERE p.id IS NOT NULL AND v.id IS NOT NULL AND v.deleted_at IS NULL), '[]'::jsonb) AS attached_files,
            EXISTS (SELECT 1 FROM menus child WHERE child.tenant_id=m.tenant_id AND child.parent_id=m.id) AS has_children,
            (count(p.id)=0 AND NOT EXISTS (SELECT 1 FROM menus child WHERE child.tenant_id=m.tenant_id AND child.parent_id=m.id)) AS can_attach_page,
            (count(p.id)=0 AND menu_tree.depth<3) AS can_create_child
            FROM menus m JOIN menu_tree ON menu_tree.id=m.id LEFT JOIN pages p ON p.menu_id=m.id AND p.deleted_at IS NULL
            LEFT JOIN page_versions v ON v.id=p.current_version AND v.tenant_id=p.tenant_id
            GROUP BY m.id,menu_tree.depth ORDER BY m.position,m.name,m.id''').fetchall()


class MenuCreate(BaseModel):
    path: list[str] = Field(min_length=1, max_length=3)


@router.post('/menus')
def create_menu(data: MenuCreate, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        menu_id = ensure_menus(conn, user['tenant_id'], data.path)
        audit(conn, user, 'menu.create', {'path': data.path})
    return {'id': menu_id}


class MenuUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    parent_id: uuid.UUID | None = None
    position: int | None = Field(default=None, ge=-2147483648, le=2147483647)
    visible: bool | None = None
    confirm_hidden: bool = False


@router.patch('/menus/{menu_id}')
def update_menu(menu_id: uuid.UUID, data: MenuUpdate, user=Depends(current_user)):
    if 'name' in data.model_fields_set and (data.name is None or not data.name.strip()):
        raise HTTPException(422, '菜单名称不能为空')
    if 'position' in data.model_fields_set and data.position is None:
        raise HTTPException(422, '排序值不能为空')
    if 'visible' in data.model_fields_set and data.visible is None:
        raise HTTPException(422, '菜单显示状态不能为空')
    try:
        with transaction(user['tenant_id']) as conn:
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
            rows = conn.execute('SELECT id,parent_id,name,position,visible FROM menus').fetchall()
            menus_by_id = {row['id']: row for row in rows}
            if menu_id not in menus_by_id:
                raise HTTPException(404, '菜单不存在')
            current = menus_by_id[menu_id]
            parent_id = data.parent_id if 'parent_id' in data.model_fields_set else current['parent_id']
            parent_changed = parent_id != current['parent_id']
            if parent_id is not None and parent_id not in menus_by_id:
                raise HTTPException(404, '上级菜单不存在')

            def is_visible(menu):
                while menu is not None:
                    if not menus_by_id[menu]['visible']:
                        return False
                    menu = menus_by_id[menu]['parent_id']
                return True

            if parent_changed and is_visible(menu_id) and parent_id is not None and not is_visible(parent_id) and not data.confirm_hidden:
                raise HTTPException(409, '目标菜单或其上级菜单已隐藏，移动后此菜单及其页面在工作空间中不可见；确认后请再次提交 confirm_hidden=true')

            ancestor = parent_id
            depth = 0
            while ancestor is not None:
                if ancestor == menu_id:
                    raise HTTPException(422, '不能将菜单放到自身或子菜单下')
                depth += 1
                ancestor = menus_by_id[ancestor]['parent_id']
            subtree = [(menu_id, 1)]
            while subtree:
                node, height = subtree.pop()
                if depth + height > 3:
                    raise HTTPException(422, '移动后菜单层级超过三级，请先调整子菜单')
                subtree.extend((row['id'], height + 1) for row in rows if row['parent_id'] == node)
            if parent_changed and parent_id is not None and conn.execute(
                    'SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL LIMIT 1',
                    (user['tenant_id'], parent_id)).fetchone():
                raise HTTPException(409, '目标菜单已挂接页面，请先将页面移至其他空闲末级菜单，再将此菜单设为其子菜单')
            name = data.name.strip() if 'name' in data.model_fields_set else current['name']
            position = data.position if 'position' in data.model_fields_set else current['position']
            visible = data.visible if 'visible' in data.model_fields_set else current['visible']
            conn.execute('UPDATE menus SET name=%s,parent_id=%s,position=%s,visible=%s WHERE id=%s',
                         (name, parent_id, position, visible, menu_id))
            audit(conn, user, 'menu.update', {'id': str(menu_id), 'name': name,
                                           'old_parent_id': str(current['parent_id']) if current['parent_id'] else None,
                                           'parent_id': str(parent_id) if parent_id else None,
                                           'position': position, 'visible': visible})
    except UniqueViolation:
        raise HTTPException(409, '同级菜单名称已存在')
    return {'ok': True}


class MenuMove(BaseModel):
    target_id: uuid.UUID
    operation: Literal['swap', 'into']
    confirm_hidden: bool = False


@router.post('/menus/{menu_id}/move')
def move_menu(menu_id: uuid.UUID, data: MenuMove, user=Depends(current_user)):
    try:
        with transaction(user['tenant_id']) as conn:
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
            rows = conn.execute('SELECT id,parent_id,name,position,visible FROM menus').fetchall()
            menus_by_id = {row['id']: row for row in rows}
            source = menus_by_id.get(menu_id)
            target = menus_by_id.get(data.target_id)
            if source is None or target is None:
                raise HTTPException(404, '菜单不存在')
            if menu_id == data.target_id:
                return {'ok': True}

            if data.operation == 'swap':
                if source['parent_id'] != target['parent_id']:
                    raise HTTPException(422, '只能交换同级菜单的顺序')
                siblings = sorted((row for row in rows if row['parent_id'] == source['parent_id']),
                                  key=lambda row: (row['position'], row['name'], row['id']))
                order = [row['id'] for row in siblings]
                left, right = order.index(menu_id), order.index(data.target_id)
                order[left], order[right] = order[right], order[left]
                if len({row['position'] for row in siblings}) == len(siblings):
                    conn.execute('UPDATE menus SET position=%s WHERE id=%s', (target['position'], menu_id))
                    conn.execute('UPDATE menus SET position=%s WHERE id=%s', (source['position'], data.target_id))
                else:
                    with conn.cursor() as cursor:
                        cursor.executemany('UPDATE menus SET position=%s WHERE id=%s',
                                           [(index, identifier) for index, identifier in enumerate(order)])
                audit(conn, user, 'menu.reorder', {'id': str(menu_id), 'target_id': str(data.target_id),
                                                   'parent_id': str(source['parent_id']) if source['parent_id'] else None})
                return {'ok': True}

            if data.target_id == menu_id:
                raise HTTPException(422, '不能将菜单移动到自身下')
            target_page = conn.execute('SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL LIMIT 1',
                                       (user['tenant_id'], data.target_id)).fetchone()
            if target_page:
                raise HTTPException(409, '目标菜单已直接挂接页面，不能作为上级菜单')

            target_depth = 0
            ancestor = data.target_id
            while ancestor is not None:
                if ancestor == menu_id:
                    raise HTTPException(422, '不能将菜单移动到自身或子菜单下')
                target_depth += 1
                ancestor = menus_by_id[ancestor]['parent_id']

            subtree = [(menu_id, 1)]
            subtree_height = 0
            while subtree:
                node, height = subtree.pop()
                subtree_height = max(subtree_height, height)
                if target_depth + height > 3:
                    raise HTTPException(422, '移动后菜单层级超过三级')
                subtree.extend((row['id'], height + 1) for row in rows if row['parent_id'] == node)

            if conn.execute('SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL LIMIT 1',
                            (user['tenant_id'], menu_id)).fetchone() and conn.execute(
                    'SELECT 1 FROM menus WHERE tenant_id=%s AND parent_id=%s LIMIT 1',
                    (user['tenant_id'], menu_id)).fetchone():
                raise HTTPException(409, '已挂页面的菜单不能同时包含子菜单')

            def is_visible(identifier):
                while identifier is not None:
                    if not menus_by_id[identifier]['visible']:
                        return False
                    identifier = menus_by_id[identifier]['parent_id']
                return True

            if source['parent_id'] != data.target_id and is_visible(menu_id) and not is_visible(data.target_id) and not data.confirm_hidden:
                raise HTTPException(409, '目标菜单或其上级菜单已隐藏，移动后此菜单及其页面在工作空间中不可见；确认后请再次提交 confirm_hidden=true')
            if source['parent_id'] == data.target_id:
                return {'ok': True}

            position = conn.execute('SELECT coalesce(max(position),-1)+1 AS position FROM menus WHERE tenant_id=%s AND parent_id=%s',
                                    (user['tenant_id'], data.target_id)).fetchone()['position']
            conn.execute('UPDATE menus SET parent_id=%s,position=%s WHERE id=%s', (data.target_id, position, menu_id))
            audit(conn, user, 'menu.move', {'id': str(menu_id),
                                            'old_parent_id': str(source['parent_id']) if source['parent_id'] else None,
                                            'parent_id': str(data.target_id), 'subtree_height': subtree_height})
    except UniqueViolation:
        raise HTTPException(409, '目标菜单下已存在同名菜单')
    return {'ok': True}


@router.delete('/menus/{menu_id}')
def delete_menu(menu_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        if conn.execute('SELECT id FROM menus WHERE parent_id=%s UNION ALL SELECT id FROM pages WHERE menu_id=%s LIMIT 1', (menu_id, menu_id)).fetchone():
            raise HTTPException(409, '请先移走子菜单和页面')
        if not conn.execute('DELETE FROM menus WHERE id=%s RETURNING id', (menu_id,)).fetchone():
            raise HTTPException(404, '菜单不存在')
        audit(conn, user, 'menu.delete', {'id': str(menu_id)})
    return {'ok': True}


@router.get('/pages')
def pages(user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return conn.execute('''SELECT p.*,m.name AS menu_name,u.username,v.size_bytes FROM pages p
            JOIN menus m ON m.id=p.menu_id LEFT JOIN users u ON u.id=p.created_by
            LEFT JOIN page_versions v ON v.id=p.current_version WHERE p.deleted_at IS NULL
            ORDER BY p.position,p.created_at DESC,p.id''').fetchall()


class PagePlacement(BaseModel):
    target_id: uuid.UUID
    before: bool = True


@router.post('/pages/{page_id}/move')
def move_page(page_id: uuid.UUID, data: PagePlacement, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        rows = conn.execute('SELECT id FROM pages WHERE deleted_at IS NULL ORDER BY position,created_at DESC,id FOR UPDATE').fetchall()
        ordered = [row['id'] for row in rows]
        if page_id not in ordered or data.target_id not in ordered:
            raise HTTPException(404, '页面不存在')
        if page_id == data.target_id:
            return {'ok': True}
        source_index, target_index = ordered.index(page_id), ordered.index(data.target_id)
        ordered[source_index], ordered[target_index] = ordered[target_index], ordered[source_index]
        with conn.cursor() as cursor:
            cursor.executemany('UPDATE pages SET position=%s WHERE id=%s', [(index, identifier) for index, identifier in enumerate(ordered)])
        audit(conn, user, 'page.reorder', {'id': str(page_id), 'target_id': str(data.target_id), 'before': data.before})
    return {'ok': True}


@router.post('/menus/{menu_id}/flat-move')
def move_flat_menu(menu_id: uuid.UUID, data: PagePlacement, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        rows = conn.execute('''SELECT m.id FROM menus m WHERE NOT EXISTS (
            SELECT 1 FROM menus child WHERE child.tenant_id=m.tenant_id AND child.parent_id=m.id)
            ORDER BY m.flat_position,m.position,m.name,m.id FOR UPDATE''').fetchall()
        ordered = [row['id'] for row in rows]
        if menu_id not in ordered or data.target_id not in ordered:
            raise HTTPException(404, '菜单不存在或不是末级菜单')
        if menu_id == data.target_id:
            return {'ok': True}
        source_index, target_index = ordered.index(menu_id), ordered.index(data.target_id)
        ordered[source_index], ordered[target_index] = ordered[target_index], ordered[source_index]
        with conn.cursor() as cursor:
            cursor.executemany('UPDATE menus SET flat_position=%s WHERE id=%s',
                               [(index, identifier) for index, identifier in enumerate(ordered)])
        audit(conn, user, 'menu.flat_reorder', {'id': str(menu_id), 'target_id': str(data.target_id), 'before': data.before})
    return {'ok': True}


@router.post('/uploads', status_code=202)
def upload(file: UploadFile = File(...), title: str = Form(''), menu_path: str = Form('[]'),
           entry_file: str = Form(''), page_id: str = Form(''), user=Depends(current_user)):
    try:
        path = json.loads(menu_path)
    except json.JSONDecodeError:
        raise HTTPException(422, '菜单路径必须是 JSON 数组')
    content = file.file.read(settings.max_upload_mb * 1024**2 + 1)
    return enqueue(user, file.filename or '', content, {'title': title, 'menu_path': path, 'entry_file': entry_file, 'page_id': page_id})


@router.get('/uploads')
def uploads(user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return conn.execute('SELECT id,filename,status,error,result,created_at FROM uploads ORDER BY created_at DESC LIMIT 100').fetchall()


@router.get('/uploads/{upload_id}')
def upload_status(upload_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        row = conn.execute('SELECT id,filename,status,error,result,created_at FROM uploads WHERE id=%s', (upload_id,)).fetchone()
    if not row:
        raise HTTPException(404, '上传记录不存在')
    return row


class PageUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    menu_id: uuid.UUID | None = None
    parent_menu_id: uuid.UUID | None = None
    confirm_hidden: bool = False


@router.patch('/pages/{page_id}')
def update_page(page_id: uuid.UUID, data: PageUpdate, user=Depends(current_user)):
    if data.menu_id is not None and data.parent_menu_id is not None:
        raise HTTPException(422, '请在直接挂接菜单和移动到父菜单之间选择一种操作')
    try:
        with transaction(user['tenant_id']) as conn:
            conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
            current = page_info(conn, page_id)
            target_menu_id = current['menu_id']
            if data.parent_menu_id is not None:
                if current['menu_id'] is None:
                    raise HTTPException(409, '当前页面没有可移动的末级菜单')
                source = conn.execute('SELECT id,parent_id,name FROM menus WHERE id=%s AND tenant_id=%s',
                                      (current['menu_id'], user['tenant_id'])).fetchone()
                if conn.execute('SELECT 1 FROM menus WHERE tenant_id=%s AND parent_id=%s LIMIT 1',
                                (user['tenant_id'], source['id'])).fetchone():
                    raise HTTPException(409, '当前页面菜单已有子菜单，无法作为末级菜单移动')
                if data.parent_menu_id == source['id']:
                    raise HTTPException(422, '不能将菜单移动到自身下')
                target = conn.execute('''WITH RECURSIVE ancestors AS (
                    SELECT id,parent_id,visible,1 AS depth FROM menus WHERE id=%s AND tenant_id=%s
                    UNION ALL SELECT m.id,m.parent_id,m.visible,a.depth+1 FROM menus m
                        JOIN ancestors a ON m.id=a.parent_id AND m.tenant_id=%s
                ) SELECT max(depth) AS depth,coalesce(bool_and(visible),false) AS visible FROM ancestors''',
                                      (data.parent_menu_id, user['tenant_id'], user['tenant_id'])).fetchone()
                if target['depth'] is None:
                    raise HTTPException(404, '目标父菜单不存在')
                if target['depth'] >= 3:
                    raise HTTPException(422, '移动后菜单层级超过三级')
                if conn.execute('SELECT 1 FROM pages WHERE tenant_id=%s AND menu_id=%s AND deleted_at IS NULL LIMIT 1',
                                (user['tenant_id'], data.parent_menu_id)).fetchone():
                    raise HTTPException(409, '目标父菜单已直接挂接页面，不能作为上级菜单')
                if source['parent_id'] != data.parent_menu_id:
                    if not target['visible'] and not data.confirm_hidden:
                        raise HTTPException(409, '目标菜单或其上级菜单已隐藏，移动后页面在工作空间中不可见；确认后请再次提交 confirm_hidden=true')
                    conn.execute('UPDATE menus SET parent_id=%s WHERE id=%s', (data.parent_menu_id, source['id']))
                    audit(conn, user, 'menu.update', {'id': str(source['id']),
                                                      'old_parent_id': str(source['parent_id']) if source['parent_id'] else None,
                                                      'parent_id': str(data.parent_menu_id), 'page_id': str(page_id)})
            elif data.menu_id is not None:
                if data.menu_id != current['menu_id']:
                    require_leaf_menu(conn, user['tenant_id'], data.menu_id, page_id)
                    target = conn.execute('''WITH RECURSIVE ancestors AS (
                        SELECT id,parent_id,visible FROM menus WHERE id=%s AND tenant_id=%s
                        UNION ALL SELECT m.id,m.parent_id,m.visible FROM menus m
                            JOIN ancestors a ON m.id=a.parent_id AND m.tenant_id=%s
                    ) SELECT coalesce(bool_and(visible),false) AS visible FROM ancestors''',
                                          (data.menu_id, user['tenant_id'], user['tenant_id'])).fetchone()
                    if not target['visible'] and not data.confirm_hidden:
                        raise HTTPException(409, '目标菜单或其上级菜单已隐藏，移动后页面在工作空间中不可见；确认后请再次提交 confirm_hidden=true')
                target_menu_id = data.menu_id
            if not conn.execute('UPDATE pages SET title=%s,menu_id=%s WHERE id=%s RETURNING id',
                                (data.title, target_menu_id, page_id)).fetchone():
                raise HTTPException(404, '页面不存在')
            audit(conn, user, 'page.update', {'id': str(page_id), 'menu_id': str(target_menu_id) if target_menu_id else None})
    except UniqueViolation:
        raise HTTPException(409, '目标父菜单下已存在同名菜单')
    return {'ok': True}


@router.get('/pages/{page_id}/preview')
def preview(page_id: uuid.UUID, user=Depends(current_user)):
    return preview_url(user, page_id)


@router.get('/pages/{page_id}')
def get_page(page_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return page_info(conn, page_id)


@router.get('/pages/{page_id}/versions')
def versions(page_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        page_info(conn, page_id)
        return conn.execute('''SELECT v.id,v.entry_file,v.size_bytes,v.file_count,v.created_at,u.username,v.version_no,v.deletion_id,
            d.status AS deletion_status,d.error AS deletion_error
            FROM page_versions v LEFT JOIN users u ON u.id=v.uploaded_by LEFT JOIN file_deletions d ON d.id=v.deletion_id
            WHERE v.page_id=%s AND v.deleted_at IS NULL ORDER BY v.version_no DESC''', (page_id,)).fetchall()


@router.post('/pages/{page_id}/rollback/{version_id}')
def rollback(page_id: uuid.UUID, version_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        page_info(conn, page_id)
        if not conn.execute('SELECT id FROM page_versions WHERE id=%s AND page_id=%s AND deleted_at IS NULL AND deletion_id IS NULL', (version_id, page_id)).fetchone():
            raise HTTPException(404, '版本不存在')
        conn.execute('UPDATE pages SET current_version=%s WHERE id=%s', (version_id, page_id))
        audit(conn, user, 'page.rollback', {'page': str(page_id), 'version': str(version_id)})
    return {'ok': True}


@router.delete('/pages/{page_id}')
def delete_page(page_id: uuid.UUID, user=Depends(current_user)):
    # 逻辑删除保留版本并计入存储，防止反复删除绕过配额。
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        page_info(conn, page_id)
        conn.execute('UPDATE pages SET deleted_at=now() WHERE id=%s', (page_id,))
        audit(conn, user, 'page.delete', {'id': str(page_id), 'files_retained': True})
    return {'ok': True}


@router.get('/credentials')
def credentials(user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return conn.execute('''SELECT id,name,scopes,active,expires_at,last_used_at,created_at FROM mcp_credentials
            WHERE user_id=%s ORDER BY created_at DESC''', (user['id'],)).fetchall()


class CredentialInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    readonly: bool = False
    expires_in_days: Literal[7, 30, 90, 180, 365] | None = 90


@router.post('/credentials')
def new_credential(data: CredentialInput, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录创建 MCP 凭证')
    value = 'hub_' + token()
    with transaction(user['tenant_id']) as conn:
        row = conn.execute('''INSERT INTO mcp_credentials(tenant_id,user_id,name,token_hash,scopes,expires_at)
            VALUES (%s,%s,%s,%s,%s,CASE WHEN %s::integer IS NULL THEN NULL
                ELSE now()+(%s::integer * interval '1 day') END) RETURNING id,expires_at''',
            (user['tenant_id'], user['id'], data.name, digest(value), ['read'] if data.readonly else ['read', 'write'],
             data.expires_in_days, data.expires_in_days)).fetchone()
        audit(conn, user, 'credential.create', {'id': str(row['id']), 'expires_in_days': data.expires_in_days})
    return {**row, 'token': value}


@router.delete('/credentials/{credential_id}')
def delete_credential(credential_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        if not conn.execute('DELETE FROM mcp_credentials WHERE id=%s AND user_id=%s RETURNING id', (credential_id, user['id'])).fetchone():
            raise HTTPException(404, '凭证不存在')
        audit(conn, user, 'credential.delete', {'id': str(credential_id)})
    return {'ok': True}
