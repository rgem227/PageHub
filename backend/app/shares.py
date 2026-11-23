import mimetypes
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .auth import current_user, rate_limit
from .config import settings
from .db import audit, transaction
from .packages import safe_path
from .security import phone_number, sign
from .storage import s3

router = APIRouter(prefix='/api', tags=['页面分享'])


def share_url(row, tenant_id):
    expires = row['expires_at']
    if not row['available'] or row['revoked_at'] or (expires and expires <= datetime.now(expires.tzinfo)):
        return None
    seconds = None if expires is None else max(1, int((expires - datetime.now(expires.tzinfo)).total_seconds()))
    capability = sign({'tenant': str(tenant_id), 'page': str(row['page_id']),
                       'version': str(row['version_id']), 'share': str(row['id'])}, seconds)
    return f'{settings.content_origin}/view/{capability}/{quote(row["entry_file"])}'


@router.get('/menus/{menu_id}/shares')
def menu_shares(menu_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '分享记录仅支持网页登录管理')
    with transaction(user['tenant_id']) as conn:
        if not conn.execute('SELECT id FROM menus WHERE id=%s', (menu_id,)).fetchone():
            raise HTTPException(404, '菜单不存在')
        pages = conn.execute('''SELECT p.id,p.title,p.current_version,v.entry_file FROM pages p
            JOIN page_versions v ON v.id=p.current_version AND v.page_id=p.id AND v.tenant_id=p.tenant_id
            WHERE p.menu_id=%s AND p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL
            ORDER BY p.position,p.created_at DESC,p.id''', (menu_id,)).fetchall()
        shares = conn.execute('''SELECT s.id,s.page_id,p.title,s.version_id,s.expires_at,s.revoked_at,s.created_at,v.entry_file,
            (p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL) AS available
            FROM page_shares s JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
            JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
            WHERE s.created_by=%s AND s.page_id IN (SELECT id FROM pages WHERE menu_id=%s)
            ORDER BY s.created_at DESC LIMIT 100''', (user['id'], menu_id)).fetchall()
    for row in shares:
        row['url'] = share_url(row, user['tenant_id'])
    return {'pages': pages, 'shares': shares}


class ShareRecipientSearch(BaseModel):
    q: str = Field(min_length=2, max_length=80)


@router.post('/share-recipients')
def share_recipients(data: ShareRecipientSearch, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '账号分享仅支持网页登录管理')
    q = data.q.strip()
    if len(q) < 2:
        return []
    try:
        phone = phone_number(q)
    except HTTPException:
        phone = None
    if phone:
        rate_limit(f'share-recipient-phone:{user["id"]}', 30, 60)
        match, params = 'u.phone=%s', (phone,)
    else:
        term = '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        match, params = '(u.username ILIKE %s OR u.full_name ILIKE %s)', (term, term)
    with transaction(admin=True) as conn:
        return conn.execute(f'''SELECT u.id,u.username,u.full_name,t.name AS tenant_name
            FROM users u JOIN tenants t ON t.id=u.tenant_id
            WHERE u.id<>%s AND u.active AND NOT u.must_change_password AND t.active AND t.deleted_at IS NULL
                AND {match}
            ORDER BY u.username,u.id LIMIT 20''', (user['id'], *params)).fetchall()


