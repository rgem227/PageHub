import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import QueryParams

from .auth import current_user, rate_limit
from .config import settings
from .db import audit, transaction
from .security import digest, token

router = APIRouter(tags=['OAuth'])
OAUTH_PREFIX = '/api/oauth'
ACCESS_TOKEN_SECONDS = 3600
GRANT_DAYS = (7, 30, 90, 180, 365)
AUTH_FIELDS = ('response_type', 'client_id', 'redirect_uri', 'scope', 'state', 'code_challenge',
               'code_challenge_method', 'resource')


def _issuer() -> str:
    parsed = urlsplit(settings.mcp_url)
    return f'{parsed.scheme}://{parsed.netloc}'


def _resource() -> str:
    return settings.mcp_url.rstrip('/')


def _url(path: str) -> str:
    return _issuer() + path


def _oauth_error(error: str, description: str, status=400):
    return JSONResponse({'error': error, 'error_description': description}, status_code=status,
                        headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


def _redirect_uri_is_safe(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # Reject malformed or out-of-range ports.
        if not parsed.scheme or parsed.username or parsed.password or parsed.fragment or any(c.isspace() for c in value):
            return False
        scheme = parsed.scheme.lower()
        if scheme == 'https':
            return bool(parsed.hostname)
        if scheme == 'http':
            if not parsed.hostname:
                return False
            if parsed.hostname.lower() == 'localhost':
                return True
            try:
                return ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                return False
        return scheme not in ('javascript', 'data', 'file') and bool(re.fullmatch(r'[a-z][a-z0-9+.-]*', scheme))
    except ValueError:
        return False


class ClientRegistration(BaseModel):
    client_name: str | None = Field(default=None, max_length=120)
    redirect_uris: list[str] = Field(min_length=1, max_length=20)
    token_endpoint_auth_method: str = 'none'
    grant_types: list[str] = Field(default_factory=lambda: ['authorization_code', 'refresh_token'])
    response_types: list[str] = Field(default_factory=lambda: ['code'])
    scope: str | None = Field(default=None, max_length=80)

    @field_validator('redirect_uris')
    @classmethod
    def safe_redirect_uris(cls, value):
        if len(set(value)) != len(value) or any(not _redirect_uri_is_safe(uri) for uri in value):
            raise ValueError('redirect_uris 必须唯一且使用安全的 HTTPS、loopback HTTP 或应用回调地址')
        return value

    @field_validator('token_endpoint_auth_method')
    @classmethod
    def public_client_only(cls, value):
        if value != 'none':
            raise ValueError('仅支持公开客户端 token_endpoint_auth_method=none')
        return value

    @field_validator('grant_types')
    @classmethod
    def supported_grants(cls, value):
        if set(value) != {'authorization_code', 'refresh_token'}:
            raise ValueError('仅支持 authorization_code 和 refresh_token')
        return value

    @field_validator('response_types')
    @classmethod
    def supported_responses(cls, value):
        if value != ['code']:
            raise ValueError('仅支持 code 响应类型')
        return value

    @field_validator('scope')
    @classmethod
    def supported_scopes(cls, value):
        if value is not None:
            scopes = value.split()
            if not scopes or 'read' not in scopes or len(set(scopes)) != len(scopes) or set(scopes) - {'read', 'write'}:
                raise ValueError('scope 仅支持 read 和 write，且必须包含 read')
        return value


class AuthorizationDecision(BaseModel):
    response_type: str
    client_id: str
    redirect_uri: str
    state: str
    code_challenge: str
    code_challenge_method: str
    scope: str = 'read write'
    resource: str | None = None
    expires_in_days: int | None = Field(default=90)

    @field_validator('expires_in_days')
    @classmethod
    def supported_expiry(cls, value):
        if value is not None and value not in GRANT_DAYS:
            raise ValueError('授权期限仅支持 7、30、90、180、365 天或永久')
        return value


def _auth_request(query: QueryParams) -> dict:
    if len(str(query)) > 8192:
        raise HTTPException(400, 'OAuth 授权请求过长')
    values = {}
    for key in AUTH_FIELDS:
        items = query.getlist(key)
        if len(items) > 1:
            raise HTTPException(400, f'OAuth 参数 {key} 不能重复')
        if items:
            values[key] = items[0]
    for key in ('response_type', 'client_id', 'redirect_uri', 'state', 'code_challenge', 'code_challenge_method'):
        if not values.get(key):
            raise HTTPException(400, f'缺少 OAuth 参数 {key}')
    if values['response_type'] != 'code' or values['code_challenge_method'] != 'S256':
        raise HTTPException(400, '仅支持带 PKCE S256 的授权码流程')
    if len(values['state']) > 2048 or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', values['code_challenge']):
        raise HTTPException(400, 'OAuth state 或 PKCE challenge 格式无效')
    scope_text = values.get('scope', 'read write').strip()
    scopes = scope_text.split()
    if not scopes or len(set(scopes)) != len(scopes) or set(scopes) - {'read', 'write'} or 'read' not in scopes:
        raise HTTPException(400, 'OAuth scope 仅支持 read 和 write，且必须包含 read')
    if not _redirect_uri_is_safe(values['redirect_uri']):
        raise HTTPException(400, 'redirect_uri 不安全')
    resource = values.get('resource')
    if resource and resource.rstrip('/') != _resource():
        raise HTTPException(400, 'OAuth resource 与 PageHub MCP 地址不匹配')
    values['scope'] = ' '.join(scope for scope in ('read', 'write') if scope in scopes)
    values['resource'] = _resource()
    with transaction(admin=True) as conn:
        client = conn.execute('SELECT client_name,redirect_uris FROM oauth_clients WHERE client_id=%s',
                              (values['client_id'],)).fetchone()
    if not client or values['redirect_uri'] not in client['redirect_uris']:
        raise HTTPException(400, 'OAuth 客户端或 redirect_uri 未注册')
    values['client_name'] = client['client_name']
    return values


def _web_request_url(values: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps({key: values[key] for key in AUTH_FIELDS if key in values},
                                                   separators=(',', ':')).encode()).decode().rstrip('=')
    return f'{settings.web_origin}/?oauth_request={quote(encoded)}'


def _callback_uri(redirect_uri: str, fields: dict) -> str:
    parsed = urlsplit(redirect_uri)
    query = parsed.query + ('&' if parsed.query else '') + urlencode(fields)
    return parsed._replace(query=query).geturl()


def _require_session_user(user):
    if user.get('credential_id') or user.get('oauth_grant_id'):
        raise HTTPException(403, 'OAuth 授权需要网页登录')
    return user


@router.get('/.well-known/oauth-authorization-server')
def authorization_server_metadata():
    return {
        'issuer': _issuer(),
        'authorization_endpoint': _url(OAUTH_PREFIX + '/authorize'),
        'token_endpoint': _url(OAUTH_PREFIX + '/token'),
        'registration_endpoint': _url(OAUTH_PREFIX + '/register'),
        'revocation_endpoint': _url(OAUTH_PREFIX + '/revoke'),
        'scopes_supported': ['read', 'write'],
        'response_types_supported': ['code'],
        'grant_types_supported': ['authorization_code', 'refresh_token'],
        'token_endpoint_auth_methods_supported': ['none'],
        'revocation_endpoint_auth_methods_supported': ['none'],
        'code_challenge_methods_supported': ['S256'],
    }


def protected_resource_metadata():
    return {'resource': _resource(), 'authorization_servers': [_issuer()], 'scopes_supported': ['read', 'write'],
            'bearer_methods_supported': ['header'], 'resource_name': 'PageHub MCP'}


@router.get('/.well-known/oauth-protected-resource')
@router.get('/.well-known/oauth-protected-resource/mcp')
def protected_resource_discovery():
    return protected_resource_metadata()


@router.post(OAUTH_PREFIX + '/register', status_code=201)
def register_client(data: ClientRegistration, request: Request):
    rate_limit('oauth-register:' + (request.client.host if request.client else 'unknown'), 30, 3600)
    client_id = 'phc_' + token()
    name = (data.client_name or '').strip()[:120] or '未命名智能体'
    with transaction(admin=True) as conn:
        row = conn.execute('''INSERT INTO oauth_clients(client_id,client_name,redirect_uris)
            VALUES (%s,%s,%s) RETURNING created_at''', (client_id, name, data.redirect_uris)).fetchone()
    return {'client_id': client_id, 'client_id_issued_at': int(row['created_at'].timestamp()),
            'client_name': name, 'redirect_uris': data.redirect_uris,
            'grant_types': data.grant_types, 'response_types': data.response_types,
            'token_endpoint_auth_method': 'none', **({'scope': data.scope} if data.scope else {})}


@router.get(OAUTH_PREFIX + '/authorize')
def begin_authorization(request: Request):
    values = _auth_request(request.query_params)
    return RedirectResponse(_web_request_url(values), status_code=303, headers={'Cache-Control': 'no-store'})


@router.get(OAUTH_PREFIX + '/authorize/details')
def authorization_details(request: Request, user=Depends(current_user)):
    user = _require_session_user(user)
    values = _auth_request(request.query_params)
    return {'client_name': values['client_name'], 'scopes': values['scope'].split(), 'resource': values['resource'],
            'account': user['full_name'] or user['username']}


@router.post(OAUTH_PREFIX + '/authorize')
def approve_authorization(data: AuthorizationDecision, user=Depends(current_user)):
    user = _require_session_user(user)
    values = _auth_request(QueryParams({key: getattr(data, key) for key in AUTH_FIELDS if getattr(data, key, None) is not None}))
    code = 'phc_' + token()
    with transaction(user['tenant_id']) as conn:
        expiry = conn.execute('''SELECT CASE WHEN %s::integer IS NULL THEN NULL
            ELSE now()+(%s::integer * interval '1 day') END AS expires_at''',
            (data.expires_in_days, data.expires_in_days)).fetchone()['expires_at']
        conn.execute('''INSERT INTO oauth_authorization_codes(code_hash,client_id,tenant_id,user_id,redirect_uri,scopes,
            resource,code_challenge,code_challenge_method,expires_at,authorization_expires_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'S256',now()+interval '5 minutes',%s)''',
            (digest(code), values['client_id'], user['tenant_id'], user['id'], values['redirect_uri'],
             values['scope'].split(), values['resource'], values['code_challenge'], expiry))
        audit(conn, user, 'oauth.authorize', {'client_id': values['client_id'], 'scopes': values['scope'].split(),
                                               'expires_in_days': data.expires_in_days})
    return {'redirect_uri': _callback_uri(values['redirect_uri'], {'code': code, 'state': values['state']})}


@router.post(OAUTH_PREFIX + '/authorize/deny')
def deny_authorization(data: AuthorizationDecision):
    values = _auth_request(QueryParams({key: getattr(data, key) for key in AUTH_FIELDS if getattr(data, key, None) is not None}))
    return {'redirect_uri': _callback_uri(values['redirect_uri'], {'error': 'access_denied', 'state': values['state']})}


def _form_values(form):
    result = {}
    for key in ('grant_type', 'client_id', 'code', 'redirect_uri', 'code_verifier', 'refresh_token', 'resource', 'token', 'token_type_hint'):
        values = form.getlist(key)
        if len(values) > 1:
            raise ValueError(f'{key} must not be repeated')
        if values:
            result[key] = str(values[0])
    return result


def _issue_tokens(conn, grant_id, tenant_id, scopes, grant_expires_at):
    access = 'pha_' + token()
    refresh = 'phr_' + token()
    conn.execute('INSERT INTO oauth_access_tokens(token_hash,tenant_id,grant_id,expires_at) VALUES (%s,%s,%s,now()+interval \'1 hour\')',
                 (digest(access), tenant_id, grant_id))
    conn.execute('INSERT INTO oauth_refresh_tokens(token_hash,tenant_id,grant_id,expires_at) VALUES (%s,%s,%s,%s)',
                 (digest(refresh), tenant_id, grant_id, grant_expires_at))
    return {'access_token': access, 'token_type': 'Bearer', 'expires_in': ACCESS_TOKEN_SECONDS,
            'refresh_token': refresh, 'scope': ' '.join(scopes),
            'authorization_expires_at': grant_expires_at.isoformat() if grant_expires_at else None}


@router.post(OAUTH_PREFIX + '/token')
async def token_endpoint(request: Request):
    rate_limit('oauth-token:' + (request.client.host if request.client else 'unknown'), 120, 60)
    try:
        form = await request.form()
        data = _form_values(form)
    except Exception:
        return _oauth_error('invalid_request', '令牌请求格式无效')
    if not data.get('grant_type'):
        return _oauth_error('invalid_request', '缺少 grant_type')
    if not data.get('client_id') or data.get('resource') and data['resource'].rstrip('/') != _resource():
        return _oauth_error('invalid_request', 'client_id 或 resource 无效')
    if data['grant_type'] == 'authorization_code':
        if not all(data.get(k) for k in ('code', 'redirect_uri', 'code_verifier')):
            return _oauth_error('invalid_request', '授权码交换缺少必要参数')
        if not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', data['code_verifier']):
            return _oauth_error('invalid_request', 'PKCE verifier 格式无效')
        expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(data['code_verifier'].encode()).digest()).decode().rstrip('=')
        result = None
        with transaction(admin=True) as conn:
            conn.execute('DELETE FROM oauth_authorization_codes WHERE expires_at<now()-interval \'1 day\'')
            row = conn.execute('SELECT * FROM oauth_authorization_codes WHERE code_hash=%s AND expires_at>now() FOR UPDATE',
                               (digest(data['code']),)).fetchone()
            client = conn.execute('SELECT redirect_uris FROM oauth_clients WHERE client_id=%s', (data['client_id'],)).fetchone()
            if row and client and row['client_id'] == data['client_id'] and row['redirect_uri'] == data['redirect_uri'] \
                    and data['redirect_uri'] in client['redirect_uris'] \
                    and hmac.compare_digest(row['code_challenge'], expected_challenge):
                conn.execute('DELETE FROM oauth_authorization_codes WHERE code_hash=%s', (digest(data['code']),))
                grant = conn.execute('''INSERT INTO oauth_grants(tenant_id,user_id,client_id,scopes,expires_at)
                    VALUES (%s,%s,%s,%s,%s) RETURNING id''',
                    (row['tenant_id'], row['user_id'], row['client_id'], row['scopes'], row['authorization_expires_at'])).fetchone()
                result = _issue_tokens(conn, grant['id'], row['tenant_id'], row['scopes'], row['authorization_expires_at'])
        if result:
            return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
        return _oauth_error('invalid_grant', '授权码无效、过期、已使用或 PKCE 校验失败')
    if data['grant_type'] == 'refresh_token':
        if not data.get('refresh_token'):
            return _oauth_error('invalid_request', '缺少 refresh_token')
        result = None
        with transaction(admin=True) as conn:
            row = conn.execute('''SELECT * FROM oauth_refresh_tokens WHERE token_hash=%s
                AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE''',
                               (digest(data['refresh_token']),)).fetchone()
            if row:
                grant = conn.execute('''SELECT * FROM oauth_grants WHERE id=%s AND tenant_id=%s
                    AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE''',
                                     (row['grant_id'], row['tenant_id'])).fetchone()
                if row['consumed_at']:
                    if grant and grant['client_id'] == data['client_id']:
                        conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE id=%s', (grant['id'],))
                elif grant and grant['client_id'] == data['client_id']:
                    conn.execute('UPDATE oauth_refresh_tokens SET consumed_at=now() WHERE token_hash=%s', (row['token_hash'],))
                    conn.execute('''UPDATE oauth_grants SET last_used_at=now()
                        WHERE id=%s''', (grant['id'],))
                    result = _issue_tokens(conn, grant['id'], row['tenant_id'], grant['scopes'], grant['expires_at'])
        if result:
            return JSONResponse(result, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})
        return _oauth_error('invalid_grant', 'refresh_token 无效、过期、已撤销或已使用')
    return _oauth_error('unsupported_grant_type', '仅支持 authorization_code 和 refresh_token')


