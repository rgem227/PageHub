import csv
import io
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, HttpUrl, field_validator

from .auth import current_user
from .config import settings
from .db import audit, transaction
from .listing import paginated, pattern
from .services import ensure_menus

router = APIRouter(prefix='/api', tags=['网站收藏'])


class CategoryData(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    position: int = Field(default=0, ge=-2147483648, le=2147483647)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError('分类名称不能为空')
        return value.strip()


class BookmarkData(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    url: HttpUrl = Field(max_length=2048)
    description: str = Field(default='', max_length=1000)
    category_id: uuid.UUID | None = None
    category_name: str | None = Field(default=None, max_length=60)
    # menu_path is accepted for old MCP clients and converted to its final segment only.
    menu_path: list[str] | None = Field(default=None, max_length=3)
    open_mode: Literal['external', 'embedded'] = 'external'
    position: int = Field(default=0, ge=-2147483648, le=2147483647)
    allow_duplicate: bool = False

    @field_validator('title')
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError('收藏名称不能为空')
        return value.strip()

    @field_validator('category_name')
    @classmethod
    def category_not_blank(cls, value):
        if value is not None and not value.strip():
            raise ValueError('分类名称不能为空')
        return value.strip() if value else value

    @field_validator('url', mode='before')
    @classmethod
    def clean_url(cls, value):
        if isinstance(value, str) and (any(ord(c) < 32 or ord(c) == 127 for c in value) or '\\' in value):
            raise ValueError('网站地址包含无效字符')
        return value

    @field_validator('url')
    @classmethod
    def url_without_credentials(cls, value):
        if value.username or value.password:
            raise ValueError('请勿在网站地址中保存用户名或密码')
        return value


class CategoryBatch(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class BookmarkMove(CategoryBatch):
    category_id: uuid.UUID


class BookmarkPlacement(BaseModel):
    target_id: uuid.UUID
    before: bool = True


def require_category(conn, category_id):
    row = conn.execute('SELECT * FROM bookmark_categories WHERE id=%s', (category_id,)).fetchone()
    if not row:
        raise HTTPException(404, '收藏分类不存在')
    return row


def require_bookmark(conn, bookmark_id):
    row = conn.execute('''SELECT b.*,c.name AS category_name FROM bookmarks b
        JOIN bookmark_categories c ON c.id=b.category_id AND c.tenant_id=b.tenant_id WHERE b.id=%s''', (bookmark_id,)).fetchone()
    if not row:
        raise HTTPException(404, '收藏不存在')
    return row


def bookmark_result(row):
    return {**row, 'kind': 'bookmark', 'access_url': settings.web_origin + '/?bookmark=' + str(row['id'])}


def category_by_name(conn, name, position=0):
    row = conn.execute('''INSERT INTO bookmark_categories(tenant_id,name,position)
        VALUES (current_setting('app.tenant_id')::uuid,%s,%s) ON CONFLICT (tenant_id,name)
        DO UPDATE SET name=excluded.name RETURNING *''', (name.strip(), position)).fetchone()
    return row


@router.get('/bookmark-categories')
def list_categories(include_empty: bool = False, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        return conn.execute('''SELECT c.id,c.name,c.position,c.created_at,c.updated_at,count(b.id) AS bookmark_count
            FROM bookmark_categories c LEFT JOIN bookmarks b ON b.category_id=c.id AND b.tenant_id=c.tenant_id
            WHERE NOT %s OR b.id IS NOT NULL GROUP BY c.id ORDER BY c.position,c.name,c.id''',
            (not include_empty,)).fetchall()


@router.post('/bookmark-categories', status_code=201)
def create_category(data: CategoryData, user=Depends(current_user)):
    try:
        with transaction(user['tenant_id']) as conn:
            row = conn.execute('INSERT INTO bookmark_categories(tenant_id,name,position) VALUES (%s,%s,%s) RETURNING *',
                               (user['tenant_id'], data.name, data.position)).fetchone()
            audit(conn, user, 'bookmark.category.create', {'id': str(row['id']), 'name': row['name']})
            return row
    except Exception as exc:
        from psycopg.errors import UniqueViolation
        if isinstance(exc, UniqueViolation):
            raise HTTPException(409, '收藏分類名称已存在')
        raise


@router.patch('/bookmark-categories/{category_id}')
def update_category(category_id: uuid.UUID, data: CategoryData, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        if not conn.execute('UPDATE bookmark_categories SET name=%s,position=%s,updated_at=now() WHERE id=%s RETURNING id',
                            (data.name, data.position, category_id)).fetchone():
            raise HTTPException(404, '收藏分类不存在')
        audit(conn, user, 'bookmark.category.update', {'id': str(category_id), 'name': data.name})
    return {'ok': True}


@router.delete('/bookmark-categories/{category_id}')
def delete_category(category_id: uuid.UUID, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        if conn.execute('SELECT id FROM bookmarks WHERE category_id=%s LIMIT 1', (category_id,)).fetchone():
            raise HTTPException(409, '此分类还有网站收藏，请先移动或删除收藏')
        if not conn.execute('DELETE FROM bookmark_categories WHERE id=%s RETURNING id', (category_id,)).fetchone():
            raise HTTPException(404, '收藏分类不存在')
        audit(conn, user, 'bookmark.category.delete', {'id': str(category_id)})
    return {'ok': True}


def get_bookmark(bookmark_id, user):
    with transaction(user['tenant_id']) as conn:
        return bookmark_result(require_bookmark(conn, bookmark_id))


@router.get('/bookmarks/export')
def export_bookmarks(q: str = Query('', max_length=200), category_id: uuid.UUID | None = None,
                     sort: Literal['position', 'name', 'updated'] = 'position', user=Depends(current_user)):
    source = '''FROM bookmarks b JOIN bookmark_categories c ON c.id=b.category_id AND c.tenant_id=b.tenant_id
        WHERE (%s::uuid IS NULL OR b.category_id=%s)
        AND (b.title ILIKE %s OR b.url ILIKE %s OR b.description ILIKE %s OR c.name ILIKE %s)'''
    order = {'position': 'c.position,c.name,b.position,b.title,b.id', 'name': 'b.title,b.id',
             'updated': 'b.updated_at DESC,b.id'}[sort]
    term = pattern(q)
    with transaction(user['tenant_id']) as conn:
        rows = conn.execute('SELECT b.title,b.url,c.name AS category_name,b.description,b.open_mode,b.created_at ' + source + ' ORDER BY ' + order,
                            (category_id, category_id, term, term, term, term)).fetchall()
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(['名称', '网址', '分类', '备注', '打开方式', '收藏时间'])
    for row in rows:
        values = [row['title'], row['url'], row['category_name'], row['description'],
                  '新标签页' if row['open_mode'] == 'external' else '尝试框架内打开', row['created_at'].isoformat()]
        writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value
                         for value in values])
    return Response('\ufeff' + output.getvalue(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': 'attachment; filename="PageHub-bookmarks.csv"'})


@router.get('/bookmarks/{bookmark_id}')
def get_bookmark_route(bookmark_id: uuid.UUID, user=Depends(current_user)):
    return get_bookmark(bookmark_id, user)


def resolve_category(conn, data):
    legacy = data.menu_path[-1].strip() if data.menu_path else None
    if data.menu_path and (any(not isinstance(part, str) or not part.strip() or len(part.strip()) > 60 for part in data.menu_path)):
        raise HTTPException(422, '旧菜单路径中每级分类名称需为 1–60 个字符')
    if sum(value is not None for value in (data.category_id, data.category_name, legacy)) > 1:
        raise HTTPException(422, '收藏分类只能指定一项')
    if data.category_id:
        return require_category(conn, data.category_id)
    if legacy:
        return category_by_name(conn, legacy)
    return category_by_name(conn, data.category_name or '未分类')


def save_bookmark(data, user, bookmark_id=None):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        current = require_bookmark(conn, bookmark_id) if bookmark_id else None
        category = resolve_category(conn, data)
        url = str(data.url)
        duplicate = conn.execute('SELECT id FROM bookmarks WHERE category_id=%s AND url=%s AND (%s::uuid IS NULL OR id<>%s)',
                                 (category['id'], url, bookmark_id, bookmark_id)).fetchone()
        if duplicate and not data.allow_duplicate and (not current or current['url'] != url or current['category_id'] != category['id']):
            raise HTTPException(409, '此分类已收藏相同网址，确认后可继续保存')
        values = (category['id'], data.title, url, data.description.strip(), data.open_mode, data.position)
        if bookmark_id:
            row = conn.execute('''UPDATE bookmarks SET category_id=%s,title=%s,url=%s,description=%s,open_mode=%s,
                position=%s,updated_at=now() WHERE id=%s RETURNING *''', (*values, bookmark_id)).fetchone()
        else:
            row = conn.execute('''INSERT INTO bookmarks(category_id,title,url,description,open_mode,position,tenant_id,created_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''', (*values, user['tenant_id'], user['id'])).fetchone()
        audit(conn, user, 'bookmark.update' if bookmark_id else 'bookmark.create', {'id': str(row['id']), 'category_id': str(category['id'])})
        return bookmark_result({**row, 'category_name': category['name']})


@router.post('/bookmarks', status_code=201)
def create_bookmark(data: BookmarkData, user=Depends(current_user)):
    return save_bookmark(data, user)


@router.patch('/bookmarks/{bookmark_id}')
def update_bookmark(bookmark_id: uuid.UUID, data: BookmarkData, user=Depends(current_user)):
    return save_bookmark(data, user, bookmark_id)


def checked_ids(conn, ids):
    ids = list(set(ids))
    rows = conn.execute('SELECT id FROM bookmarks WHERE id=ANY(%s) FOR UPDATE', (ids,)).fetchall()
    if len(rows) != len(ids):
        raise HTTPException(404, '部分收藏不存在，请刷新后重试')
    return ids


@router.post('/bookmarks/batch-delete')
def delete_bookmarks(data: CategoryBatch, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        ids = checked_ids(conn, data.ids)
        conn.execute('DELETE FROM bookmarks WHERE id=ANY(%s)', (ids,))
        audit(conn, user, 'bookmark.delete', {'ids': [str(i) for i in ids]})
    return {'deleted': len(ids)}


@router.post('/bookmarks/batch-move')
def move_bookmarks(data: BookmarkMove, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        category = require_category(conn, data.category_id)
        ids = checked_ids(conn, data.ids)
        conn.execute('UPDATE bookmarks SET category_id=%s,updated_at=now() WHERE id=ANY(%s)', (category['id'], ids))
        audit(conn, user, 'bookmark.move', {'ids': [str(i) for i in ids], 'category_id': str(category['id'])})
    return {'moved': len(ids)}


@router.post('/bookmarks/{bookmark_id}/move')
def move_bookmark(bookmark_id: uuid.UUID, data: BookmarkPlacement, user=Depends(current_user)):
    with transaction(user['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (user['tenant_id'],))
        source = require_bookmark(conn, bookmark_id)
        target = require_bookmark(conn, data.target_id)
        if source['category_id'] != target['category_id']:
            raise HTTPException(422, '收藏只能在同一分类内排序')
        rows = conn.execute('SELECT id FROM bookmarks WHERE category_id=%s ORDER BY position,title,id FOR UPDATE',
                            (source['category_id'],)).fetchall()
        ordered = [row['id'] for row in rows]
        if bookmark_id == data.target_id:
            return {'ok': True}
        source_index, target_index = ordered.index(bookmark_id), ordered.index(data.target_id)
        ordered[source_index], ordered[target_index] = ordered[target_index], ordered[source_index]
        with conn.cursor() as cursor:
            cursor.executemany('UPDATE bookmarks SET position=%s WHERE id=%s',
                               [(index, identifier) for index, identifier in enumerate(ordered)])
        audit(conn, user, 'bookmark.reorder', {'id': str(bookmark_id), 'target_id': str(data.target_id), 'before': data.before})
    return {'ok': True}


@router.get('/bookmarks')
def list_bookmarks(q: str = Query('', max_length=200), category_id: uuid.UUID | None = None,
                   page: int = Query(1, ge=1), size: int = Query(40, ge=1, le=100),
                   sort: Literal['position', 'name', 'updated'] = 'position', user=Depends(current_user)):
    source = '''FROM bookmarks b JOIN bookmark_categories c ON c.id=b.category_id AND c.tenant_id=b.tenant_id
        WHERE (%s::uuid IS NULL OR b.category_id=%s)
        AND (b.title ILIKE %s OR b.url ILIKE %s OR b.description ILIKE %s OR c.name ILIKE %s)'''
    order = {'position': 'c.position,c.name,b.position,b.title,b.id', 'name': 'b.title,b.id',
             'updated': 'b.updated_at DESC,b.id'}[sort]
    term = pattern(q)
    with transaction(user['tenant_id']) as conn:
        if category_id:
            require_category(conn, category_id)
        return paginated(conn, "b.*,c.name AS category_name,c.position AS category_position,'bookmark' AS kind", source,
                         (category_id, category_id, term, term, term, term), order, page, size)