@router.get('/shares')
def my_shares(user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '分享记录仅支持网页登录管理')
    with transaction(admin=True) as conn:
        public = conn.execute('''SELECT s.id,s.tenant_id,s.page_id,p.title,s.version_id,s.expires_at,s.revoked_at,s.created_at,
            v.entry_file,m.name AS menu_name,(p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL) AS available
            FROM page_shares s JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
            JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
            LEFT JOIN menus m ON m.id=p.menu_id AND m.tenant_id=p.tenant_id
            WHERE s.created_by=%s ORDER BY s.created_at DESC''', (user['id'],)).fetchall()
        accounts = conn.execute('''SELECT s.id,s.page_id,p.title,s.version_id,s.expires_at,s.revoked_at,
            s.recipient_removed_at,s.created_at,s.recipient_user_id,recipient.username AS recipient_username,
            recipient.full_name AS recipient_name,v.entry_file,m.name AS menu_name,
            (p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL
                AND t.active AND t.deleted_at IS NULL AND recipient.active AND rt.active AND rt.deleted_at IS NULL) AS available
            FROM account_shares s JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
            JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
            JOIN users recipient ON recipient.id=s.recipient_user_id AND recipient.tenant_id=s.recipient_tenant_id
            JOIN tenants rt ON rt.id=s.recipient_tenant_id JOIN tenants t ON t.id=s.tenant_id
            LEFT JOIN menus m ON m.id=p.menu_id AND m.tenant_id=p.tenant_id
            WHERE s.created_by=%s ORDER BY s.created_at DESC''', (user['id'],)).fetchall()
    rows = [{**row, 'share_type': 'link', 'status': ('已撤销' if row['revoked_at'] else '已过期' if row['expires_at'] and row['expires_at'] <= datetime.now(row['expires_at'].tzinfo) else '页面不可用' if not row['available'] else '有效')} for row in public]
    for row in accounts:
        expired = row['expires_at'] is not None and row['expires_at'] <= datetime.now(row['expires_at'].tzinfo)
        rows.append({**row, 'share_type': 'account', 'status': ('已撤销' if row['revoked_at'] else '接收人已移除' if row['recipient_removed_at'] else '已过期' if expired else '页面不可用' if not row['available'] else '永久有效' if row['expires_at'] is None else '有效')})
    for row in rows:
        if row['share_type'] == 'link':
            row['url'] = share_url(row, row['tenant_id'])
    return sorted(rows, key=lambda row: row['created_at'], reverse=True)


class ShareInput(BaseModel):
    page_id: uuid.UUID
    days: int | None = Field(default=7, ge=1, le=30)


