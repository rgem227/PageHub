import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query

from .auth import current_user
from .db import transaction
from .listing import paginated, pattern

router = APIRouter(prefix='/api', tags=['应用目录'])

TREE = '''WITH RECURSIVE tree AS (
    SELECT m.*,m.id AS root_id,m.name AS path,m.visible AS shown,ARRAY[m.id] AS path_ids FROM menus m WHERE parent_id IS NULL AND tenant_id=%s
    UNION ALL SELECT m.*,t.root_id,t.path || ' / ' || m.name,t.shown AND m.visible,t.path_ids || m.id
    FROM menus m JOIN tree t ON m.parent_id=t.id AND m.tenant_id=t.tenant_id
) '''


@router.get('/catalogue')
def catalogue(q: str = Query('', max_length=200), category: uuid.UUID | None = None,
              sort: Literal['position', 'name', 'updated'] = 'position',
              page: int = Query(1, ge=1), size: int = Query(48, ge=1, le=100), user=Depends(current_user)):
    prefix = TREE + ''', entries AS (
        SELECT t.*,count(p.id) AS page_count,min(p.id::text) AS single_page_id,
            min(p.title) AS single_page_title,max(p.created_at) AS published_at,
            max(coalesce(v.created_at,p.created_at)) AS updated_at
        FROM tree t LEFT JOIN pages p ON p.menu_id=t.id AND p.tenant_id=t.tenant_id AND p.deleted_at IS NULL
        LEFT JOIN page_versions v ON v.id=p.current_version AND v.tenant_id=t.tenant_id
        LEFT JOIN (SELECT parent_id FROM menus WHERE tenant_id=%s AND parent_id IS NOT NULL GROUP BY parent_id) branch ON branch.parent_id=t.id
        WHERE t.shown AND branch.parent_id IS NULL
        GROUP BY t.id,t.tenant_id,t.parent_id,t.name,t.position,t.flat_position,t.visible,t.root_id,t.path,t.shown,t.path_ids
    ) '''
    source = '''FROM entries e WHERE (%s::uuid IS NULL OR e.root_id=%s)
        AND (e.name ILIKE %s OR EXISTS(SELECT 1 FROM pages p WHERE p.menu_id=e.id AND p.tenant_id=e.tenant_id
            AND p.deleted_at IS NULL AND p.title ILIKE %s))'''
    term = pattern(q)
    fields = '''e.*,(SELECT coalesce(jsonb_agg(match),'[]'::jsonb) FROM (
        SELECT p.id,p.title,p.menu_id FROM pages p WHERE p.menu_id=e.id AND p.tenant_id=e.tenant_id AND p.deleted_at IS NULL
            AND %s <> '' AND p.title ILIKE %s ORDER BY p.title,p.id LIMIT 3) match) AS matches'''
    # 匹配页面只为当前页读取，目录本身不预载 HTML 或全部页面元数据。
    order = {'position': 'e.flat_position,e.position,e.name,e.id', 'name': 'e.name,e.id',
             'updated': 'e.updated_at DESC NULLS LAST,e.name,e.id'}[sort]
    with transaction(user['tenant_id']) as conn:
        # 搜索与租户规模差异较大，使用当前参数规划目录查询。
        total = conn.execute(prefix + 'SELECT count(*) AS n ' + source, (user['tenant_id'], user['tenant_id'], category, category, term, term), prepare=False).fetchone()['n']
        page = min(page, max(1, (total + size - 1) // size))
        items = conn.execute(prefix + 'SELECT ' + fields + ' ' + source + ' ORDER BY ' + order + ' LIMIT %s OFFSET %s',
                             (user['tenant_id'], user['tenant_id'], q.strip(), term, category, category, term, term, size, (page - 1) * size), prepare=False).fetchall()
    return {'items': items, 'total': total, 'page': page, 'size': size}


@router.get('/page-list')
def page_list(q: str = Query('', max_length=200), menu: uuid.UUID | None = None,
              page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100), user=Depends(current_user)):
    source = '''FROM pages p JOIN tree t ON t.id=p.menu_id AND t.tenant_id=p.tenant_id LEFT JOIN users u ON u.id=p.created_by
        LEFT JOIN page_versions v ON v.id=p.current_version AND v.tenant_id=p.tenant_id WHERE p.deleted_at IS NULL
        AND p.title ILIKE %s AND (%s::uuid IS NULL OR t.id=%s OR t.path_ids @> ARRAY[%s]::uuid[])'''
    prefix = '''WITH RECURSIVE tree AS (
        SELECT m.id,m.tenant_id,ARRAY[m.id] AS path_ids FROM menus m WHERE parent_id IS NULL AND tenant_id=%s
        UNION ALL SELECT m.id,m.tenant_id,t.path_ids || m.id FROM menus m JOIN tree t ON m.parent_id=t.id AND m.tenant_id=t.tenant_id
    ) '''
    with transaction(user['tenant_id']) as conn:
        return paginated(conn, 'p.*,u.username,v.size_bytes', source, (user['tenant_id'], pattern(q), menu, menu, menu),
                         'p.position,p.created_at DESC,p.id', page, size, prefix)


@router.get('/upload-list')
def upload_list(q: str = Query('', max_length=200),
                status: Literal['all', 'queued', 'processing', 'success', 'failed'] = 'all',
                page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100), user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return paginated(conn, '''j.id,j.filename,j.status,j.error,j.result,j.created_at,j.size_bytes,j.expanded_bytes,
            j.page_id,v.id AS version_id,v.deleted_at AS file_deleted_at,d.status AS deletion_status''',
                         '''FROM uploads j LEFT JOIN page_versions v ON v.id=j.id
                         LEFT JOIN file_deletions d ON d.id=v.deletion_id
                         WHERE j.filename ILIKE %s AND (%s='all' OR j.status=%s)''',
                         (pattern(q), status, status), 'j.created_at DESC,j.id', page, size)


@router.get('/menu-list')
def menu_list(q: str = Query('', max_length=200), page: int = Query(1, ge=1),
              size: int = Query(50, ge=1, le=100), user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return paginated(conn, '''t.*,
            (SELECT count(*) FROM pages p WHERE p.menu_id=t.id AND p.tenant_id=t.tenant_id AND p.deleted_at IS NULL) AS page_count,
            (SELECT coalesce(jsonb_agg(jsonb_build_object('title',p.title,'path',v.entry_file)
                ORDER BY p.position,p.title), '[]'::jsonb)
                FROM pages p JOIN page_versions v ON v.id=p.current_version AND v.tenant_id=p.tenant_id
                WHERE p.menu_id=t.id AND p.tenant_id=t.tenant_id AND p.deleted_at IS NULL AND v.deleted_at IS NULL) AS attached_files''',
                         'FROM tree t WHERE t.path ILIKE %s',
                         (user['tenant_id'], pattern(q)), 't.position,t.path,t.id', page, size, TREE)
