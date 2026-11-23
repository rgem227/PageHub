import shutil
import uuid
from datetime import datetime, timezone
from typing import Literal

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from .auth import require_admin
from .config import settings
from .db import audit, transaction
from .security import passwords, phone_number, token
from .listing import paginated, pattern

router = APIRouter(prefix='/api/admin', tags=['平台管理'], dependencies=[Depends(require_admin)])


@router.get('/tenants')
def tenants(user=Depends(require_admin)):
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT t.*,
            (SELECT count(*) FROM users WHERE tenant_id=t.id) AS users,
            (SELECT count(*) FROM pages WHERE tenant_id=t.id AND deleted_at IS NULL) AS pages,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE tenant_id=t.id AND deleted_at IS NULL) AS storage_bytes
            FROM tenants t ORDER BY t.created_at DESC''').fetchall()


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    quota_mb: int = Field(default=1024, gt=0, le=1048576)


@router.post('/tenants')
def create_tenant(data: TenantCreate, user=Depends(require_admin)):
    with transaction(admin=True) as conn:
        row = conn.execute('INSERT INTO tenants(name,quota_bytes) VALUES (%s,%s) RETURNING id', (data.name, data.quota_mb * 1024**2)).fetchone()
        audit(conn, user, 'admin.tenant.create', {'id': str(row['id']), 'name': data.name})
    return row


class TenantUpdate(TenantCreate):
    active: bool = True


@router.patch('/tenants/{tenant_id}')
def update_tenant(tenant_id: uuid.UUID, data: TenantUpdate, user=Depends(require_admin)):
    if tenant_id == user['tenant_id'] and not data.active:
        raise HTTPException(400, '不能停用当前管理员所属租户')
    with transaction(admin=True) as conn:
        row = conn.execute('UPDATE tenants SET name=%s,quota_bytes=%s,active=%s WHERE id=%s AND deleted_at IS NULL RETURNING id',
                           (data.name, data.quota_mb * 1024**2, data.active, tenant_id)).fetchone()
        if not row:
            raise HTTPException(404, '租户不存在')
        if not data.active:
            conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE tenant_id=%s', (tenant_id,))
            conn.execute('DELETE FROM oauth_authorization_codes WHERE tenant_id=%s', (tenant_id,))
        audit(conn, user, 'admin.tenant.update', {'id': str(tenant_id), **data.model_dump()})
    return {'ok': True}


@router.delete('/tenants/{tenant_id}')
def delete_tenant(tenant_id: uuid.UUID, user=Depends(require_admin)):
    if tenant_id == user['tenant_id']:
        raise HTTPException(400, '不能删除当前管理员所属租户')
    with transaction(admin=True) as conn:
        if not conn.execute('UPDATE tenants SET active=false,deleted_at=now() WHERE id=%s RETURNING id', (tenant_id,)).fetchone():
            raise HTTPException(404, '租户不存在')
        conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE tenant_id=%s', (tenant_id,))
        conn.execute('DELETE FROM oauth_authorization_codes WHERE tenant_id=%s', (tenant_id,))
        audit(conn, user, 'admin.tenant.delete', {'id': str(tenant_id), 'data_retained': True})
    return {'ok': True, 'message': '租户已逻辑删除，数据保留'}


@router.get('/users')
def users():
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT u.id,u.username,u.full_name,u.phone,u.role,u.active,u.tenant_id,u.quota_bytes,t.name AS tenant_name,u.created_at,
            (SELECT count(*) FROM pages WHERE created_by=u.id AND deleted_at IS NULL) AS pages,
            (SELECT count(*) FROM uploads WHERE user_id=u.id) AS uploads,
            (SELECT count(*) FROM uploads WHERE user_id=u.id AND status='failed') AS failed,
            (SELECT count(*) FROM page_versions WHERE uploaded_by=u.id AND deleted_at IS NULL) AS versions,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE uploaded_by=u.id AND deleted_at IS NULL) AS storage_bytes,
            (SELECT max(created_at) FROM uploads WHERE user_id=u.id) AS last_upload_at
            FROM users u JOIN tenants t ON t.id=u.tenant_id ORDER BY u.created_at DESC''').fetchall()


