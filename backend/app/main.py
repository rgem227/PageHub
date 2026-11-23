from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from . import admin, api, auth, bookmarks, directory, files, shares
from .config import settings
from .db import pool
from .mcp_server import MCPAuthentication, mcp


@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    pool.open(wait=True)
    async with mcp.session_manager.run():
        yield
    pool.close()


app = FastAPI(title='PageHub', version='0.1.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.web_origin], allow_credentials=True,
                   allow_methods=['GET', 'POST', 'PATCH', 'DELETE'], allow_headers=['Content-Type', 'Authorization'])


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get('body', b''))
            if consumed > (settings.max_upload_mb * 4 // 3 + 2) * 1024**2:
                from fastapi import HTTPException
                raise HTTPException(413, '请求超过上传限制')
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(BodyLimit)


@app.middleware('http')
async def security_headers(request: Request, call_next):
    if request.method in ('POST', 'PATCH', 'DELETE', 'PUT'):
        origin = request.headers.get('origin')
        if origin and origin != settings.web_origin:
            return JSONResponse({'detail': '请求来源不允许'}, status_code=403)
        try:
            length = int(request.headers.get('content-length', '0'))
        except ValueError:
            return JSONResponse({'detail': '请求长度无效'}, status_code=400)
        if length > (settings.max_upload_mb * 4 // 3 + 2) * 1024**2:
            return JSONResponse({'detail': '请求超过上传限制'}, status_code=413)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(ValueError)
async def validation_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    labels = {'username': '用户名', 'account': '登录账号', 'phone': '手机号', 'code': '短信验证码',
              'password': '密码', 'old_password': '原密码', 'new_password': '新密码',
              'name': '名称', 'title': '页面标题', 'path': '菜单路径', 'position': '排序值',
              'parent_id': '上级菜单', 'menu_id': '所属菜单', 'tenant_id': '所属租户',
              'quota_mb': '存储配额', 'file': '上传文件', 'url': '网站地址', 'description': '备注'}
    if request.url.path.startswith('/api/credentials'):
        labels['name'] = '凭证名称'
    elif request.url.path.startswith('/api/bookmarks'):
        labels['title'] = '收藏名称'
    elif request.url.path.startswith('/api/menus'):
        labels['name'] = '菜单名称'
    elif request.url.path.startswith('/api/admin/tenants'):
        labels['name'] = '租户名称'
    errors = []
    for error in exc.errors():
        field = error['loc'][1] if len(error['loc']) > 1 else ''
        label = labels.get(field, '输入内容')
        context = error.get('ctx', {})
        kind = error['type']
        if kind == 'missing' or (kind == 'string_too_short' and context.get('min_length') == 1):
            message = f'{label}不能为空'
        elif kind == 'string_too_short':
            message = f'{label}至少需要 {context["min_length"]} 个字符'
        elif kind == 'string_too_long':
            message = f'{label}不能超过 {context["max_length"]} 个字符'
        elif kind == 'too_short':
            message = f'{label}至少需要 {context["min_length"]} 项'
        elif kind == 'too_long':
            message = f'{label}最多支持 {context["max_length"]} 项'
        elif kind == 'string_pattern_mismatch' and field == 'username':
            message = '用户名须以汉字、字母或下划线开头，仅包含汉字、字母、数字或下划线'
        elif kind == 'string_pattern_mismatch' and field == 'code':
            message = '短信验证码须为 6 位数字'
        else:
            message = f'{label}格式或取值不正确'
        errors.append({'loc': error['loc'], 'msg': message, 'type': kind})
    return JSONResponse({'detail': errors}, status_code=422)


@app.exception_handler(UniqueViolation)
async def duplicate_error(request, exc):
    return JSONResponse({'detail': '名称、用户名或手机号已存在'}, status_code=409)


@app.exception_handler(ForeignKeyViolation)
async def reference_error(request, exc):
    return JSONResponse({'detail': '关联记录不可用或仍有内容，请刷新重试'}, status_code=409)


@app.get('/api/health')
def health():
    with pool.connection() as conn:
        conn.execute('SELECT 1')
    return {'status': 'ok', 'environment': settings.environment}


app.include_router(auth.router)
app.include_router(api.router)
app.include_router(directory.router)
app.include_router(admin.router)
app.include_router(files.router)
app.include_router(shares.router)
app.include_router(bookmarks.router)
app.mount('/mcp', MCPAuthentication(mcp.streamable_http_app()))
