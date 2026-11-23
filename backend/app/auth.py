import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from psycopg.errors import UniqueViolation

from .config import settings
from .db import audit, transaction
from .security import digest, passwords, phone_number, token

router = APIRouter(prefix='/api/auth', tags=['认证'])


def rate_limit(key: str, limit=10, seconds=60):
    with transaction() as conn:
        row = conn.execute('''INSERT INTO rate_limits(key,count,expires_at) VALUES (%s,1,now()+%s*interval '1 second')
            ON CONFLICT(key) DO UPDATE SET count=CASE WHEN rate_limits.expires_at<now() THEN 1 ELSE rate_limits.count+1 END,
            expires_at=CASE WHEN rate_limits.expires_at<now() THEN excluded.expires_at ELSE rate_limits.expires_at END
            RETURNING count''', (key, seconds)).fetchone()
    if row['count'] > limit:
        raise HTTPException(429, '操作过于频繁，请稍后重试')


def current_user(request: Request):
    auth = request.headers.get('authorization', '')
    bearer = auth[7:] if auth.startswith('Bearer ') else None
    value = bearer or request.cookies.get(settings.session_cookie_name)
    if not value:
        raise HTTPException(401, {'message': '请登录或注册', 'register_url': settings.web_origin + '/?register=1'})
    with transaction(admin=True) as conn:
        if bearer:
            row = conn.execute('''SELECT u.*, c.scopes, c.id AS credential_id FROM mcp_credentials c
                JOIN users u ON u.id=c.user_id AND u.tenant_id=c.tenant_id
                JOIN tenants t ON t.id=u.tenant_id
                WHERE c.token_hash=%s AND c.active AND (c.expires_at IS NULL OR c.expires_at>now())
                    AND u.active AND t.active AND t.deleted_at IS NULL''', (digest(value),)).fetchone()
            if row:
                conn.execute('UPDATE mcp_credentials SET last_used_at=now() WHERE id=%s', (row['credential_id'],))
            elif request.url.path.startswith('/mcp'):
                row = conn.execute('''SELECT u.*,g.scopes,g.id AS oauth_grant_id FROM oauth_access_tokens a
                    JOIN oauth_grants g ON g.id=a.grant_id AND g.tenant_id=a.tenant_id
                    JOIN users u ON u.id=g.user_id AND u.tenant_id=g.tenant_id
                    JOIN tenants t ON t.id=u.tenant_id
                    WHERE a.token_hash=%s AND a.expires_at>now() AND g.revoked_at IS NULL
                        AND (g.expires_at IS NULL OR g.expires_at>now())
                        AND u.active AND t.active AND t.deleted_at IS NULL''', (digest(value),)).fetchone()
                if row:
                    conn.execute('''UPDATE oauth_grants SET last_used_at=now()
                        WHERE id=%s AND (last_used_at IS NULL OR last_used_at<now()-interval '1 minute')''',
                        (row['oauth_grant_id'],))
        else:
            row = conn.execute('''SELECT u.*,s.token_hash AS session_hash FROM sessions s JOIN users u ON u.id=s.user_id JOIN tenants t ON t.id=u.tenant_id
                WHERE s.token_hash=%s AND s.expires_at>now() AND u.active AND t.active AND t.deleted_at IS NULL''', (digest(value),)).fetchone()
    if not row:
        raise HTTPException(401, '登录或 MCP 凭证已失效')
    if row['must_change_password'] and request.url.path not in ('/api/auth/me', '/api/auth/password', '/api/auth/logout'):
        raise HTTPException(403, '首次登录请修改临时密码')
    if bearer and request.method not in ('GET', 'HEAD', 'OPTIONS') and request.url.path.startswith('/api/'):
        if 'write' not in row['scopes']:
            raise HTTPException(403, '当前凭证只有读取权限')
    return row


def require_admin(user=Depends(current_user)):
    if user['role'] != 'superadmin' or user.get('credential_id'):
        raise HTTPException(403, '需要平台超级管理员网页登录权限')
    return user


def public_user(user):
    return {k: user[k] for k in ('id', 'tenant_id', 'username', 'full_name', 'phone', 'role', 'theme', 'directory_view', 'must_change_password')}


def start_session(conn, response, user_id):
    value = token()
    conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,now()+interval '7 days')", (digest(value), user_id))
    conn.execute('UPDATE users SET last_activity_at=now() WHERE id=%s', (user_id,))
    response.set_cookie(settings.session_cookie_name, value, httponly=True, secure=settings.environment == 'production', samesite='strict', max_age=7*86400, path='/')


class PhoneInput(BaseModel):
    phone: str


@router.post('/sms')
def send_sms(data: PhoneInput, request: Request):
    phone = phone_number(data.phone)
    rate_limit('sms-ip:' + request.client.host, 10, 3600)
    rate_limit('sms-phone:' + phone, 1, 60)
    development_mode = settings.sms_mode == 'development' and settings.environment == 'development'
    temporary_mode = settings.sms_mode == 'temporary' and settings.environment == 'production'
    if not (development_mode or temporary_mode):
        raise HTTPException(503, '尚未配置真实短信服务')
    code = f'{secrets.randbelow(1000000):06d}'
    with transaction() as conn:
        conn.execute('''INSERT INTO sms_codes(phone,code_hash,expires_at) VALUES (%s,%s,now()+interval '5 minutes')
            ON CONFLICT(phone) DO UPDATE SET code_hash=excluded.code_hash,expires_at=excluded.expires_at,attempts=0,sent_at=now()''', (phone, digest(code)))
    return {
        'message': '临时验证码模式，短信未发送' if temporary_mode else '本地开发验证码，不会发送短信',
        'development_code': code,
        'verification_mode': 'temporary' if temporary_mode else 'development',
    }