@router.get('/user-activity')
def user_activity():
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT
            count(*) FILTER (WHERE EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=u.id
                AND s.expires_at>now() AND s.last_seen_at>=now()-interval '5 minutes')) AS online,
            count(*) FILTER (WHERE u.last_activity_at>=now()-interval '7 days') AS active_7d,
            count(*) FILTER (WHERE u.last_activity_at<now()-interval '7 days'
                AND u.last_activity_at>=now()-interval '30 days') AS inactive_8_30d,
            count(*) FILTER (WHERE u.last_activity_at<now()-interval '30 days') AS inactive_30d,
            count(*) FILTER (WHERE u.last_activity_at IS NULL) AS never_active
            FROM users u JOIN tenants t ON t.id=u.tenant_id
            WHERE t.active AND t.deleted_at IS NULL''').fetchone()


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$')
    full_name: str = Field(min_length=1, max_length=60)
    phone: str
    tenant_id: uuid.UUID
    quota_mb: int = Field(default=1024, gt=0, le=1048576)


@router.post('/users')
def create_user(data: UserCreate, user=Depends(require_admin)):
    phone = phone_number(data.phone)
    password = token()[:20]
    hashed = passwords.hash(password)
    with transaction(admin=True) as conn:
        if not conn.execute('SELECT id FROM tenants WHERE id=%s AND active AND deleted_at IS NULL', (data.tenant_id,)).fetchone():
            raise HTTPException(400, '目标租户不可用')
        if not data.full_name.strip():
            raise HTTPException(422, '请输入姓名')
        row = conn.execute('''INSERT INTO users(tenant_id,username,full_name,phone,password_hash,must_change_password,quota_bytes)
            VALUES (%s,%s,%s,%s,%s,true,%s) RETURNING id''',
            (data.tenant_id, data.username, data.full_name.strip(), phone, hashed, data.quota_mb * 1024**2)).fetchone()
        audit(conn, user, 'admin.user.create', {'id': str(row['id'])})
    return {**row, 'username': data.username, 'temporary_password': password, 'url': settings.web_origin}


class BulkDeactivate(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator('user_ids')
    @classmethod
    def unique_users(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('账号列表不能重复')
        return value


@router.post('/users/deactivate')
def bulk_deactivate(data: BulkDeactivate, user=Depends(require_admin)):
    with transaction(admin=True) as conn:
        rows = conn.execute("SELECT id FROM users WHERE id=ANY(%s) AND role='user' AND active FOR UPDATE",
                            (data.user_ids,)).fetchall()
        if len(rows) != len(data.user_ids):
            raise HTTPException(409, '所选账号已变化或包含不可停用账号，请刷新后重试')
        ids = [row['id'] for row in rows]
        conn.execute('UPDATE users SET active=false WHERE id=ANY(%s)', (ids,))
        conn.execute('DELETE FROM sessions WHERE user_id=ANY(%s)', (ids,))
        conn.execute('UPDATE mcp_credentials SET active=false WHERE user_id=ANY(%s)', (ids,))
        conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE user_id=ANY(%s)', (ids,))
        conn.execute('DELETE FROM oauth_authorization_codes WHERE user_id=ANY(%s)', (ids,))
        for user_id in ids:
            audit(conn, user, 'admin.user.bulk_deactivate', {'id': str(user_id)})
    return {'ok': True, 'count': len(ids)}


class UserUpdate(BaseModel):
    tenant_id: uuid.UUID
    full_name: str | None = Field(default=None, min_length=1, max_length=60)
    active: bool = True
    quota_mb: int | None = Field(default=None, gt=0, le=1048576)


@router.patch('/users/{user_id}')
def update_user(user_id: uuid.UUID, data: UserUpdate, user=Depends(require_admin)):
    if user_id == user['id']:
        raise HTTPException(400, '不能在此处调整自身归属或状态')
    with transaction(admin=True) as conn:
        if not conn.execute('SELECT id FROM tenants WHERE id=%s AND active AND deleted_at IS NULL', (data.tenant_id,)).fetchone():
            raise HTTPException(400, '目标租户不可用')
        old = conn.execute('SELECT * FROM users WHERE id=%s AND role=\'user\' FOR UPDATE', (user_id,)).fetchone()
        if not old:
            raise HTTPException(404, '普通用户不存在')
        if data.full_name is not None and not data.full_name.strip():
            raise HTTPException(422, '请输入姓名')
        conn.execute('UPDATE users SET tenant_id=%s,full_name=%s,active=%s,quota_bytes=%s WHERE id=%s',
                     (data.tenant_id, data.full_name.strip() if data.full_name is not None else old['full_name'],
                      data.active, data.quota_mb * 1024**2 if data.quota_mb else old['quota_bytes'], user_id))
        changed_identity = old['tenant_id'] != data.tenant_id or old['active'] != data.active
        if changed_identity:
            conn.execute('DELETE FROM sessions WHERE user_id=%s', (user_id,))
            conn.execute('UPDATE mcp_credentials SET active=false WHERE user_id=%s', (user_id,))
            conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE user_id=%s', (user_id,))
            conn.execute('DELETE FROM oauth_authorization_codes WHERE user_id=%s', (user_id,))
        audit(conn, user, 'admin.user.update', {'id': str(user_id), 'tenant_id': str(data.tenant_id), 'full_name': data.full_name, 'active': data.active, 'quota_mb': data.quota_mb})
    return {'ok': True, 'message': '账号设置已保存' if not changed_identity else '归属或状态已调整；原有页面保留在原租户，登录和 MCP 凭证已失效'}


@router.get('/resources')
def resources(include_storage: bool = True):
    storage_stats = {}
    if include_storage:
        with transaction(admin=True) as conn:
            db = conn.execute('SELECT pg_database_size(current_database()) AS size_bytes').fetchone()
            jobs = conn.execute('SELECT status,count(*) AS count FROM uploads GROUP BY status').fetchall()
            stored = conn.execute('SELECT coalesce(sum(size_bytes),0) AS size_bytes FROM page_versions WHERE deleted_at IS NULL').fetchone()
        storage_stats = {'database_bytes': db['size_bytes'], 'page_storage_bytes': stored['size_bytes'],
                         'jobs': jobs, 'storage_sampled_at': datetime.now(timezone.utc),
                         'storage_note': '页面版本逻辑用量，不包含暂存上传和未清理对象'}
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(settings.data_dir if settings.data_dir.exists() else settings.data_dir.parent)
    return {'cpu_percent': psutil.cpu_percent(interval=.05), 'memory_percent': memory.percent,
            'memory_total': memory.total, 'disk_total': disk.total, 'disk_used': disk.used,
            'memory_used': memory.used, 'disk_free': disk.free, 'sampled_at': datetime.now(timezone.utc),
            'scope': '当前 API 实例所在主机；多主机需要聚合监控', **storage_stats}


@router.get('/audit')
def audit_logs():
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT a.id,a.action,a.detail,a.created_at,u.username,t.name AS tenant_name
            FROM audit_logs a JOIN users u ON u.id=a.user_id JOIN tenants t ON t.id=a.tenant_id
            ORDER BY a.created_at DESC LIMIT 200''').fetchall()


