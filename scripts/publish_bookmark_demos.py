"""通过真实 MCP 发布不同账号的收藏示例，并验证跨租户读取拒绝。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.config import settings
from app.db import pool, transaction
from app.security import digest, token
from publish_mcp_demos import unpack

SAMPLES = {
    'admin': [
        ('FastAPI 开发文档', 'https://fastapi.tiangolo.com/', 'Python API 框架文档'),
        ('PostgreSQL 数据库文档', 'https://www.postgresql.org/docs/', '数据库、SQL 与运维参考'),
        ('Vue 前端开发文档', 'https://cn.vuejs.org/', 'Vue 中文指南和 API 参考'),
        ('Python 官方文档', 'https://docs.python.org/zh-cn/3/', 'Python 标准库和语言参考'),
    ],
    'test': [
        ('维基百科知识入口', 'https://www.wikipedia.org/', '多语言知识与百科检索'),
        ('OpenStreetMap 开放地图', 'https://www.openstreetmap.org/', '浏览开放的地理与地图数据'),
        ('中国天气网', 'https://www.weather.com.cn/', '天气预报与出行参考'),
        ('中国国家图书馆', 'https://www.nlc.cn/', '阅读资源、馆藏与服务信息'),
    ],
}


async def run():
    pool.open(wait=True)
    accounts, credentials = [], []
    try:
        with transaction(admin=True) as conn:
            for username in SAMPLES:
                account = conn.execute('''SELECT u.id,u.tenant_id FROM users u JOIN tenants t ON t.id=u.tenant_id
                    WHERE u.username=%s AND u.active AND t.active AND t.deleted_at IS NULL''', (username,)).fetchone()
                assert account, '示例账号不可用：' + username
                secret = 'hub_' + token()
                credentials.append(conn.execute('''INSERT INTO mcp_credentials(tenant_id,user_id,name,token_hash,expires_at)
                    VALUES (%s,%s,'网站收藏演示临时凭证',%s,now()+interval '10 minutes') RETURNING id''',
                    (account['tenant_id'], account['id'], digest(secret))).fetchone()['id'])
                accounts.append({'username': username, 'tenant_id': account['tenant_id'], 'secret': secret, 'bookmarks': []})
        assert accounts[0]['tenant_id'] != accounts[1]['tenant_id']
        for account in accounts:
            async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + account['secret']}, trust_env=False, timeout=30) as http:
                async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert {'create_bookmark', 'update_bookmark', 'get_bookmark', 'search_bookmarks', 'delete_bookmark'} <= {t.name for t in tools.tools}
                        for index, (title, url, description) in enumerate(SAMPLES[account['username']]):
                            existing = unpack(await session.call_tool('search_bookmarks', {'query': title}))
                            matches = [b for b in existing['items'] if b['title'] == title]
                            assert len(matches) <= 1
                            if matches:
                                item = unpack(await session.call_tool('get_bookmark', {'bookmark_id': matches[0]['id']}))
                            else:
                                item = unpack(await session.call_tool('create_bookmark', {'title': title, 'url': url, 'description': description,
                                    'category_name': '开发文档' if account['username'] == 'admin' else '日常资源', 'position': index}))
                            assert item['open_mode'] == 'external'
                            account['bookmarks'].append(item)
                        if account is accounts[1]:
                            for foreign in accounts[0]['bookmarks']:
                                assert (await session.call_tool('get_bookmark', {'bookmark_id': foreign['id']})).isError
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + accounts[0]['secret']}, trust_env=False, timeout=30) as http:
            async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for foreign in accounts[1]['bookmarks']:
                        assert (await session.call_tool('get_bookmark', {'bookmark_id': foreign['id']})).isError
        public = [{k: v for k, v in a.items() if k != 'secret'} for a in accounts]
        runtime = Path(__file__).resolve().parents[1] / '.runtime'
        runtime.mkdir(exist_ok=True)
        (runtime / 'bookmark-demos.json').write_text(json.dumps(public, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        print('MCP 收藏示例：admin/test 各 4 项；双向隔离验证通过。')
    finally:
        with transaction(admin=True) as conn:
            for credential in credentials:
                conn.execute('UPDATE mcp_credentials SET active=false WHERE id=%s', (credential,))
        pool.close()


if __name__ == '__main__':
    asyncio.run(run())
