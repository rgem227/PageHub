import base64
import binascii
from contextvars import ContextVar
from functools import partial, wraps
from uuid import UUID
from urllib.parse import urlparse

import anyio
from fastapi import HTTPException, Request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from . import api, bookmarks, directory
from .auth import current_user
from .config import settings
from .db import audit, transaction
from .services import enqueue, ensure_menus

identity: ContextVar[dict] = ContextVar('mcp_identity')
mcp = FastMCP('PageHub', stateless_http=True, json_response=True, streamable_http_path='/',
              transport_security=TransportSecuritySettings(
                  allowed_hosts=['127.0.0.1', '127.0.0.1:*', 'localhost', 'localhost:*', urlparse(settings.web_origin).netloc,
                                 urlparse(settings.mcp_url).netloc],
                  allowed_origins=[settings.web_origin]))


def threaded_tool(fn):
    # MCP SDK 直接执行同步工具；将数据库和文件 I/O 移出事件循环。
    @wraps(fn)
    async def run(**kwargs):
        return await anyio.to_thread.run_sync(partial(fn, **kwargs))
    return mcp.tool()(run)


def user(write=False):
    account = identity.get()
    if write and 'write' not in account.get('scopes', []):
        raise ValueError('当前 MCP 凭证没有写入权限')
    if not write and 'read' not in account.get('scopes', []):
        raise ValueError('当前 MCP 凭证没有读取权限')
    return account


@threaded_tool
def list_menus() -> list:
    """列出当前租户菜单、depth、子菜单及页面占用状态。can_attach_page=true 表示可直接挂页面；has_children=true 且 can_create_child=true 表示上传时可按页面标题自动创建下一级菜单。"""
    return api.menus(user())


@threaded_tool
def create_menu(menu_path: list[str]) -> dict:
    """按最多三级路径创建菜单；已存在的层级复用。"""
    account = user(True)
    with transaction(account['tenant_id']) as conn:
        conn.execute('SELECT id FROM tenants WHERE id=%s FOR UPDATE', (account['tenant_id'],))
        menu_id = ensure_menus(conn, account['tenant_id'], menu_path)
        audit(conn, account, 'mcp.menu.create', {'path': menu_path})
    return {'id': str(menu_id)}


@threaded_tool
def list_pages() -> list:
    """列出当前租户发布的页面。"""
    return api.pages(user())


@threaded_tool
def search_pages(query: str = '', page: int = 1, page_size: int = 50) -> dict:
    """按名称搜索当前租户页面，返回 items/total/page/size。页面较多时优先使用此分页工具，单页最多 100 项。"""
    if len(query) > 200 or page < 1 or not 1 <= page_size <= 100:
        raise ValueError('搜索词最多 200 字符，页码至少 1，单页数量需为 1–100')
    return directory.page_list(q=query, menu=None, page=page, size=page_size, user=user())


@threaded_tool
def upload_html(html: str, title: str = '', menu_path: list[str] | None = None, page_id: str = '', filename: str = 'index.html') -> dict:
    """上传 HTML/HTM，返回异步任务 ID；用 get_upload_status 轮询发布结果。上传前先用 list_menus 检查路径。若 menu_path 以无页面、但已有子菜单且 can_create_child=true 的父节点结束，系统会按实际页面标题自动创建下一级菜单并挂接，成功结果返回完整 menu_path 与 menu_path_auto_extended；不超过三级。空闲末级节点直接挂接。已挂页面的末级菜单不能重复挂接；请询问用户新菜单名称，或提示其到后台菜单管理创建空闲末级菜单。page_id 更新已有页面；缺少的路径会自动创建。filename 可保留原始文件名。"""
    account = user(True)
    if not filename.lower().endswith(('.html', '.htm')) or len(filename) > 200:
        raise ValueError('HTML 文件名需以 .html 或 .htm 结尾且不超过 200 字符')
    return enqueue(account, filename, html.encode('utf-8'), {
        'title': title, 'menu_path': menu_path or [], 'page_id': page_id, 'auto_create_child': True,
    })