@router.get('/overview')
def overview():
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT
            (SELECT count(*) FROM tenants WHERE deleted_at IS NULL) AS tenants,
            (SELECT count(*) FROM users) AS users,
            (SELECT count(*) FROM pages WHERE deleted_at IS NULL) AS pages,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE deleted_at IS NULL) AS storage_bytes,
            (SELECT count(*) FROM uploads WHERE status='failed') AS failed,
            (SELECT count(*) FROM uploads WHERE status IN ('queued','processing')) AS pending,
            (SELECT count(*) FROM users u WHERE (SELECT coalesce(sum(size_bytes),0)
                FROM page_versions v WHERE v.uploaded_by=u.id AND v.deleted_at IS NULL) >= u.quota_bytes*.8) AS near_quota''').fetchone()


@router.get('/tenant-options')
def tenant_options(q: str = Query('', max_length=100), include: uuid.UUID | None = None):
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT id,name FROM tenants WHERE active AND deleted_at IS NULL
            AND (name ILIKE %s OR id=%s) ORDER BY (id=%s) DESC NULLS LAST,name,id LIMIT 30''',
                            (pattern(q), include, include)).fetchall()


@router.get('/records/{kind}')
def records(kind: Literal['tenants', 'users', 'uploads', 'audit'], q: str = Query('', max_length=200),
            status: Literal['all', 'active', 'inactive', 'deleted', 'queued', 'processing', 'success', 'failed', 'pending', 'near_quota'] = 'all',
            activity: Literal['all', 'online', 'active_7d', 'inactive_8_30d', 'inactive_30d', 'never'] = 'all',
            tenant: uuid.UUID | None = None, entity: uuid.UUID | None = None,
            action: str = Query('', max_length=100), after: datetime | None = None, before: datetime | None = None,
            page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)):
    if after and after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    if before and before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    if after and before and after > before:
        raise HTTPException(422, '开始时间不能晚于结束时间')
    term = pattern(q)
    if kind == 'tenants':
        fields = '''t.*,(SELECT count(*) FROM users WHERE tenant_id=t.id) AS users,
            (SELECT count(*) FROM pages WHERE tenant_id=t.id AND deleted_at IS NULL) AS pages,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE tenant_id=t.id AND deleted_at IS NULL) AS storage_bytes'''
        base = 'FROM tenants t WHERE t.name ILIKE %s AND (%s::uuid IS NULL OR t.id=%s)'
        params = [term, entity, entity]
        status_sql = {'all': '', 'active': ' AND t.active AND t.deleted_at IS NULL',
                      'inactive': ' AND NOT t.active AND t.deleted_at IS NULL', 'deleted': ' AND t.deleted_at IS NOT NULL'}
        activity_sql = {'all': ''}
        order = 't.created_at DESC,t.id'
    elif kind == 'users':
        fields = '''u.id,u.username,u.full_name,u.phone,u.role,u.active,u.tenant_id,u.quota_bytes,u.created_at,t.name AS tenant_name,
            u.last_activity_at,
            (SELECT count(*) FROM sessions s WHERE s.user_id=u.id AND s.expires_at>now()
                AND s.last_seen_at>=now()-interval '5 minutes') AS online_sessions,
            (SELECT count(*) FROM pages WHERE created_by=u.id AND deleted_at IS NULL) AS pages,
            (SELECT count(*) FROM uploads WHERE user_id=u.id) AS uploads,
            (SELECT count(*) FROM uploads WHERE user_id=u.id AND status='failed') AS failed,
            (SELECT count(*) FROM page_versions WHERE uploaded_by=u.id AND deleted_at IS NULL) AS versions,
            (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE uploaded_by=u.id AND deleted_at IS NULL) AS storage_bytes,
            (SELECT max(created_at) FROM uploads WHERE user_id=u.id) AS last_upload_at'''
        base = '''FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE (u.username ILIKE %s OR u.full_name ILIKE %s OR u.phone ILIKE %s)
            AND (%s::uuid IS NULL OR u.tenant_id=%s) AND (%s::uuid IS NULL OR u.id=%s)'''
        params = [term, term, term, tenant, tenant, entity, entity]
        status_sql = {'all': '', 'active': ' AND u.active', 'inactive': ' AND NOT u.active',
                      'near_quota': ' AND (SELECT coalesce(sum(size_bytes),0) FROM page_versions WHERE uploaded_by=u.id AND deleted_at IS NULL) >= u.quota_bytes*.8'}
        activity_sql = {
            'all': '',
            'online': " AND u.active AND t.active AND t.deleted_at IS NULL AND EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=u.id AND s.expires_at>now() AND s.last_seen_at>=now()-interval '5 minutes')",
            'active_7d': " AND u.last_activity_at>=now()-interval '7 days'",
            'inactive_8_30d': " AND u.last_activity_at<now()-interval '7 days' AND u.last_activity_at>=now()-interval '30 days'",
            'inactive_30d': " AND u.last_activity_at<now()-interval '30 days'",
            'never': ' AND u.last_activity_at IS NULL',
        }
        order = 'u.created_at DESC,u.id'
    elif kind == 'uploads':
        fields = 'j.id,j.filename,j.status,j.error,j.result,j.created_at,u.username,u.full_name,u.phone,t.name AS tenant_name'
        base = '''FROM uploads j JOIN users u ON u.id=j.user_id JOIN tenants t ON t.id=j.tenant_id
            WHERE (j.filename ILIKE %s OR u.username ILIKE %s OR u.full_name ILIKE %s OR u.phone ILIKE %s) AND (%s::uuid IS NULL OR j.tenant_id=%s)
            AND (%s::uuid IS NULL OR j.user_id=%s)'''
        params = [term, term, term, term, tenant, tenant, entity, entity]
        status_sql = {'all': '', 'pending': " AND j.status IN ('queued','processing')", **{s: " AND j.status='" + s + "'" for s in ('queued', 'processing', 'success', 'failed')}}
        activity_sql = {'all': ''}
        order = 'j.created_at DESC,j.id'
    else:
        fields = 'a.*,u.username,u.full_name,u.phone,t.name AS tenant_name'
        base = '''FROM audit_logs a JOIN users u ON u.id=a.user_id JOIN tenants t ON t.id=a.tenant_id
            WHERE (u.username ILIKE %s OR u.full_name ILIKE %s OR u.phone ILIKE %s OR a.action ILIKE %s) AND (%s::uuid IS NULL OR a.tenant_id=%s)
            AND a.action ILIKE %s AND (%s::timestamptz IS NULL OR a.created_at >= %s)
            AND (%s::timestamptz IS NULL OR a.created_at <= %s)'''
        params = [term, term, term, term, tenant, tenant, pattern(action), after, after, before, before]
        status_sql = {'all': ''}
        activity_sql = {'all': ''}
        order = 'a.created_at DESC,a.id DESC'
    if status not in status_sql:
        raise HTTPException(422, '此列表不支持所选状态')
    if activity not in activity_sql:
        raise HTTPException(422, '此列表不支持所选活跃情况')
    with transaction(admin=True) as conn:
        return paginated(conn, fields, base + status_sql[status] + activity_sql[activity], params, order, page, size)