@router.post(OAUTH_PREFIX + '/revoke')
async def revoke_token(request: Request):
    try:
        form = await request.form()
        data = _form_values(form)
    except Exception:
        return _oauth_error('invalid_request', '撤销请求格式无效')
    if data.get('token') and data.get('client_id'):
        with transaction(admin=True) as conn:
            for table in ('oauth_access_tokens', 'oauth_refresh_tokens'):
                row = conn.execute(f'''SELECT grant_id,tenant_id FROM {table} WHERE token_hash=%s''',
                                   (digest(data['token']),)).fetchone()
                if row:
                    owner = conn.execute('SELECT client_id FROM oauth_grants WHERE id=%s AND tenant_id=%s',
                                         (row['grant_id'], row['tenant_id'])).fetchone()
                    if owner and owner['client_id'] == data['client_id']:
                        conn.execute('UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now()) WHERE id=%s',
                                     (row['grant_id'],))
    return JSONResponse({}, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache'})


@router.get(OAUTH_PREFIX + '/grants')
def list_grants(user=Depends(current_user)):
    _require_session_user(user)
    with transaction(user['tenant_id']) as conn:
        return conn.execute('''SELECT g.id,g.scopes,g.created_at,g.expires_at,g.revoked_at,g.last_used_at,c.client_name,
            CASE WHEN g.revoked_at IS NOT NULL THEN 'revoked'
                WHEN g.expires_at IS NOT NULL AND g.expires_at<=now() THEN 'expired' ELSE 'active' END AS status
            FROM oauth_grants g JOIN oauth_clients c ON c.client_id=g.client_id
            WHERE g.user_id=%s ORDER BY g.created_at DESC''', (user['id'],)).fetchall()


@router.delete(OAUTH_PREFIX + '/grants/{grant_id}')
def revoke_grant(grant_id: str, user=Depends(current_user)):
    _require_session_user(user)
    with transaction(user['tenant_id']) as conn:
        row = conn.execute('''UPDATE oauth_grants SET revoked_at=coalesce(revoked_at,now())
            WHERE id=%s AND user_id=%s RETURNING id,client_id,scopes''', (grant_id, user['id'])).fetchone()
        if not row:
            raise HTTPException(404, 'OAuth 授权不存在')
        audit(conn, user, 'oauth.revoke', {'grant_id': str(row['id']), 'client_id': row['client_id']})
    return {'ok': True}