@threaded_tool
def upload_zip(zip_base64: str, title: str = '', menu_path: list[str] | None = None,
               entry_file: str = '', page_id: str = '') -> dict:
    """上传 base64 编码的 ZIP，支持 manifest.json。返回异步任务 ID。上传前先用 list_menus 检查路径。若 menu_path 以无页面、但已有子菜单且 can_create_child=true 的父节点结束，系统会按实际页面标题自动创建下一级菜单并挂接，成功结果返回完整 menu_path 与 menu_path_auto_extended；不超过三级。空闲末级节点直接挂接。已挂页面的末级菜单不能重复挂接；请询问用户新菜单名称，或提示其到后台菜单管理创建空闲末级菜单。缺少的路径会自动创建。"""
    account = user(True)
    if len(zip_base64) > (settings.max_upload_mb * 1024**2 + 2) * 4 // 3 + 4:
        raise ValueError('ZIP 超出上传上限')
    try:
        content = base64.b64decode(zip_base64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('ZIP 必须使用有效的 base64 编码')
    return enqueue(account, 'page.zip', content, {
        'title': title, 'menu_path': menu_path or [], 'entry_file': entry_file, 'page_id': page_id,
        'auto_create_child': True,
    })


@threaded_tool
def get_upload_status(upload_id: str) -> dict:
    """查询上传解析状态；成功后包含页面 URL、最终菜单路径和 menu_path_auto_extended 标记。失败时必须阅读 error 并如实告知用户。"""
    return api.upload_status(UUID(upload_id), user())


@threaded_tool
def get_page_url(page_id: str) -> dict:
    """返回页面的稳定平台访问 URL，访问者必须登录到所属租户。"""
    return api.get_page(UUID(page_id), user())


@threaded_tool
def search_bookmarks(query: str = '', category_id: str = '', page: int = 1, page_size: int = 40) -> dict:
    """搜索当前租户的外链收藏名称、网址、备注，可按平级收藏分类筛选并分页，单页最多 100 项。"""
    if len(query) > 200 or page < 1 or not 1 <= page_size <= 100:
        raise ValueError('搜索词最多 200 字符，页码至少 1，单页数量需为 1–100')
    return bookmarks.list_bookmarks(q=query, category_id=UUID(category_id) if category_id else None, page=page, size=page_size, sort='position', user=user())


@threaded_tool
def create_bookmark(title: str, url: str, category_name: str = '未分类', menu_path: list[str] | None = None, description: str = '',
                    open_mode: str = 'external', position: int = 0, allow_duplicate: bool = False) -> dict:
    """收藏 HTTP/HTTPS 外链，按 category_name 自动创建平级分类。旧 menu_path 调用仅取最后一段作为分类名，不创建租户菜单。默认新标签页打开；同分类重复网址需显式确认。"""
    account = user(True)
    data = bookmarks.BookmarkData(title=title, url=url, category_name=category_name if menu_path is None else None,
                                  menu_path=menu_path, description=description,
                                  open_mode=open_mode, position=position, allow_duplicate=allow_duplicate)
    return bookmarks.create_bookmark(data, account)


@threaded_tool
def get_bookmark(bookmark_id: str) -> dict:
    """读取当前租户收藏及稳定平台访问地址，外部网站可能禁止框架内显示。"""
    return bookmarks.get_bookmark(UUID(bookmark_id), user())


@threaded_tool
def update_bookmark(bookmark_id: str, title: str | None = None, url: str | None = None,
                    category_name: str | None = None, menu_path: list[str] | None = None, description: str | None = None,
                    open_mode: str | None = None, position: int | None = None, allow_duplicate: bool = False) -> dict:
    """维护当前租户外链收藏；category_name 可移动到平级分类。旧 menu_path 只使用末级名称。"""
    account = user(True)
    current = bookmarks.get_bookmark(UUID(bookmark_id), account)
    values = {key: value for key, value in dict(title=title, url=url, description=description, open_mode=open_mode, position=position).items() if value is not None}
    values['category_id'] = current['category_id']
    if menu_path is not None:
        values.update(category_id=None, category_name=None, menu_path=menu_path)
    elif category_name is not None:
        values.update(category_id=None, category_name=category_name)
    fields = ('title', 'url', 'description', 'category_id', 'open_mode', 'position')
    data = bookmarks.BookmarkData(**{**{key: current[key] for key in fields}, **values, 'allow_duplicate': allow_duplicate})
    return bookmarks.update_bookmark(UUID(bookmark_id), data, account)


@threaded_tool
def delete_bookmark(bookmark_id: str) -> dict:
    """删除当前租户收藏链接，目标网站不受影响，操作记录审计。"""
    return bookmarks.delete_bookmarks(bookmarks.CategoryBatch(ids=[UUID(bookmark_id)]), user(True))


class MCPAuthentication:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            if not request.headers.get('authorization', '').startswith('Bearer '):
                raise HTTPException(401, '请在平台创建 MCP 凭证，并通过 Authorization: Bearer 提供')
            account = await anyio.to_thread.run_sync(current_user, request)
        except HTTPException as exc:
            response = JSONResponse({'detail': exc.detail, 'register_url': settings.web_origin + '/?register=1'}, status_code=exc.status_code)
            await response(scope, receive, send)
            return
        context_token = identity.set(account)
        try:
            await self.app(scope, receive, send)
        finally:
            identity.reset(context_token)