@router.post('/menus/{menu_id}/shares')
def create_share(menu_id: uuid.UUID, data: ShareInput, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录创建分享')
    with transaction(user['tenant_id']) as conn:
        page = conn.execute('''SELECT p.id,p.title,p.current_version,v.entry_file FROM pages p
            JOIN page_versions v ON v.id=p.current_version AND v.page_id=p.id AND v.tenant_id=p.tenant_id
            WHERE p.id=%s AND p.menu_id=%s AND p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL''',
            (data.page_id, menu_id)).fetchone()
        if not page:
            raise HTTPException(404, '页面不在此菜单中或当前版本不可分享')
        row = conn.execute('''INSERT INTO page_shares(tenant_id,page_id,version_id,created_by,expires_at)
            VALUES (%s,%s,%s,%s,CASE WHEN %s::integer IS NULL THEN NULL ELSE now()+%s::integer*interval '1 day' END)
            RETURNING id,expires_at,created_at''',
            (user['tenant_id'], page['id'], page['current_version'], user['id'], data.days, data.days)).fetchone()
        audit(conn, user, 'share.create', {'id': str(row['id']), 'page_id': str(page['id']), 'version_id': str(page['current_version']), 'days': data.days})
    result = {**row, 'page_id': page['id'], 'version_id': page['current_version'], 'title': page['title'], 'available': True, 'revoked_at': None, 'entry_file': page['entry_file']}
    return {**row, 'page_id': page['id'], 'title': page['title'], 'available': True, 'url': share_url(result, user['tenant_id'])}


class AccountShareInput(BaseModel):
    page_id: uuid.UUID
    recipient_user_id: uuid.UUID
    days: int | None = Field(default=7, ge=1, le=30)


@router.post('/menus/{menu_id}/account-shares')
def create_account_share(menu_id: uuid.UUID, data: AccountShareInput, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '账号分享仅支持网页登录管理')
    with transaction(admin=True) as conn:
        recipient = conn.execute('''SELECT u.id,u.tenant_id,u.username,u.full_name FROM users u
            JOIN tenants t ON t.id=u.tenant_id WHERE u.id=%s AND u.active AND NOT u.must_change_password
                AND t.active AND t.deleted_at IS NULL''', (data.recipient_user_id,)).fetchone()
    if not recipient or recipient['id'] == user['id']:
        raise HTTPException(404, '接收账号不可用')
    with transaction(user['tenant_id']) as conn:
        page = conn.execute('''SELECT p.id,p.title,p.current_version FROM pages p
            JOIN page_versions v ON v.id=p.current_version AND v.page_id=p.id AND v.tenant_id=p.tenant_id
            WHERE p.id=%s AND p.menu_id=%s AND p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL''',
            (data.page_id, menu_id)).fetchone()
        if not page:
            raise HTTPException(404, '页面不在此菜单中或当前版本不可分享')
        row = conn.execute('''INSERT INTO account_shares(tenant_id,recipient_tenant_id,recipient_user_id,page_id,version_id,created_by,expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,CASE WHEN %s::integer IS NULL THEN NULL ELSE now()+%s::integer*interval '1 day' END)
            RETURNING id,created_at,expires_at''',
            (user['tenant_id'], recipient['tenant_id'], recipient['id'], page['id'], page['current_version'], user['id'], data.days, data.days)).fetchone()
        audit(conn, user, 'account_share.create', {'id': str(row['id']), 'page_id': str(page['id']), 'recipient_user_id': str(recipient['id']), 'version_id': str(page['current_version']), 'days': data.days})
    return {**row, 'page_id': page['id'], 'title': page['title'], 'recipient_username': recipient['username'], 'recipient_name': recipient['full_name']}


@router.get('/shares/received')
def received_shares(user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '账号分享仅支持网页登录')
    with transaction(admin=True) as conn:
        return conn.execute('''SELECT s.id,s.page_id,p.title,s.version_id,s.expires_at,s.revoked_at,s.created_at,
            u.username AS sender_username,u.full_name AS sender_name,t.name AS sender_tenant,
            (p.deleted_at IS NULL AND v.deleted_at IS NULL AND v.deletion_id IS NULL
                AND t.active AND t.deleted_at IS NULL AND owner.active) AS available
            FROM account_shares s JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
            JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
            JOIN users owner ON owner.id=s.created_by AND owner.tenant_id=s.tenant_id
            JOIN tenants t ON t.id=s.tenant_id JOIN users u ON u.id=s.created_by
            WHERE s.recipient_user_id=%s AND s.recipient_tenant_id=%s AND s.recipient_removed_at IS NULL
            ORDER BY s.created_at DESC''', (user['id'], user['tenant_id'])).fetchall()


def authorized_account_share(share_id, user):
    with transaction(admin=True) as conn:
        row = conn.execute('''SELECT s.id,s.tenant_id,s.page_id,s.version_id,v.entry_file,v.storage_prefix
            FROM account_shares s JOIN pages p ON p.id=s.page_id AND p.tenant_id=s.tenant_id
            JOIN page_versions v ON v.id=s.version_id AND v.page_id=s.page_id AND v.tenant_id=s.tenant_id
            JOIN tenants owner_tenant ON owner_tenant.id=s.tenant_id
            JOIN users owner ON owner.id=s.created_by AND owner.tenant_id=s.tenant_id
            JOIN users recipient ON recipient.id=s.recipient_user_id AND recipient.tenant_id=s.recipient_tenant_id
            JOIN tenants recipient_tenant ON recipient_tenant.id=s.recipient_tenant_id
            WHERE s.id=%s AND s.recipient_user_id=%s AND s.recipient_tenant_id=%s
                AND s.revoked_at IS NULL AND s.recipient_removed_at IS NULL
                AND (s.expires_at IS NULL OR s.expires_at>now()) AND p.deleted_at IS NULL
                AND v.deleted_at IS NULL AND v.deletion_id IS NULL AND owner_tenant.active
                AND owner_tenant.deleted_at IS NULL AND recipient_tenant.active
                AND recipient_tenant.deleted_at IS NULL AND owner.active AND recipient.active''',
            (share_id, user['id'], user['tenant_id'])).fetchone()
    if not row:
        raise HTTPException(403, '账号分享已撤销、过期或不可用')
    return row


@router.get('/shares/received/{share_id}/preview')
def received_preview(share_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '账号分享仅支持网页登录')
    share = authorized_account_share(share_id, user)
    return {'url': f'/api/shares/received/{share_id}/content/{quote(share["entry_file"], safe="/")}'}


@router.get('/shares/received/{share_id}/content/{path:path}')
def received_content(share_id: uuid.UUID, path: str, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '账号分享仅支持网页登录')
    share = authorized_account_share(share_id, user)
    try:
        path = safe_path(path)
    except ValueError:
        raise HTTPException(400, '非法路径')
    key = share['storage_prefix'] + path
    headers = {'Content-Security-Policy': f"sandbox allow-scripts; frame-ancestors {settings.web_origin}; object-src 'none'; base-uri 'none'",
               'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'}
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


@router.delete('/shares/accounts/{share_id}')
def revoke_account_share(share_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录撤销分享')
    with transaction(user['tenant_id']) as conn:
        row = conn.execute('''UPDATE account_shares SET revoked_at=now() WHERE id=%s AND created_by=%s
            AND revoked_at IS NULL RETURNING id,page_id,recipient_user_id''', (share_id, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, '账号分享不存在或已撤销')
        audit(conn, user, 'account_share.revoke', {'id': str(row['id']), 'page_id': str(row['page_id']), 'recipient_user_id': str(row['recipient_user_id'])})
    return {'ok': True}


@router.delete('/shares/received/{share_id}')
def remove_received_share(share_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录移除分享')
    with transaction(user['tenant_id'], user_id=user['id']) as conn:
        row = conn.execute('''UPDATE account_shares SET recipient_removed_at=now() WHERE id=%s
            AND recipient_user_id=%s AND recipient_tenant_id=%s AND recipient_removed_at IS NULL
            RETURNING id,page_id''', (share_id, user['id'], user['tenant_id'])).fetchone()
        if not row:
            raise HTTPException(404, '接收的分享不存在或已移除')
        audit(conn, user, 'account_share.remove', {'id': str(row['id']), 'page_id': str(row['page_id'])})
    return {'ok': True}


@router.delete('/shares/{share_id}')
def revoke_share(share_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录撤销分享')
    with transaction(user['tenant_id']) as conn:
        row = conn.execute('''UPDATE page_shares SET revoked_at=now() WHERE id=%s AND created_by=%s AND revoked_at IS NULL
            RETURNING id,page_id''', (share_id, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, '分享不存在或已撤销')
        audit(conn, user, 'share.revoke', {'id': str(row['id']), 'page_id': str(row['page_id'])})
    return {'ok': True}


@router.delete('/shares/{share_type}/{share_id}/record')
def delete_share(share_type: Literal['link', 'account'], share_id: uuid.UUID, user=Depends(current_user)):
    if user.get('credential_id'):
        raise HTTPException(403, '只能通过网页登录删除分享')
    table = 'account_shares' if share_type == 'account' else 'page_shares'
    with transaction(user['tenant_id']) as conn:
        row = conn.execute(f'''DELETE FROM {table} WHERE id=%s AND created_by=%s AND tenant_id=%s
            RETURNING page_id''', (share_id, user['id'], user['tenant_id'])).fetchone()
        if not row:
            raise HTTPException(404, '分享记录不存在')
        audit(conn, user, 'account_share.delete' if share_type == 'account' else 'share.delete',
              {'id': str(share_id), 'page_id': str(row['page_id'])})
    return {'ok': True}