def consume_code(phone, code):
    valid = False
    with transaction() as conn:
        row = conn.execute('SELECT * FROM sms_codes WHERE phone=%s FOR UPDATE', (phone,)).fetchone()
        if row:
            valid = row['attempts'] < 5 and row['code_hash'] == digest(code) and conn.execute('SELECT %s>now() AS valid', (row['expires_at'],)).fetchone()['valid']
            if valid:
                conn.execute('DELETE FROM sms_codes WHERE phone=%s', (phone,))
            else:
                conn.execute('UPDATE sms_codes SET attempts=attempts+1 WHERE phone=%s', (phone,))
    if not valid:
        raise HTTPException(400, '验证码错误、已使用或已过期')


class RegisterInput(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r'^[a-zA-Z_\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff]*$')
    full_name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=10, max_length=128)
    phone: str
    code: str = Field(pattern=r'^\d{6}$')

    @field_validator('full_name')
    @classmethod
    def clean_full_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('请输入姓名')
        return value


@router.post('/register')
def register(data: RegisterInput, response: Response, request: Request):
    rate_limit('register:' + request.client.host, 10, 3600)
    phone = phone_number(data.phone)
    consume_code(phone, data.code)
    hashed = passwords.hash(data.password)
    try:
        with transaction(admin=True) as conn:
            tenant = conn.execute('INSERT INTO tenants(name,quota_bytes) VALUES (%s,%s) RETURNING id',
                                  (data.username + '的空间', settings.default_quota_mb * 1024**2)).fetchone()
            user = conn.execute('INSERT INTO users(tenant_id,username,full_name,phone,password_hash) VALUES (%s,%s,%s,%s,%s) RETURNING *',
                                (tenant['id'], data.username, data.full_name, phone, hashed)).fetchone()
            audit(conn, user, 'register', {})
            start_session(conn, response, user['id'])
            return public_user(user)
    except UniqueViolation:
        raise HTTPException(409, '用户名或手机号已注册')


class LoginInput(BaseModel):
    account: str = Field(max_length=80)
    password: str = Field(max_length=128)


@router.post('/login')
def login(data: LoginInput, response: Response, request: Request):
    rate_limit('login:' + request.client.host, 20, 60)
    with transaction(admin=True) as conn:
        user = conn.execute('''SELECT u.* FROM users u JOIN tenants t ON t.id=u.tenant_id
            WHERE (u.username=%s OR u.phone=%s) AND u.active AND t.active AND t.deleted_at IS NULL''', (data.account, data.account)).fetchone()
    if not user or not passwords.verify(data.password, user['password_hash']):
        raise HTTPException(401, '账号、密码错误或租户已停用')
    with transaction(user['tenant_id']) as conn:
        start_session(conn, response, user['id'])
        audit(conn, user, 'login', {})
    return public_user(user)


@router.get('/me')
def me(user=Depends(current_user)):
    return public_user(user)


@router.post('/heartbeat')
def heartbeat(user=Depends(current_user)):
    if not user.get('session_hash'):
        raise HTTPException(403, '工作台心跳仅支持网页登录')
    with transaction(admin=True) as conn:
        session = conn.execute('''UPDATE sessions SET last_seen_at=now()
            WHERE token_hash=%s AND last_seen_at<now()-interval '1 minute' RETURNING user_id''',
            (user['session_hash'],)).fetchone()
        if session:
            conn.execute('''UPDATE users SET last_activity_at=now()
                WHERE id=%s AND (last_activity_at IS NULL OR last_activity_at<now()-interval '1 minute')''',
                (session['user_id'],))
    return {'ok': True}


@router.post('/logout')
def logout(request: Request, response: Response):
    with transaction() as conn:
        conn.execute('DELETE FROM sessions WHERE token_hash=%s', (digest(request.cookies.get(settings.session_cookie_name, '')),))
    response.delete_cookie(settings.session_cookie_name, path='/')
    return {'ok': True}


class PasswordInput(BaseModel):
    old_password: str
    new_password: str = Field(min_length=10, max_length=128)


@router.post('/password')
def change_password(data: PasswordInput, response: Response, user=Depends(current_user)):
    if not passwords.verify(data.old_password, user['password_hash']):
        raise HTTPException(400, '原密码错误')
    if data.old_password == data.new_password:
        raise HTTPException(400, '新密码不能与原密码相同')
    hashed = passwords.hash(data.new_password)
    with transaction(user['tenant_id']) as conn:
        conn.execute('UPDATE users SET password_hash=%s,must_change_password=false WHERE id=%s', (hashed, user['id']))
        conn.execute('DELETE FROM sessions WHERE user_id=%s', (user['id'],))
        conn.execute('UPDATE mcp_credentials SET active=false WHERE user_id=%s', (user['id'],))
        conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE user_id=%s', (user['id'],))
        conn.execute('DELETE FROM oauth_authorization_codes WHERE user_id=%s', (user['id'],))
        start_session(conn, response, user['id'])
        audit(conn, user, 'password.change', {})
    return {'ok': True}
