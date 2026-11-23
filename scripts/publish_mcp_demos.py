"""通过真实 MCP SDK 发布 admin/test 的不同示例并验证隔离，输出不含临时凭证。"""
import asyncio
import base64
import io
import json
import sys
import time
import zipfile
from html import escape
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.config import settings
from app.db import pool, transaction
from app.security import digest, token


CSS = '''*{box-sizing:border-box}body{margin:0;background:#0b1423;color:#e9f1ff;font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1120px;margin:auto;padding:48px 28px}header{padding:32px 0;border-bottom:1px solid #29364d;margin-bottom:28px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:2px}h1{font-size:clamp(27px,4vw,46px);letter-spacing:-1px;line-height:1.3;margin:14px 0}h2{font-size:21px}h3{font-size:15px}p,small{color:#a8bad4}small{font-size:12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,210px),1fr));gap:16px}.card,section{padding:22px;background:#131f33;border:1px solid #29364d;border-radius:16px;margin-bottom:18px;min-width:0}.card strong{display:block;font-size:30px;line-height:1.4;color:var(--accent)}.grid>.card{margin:0}label{display:block;margin:14px 0;color:#b8cae3}input,select,button{font:inherit;border:1px solid #354b68;border-radius:9px;background:#0b1423;color:#e9f1ff;padding:10px 13px;max-width:100%}input[type=number],input[type=text],input[type=color],select{display:block;width:100%;margin-top:7px}button{cursor:pointer;background:var(--accent);color:#0a1420;font-weight:600;margin:8px 8px 0 0}.result{padding:20px;border-left:3px solid var(--accent);background:#0b1423;border-radius:8px;font-size:24px;color:var(--accent)}table{border-collapse:collapse;width:100%;min-width:460px}td,th{padding:14px;text-align:left;border-bottom:1px solid #29364d}th{color:var(--accent);font-size:12px}.table-wrap{overflow:auto}code,pre{background:#0b1423;padding:12px;border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}svg{max-width:100%;height:auto}.timeline article{padding:12px 0 12px 24px;border-left:2px solid var(--accent);position:relative}.timeline article:before{content:"";position:absolute;left:-6px;top:24px;width:10px;height:10px;border-radius:50%;background:var(--accent)}details{border-bottom:1px solid #29364d;padding:16px 0}summary{cursor:pointer;font-weight:600}footer{border-top:1px solid #29364d;margin-top:32px;padding-top:18px;font-size:12px;color:#7790b0}progress{width:100%;accent-color:var(--accent)}.checks label{display:flex;gap:12px;align-items:center}.checks input:checked+span{text-decoration:line-through;color:#6b829f}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:2px solid white;outline-offset:3px}@media(max-width:540px){main{padding:22px 16px}header{padding:20px 0}section,.card{padding:18px}}'''


SAMPLES = {
    'admin': [
        ('经营数据驾驶舱', '数据看板', '<div class="grid"><article class="card"><small>本月演示营收</small><strong>¥268,400</strong><p>环比 +18.6%</p></article><article class="card"><small>演示订单</small><strong>1,286</strong><p>转化率 8.4%</p></article><article class="card"><small>交付达成</small><strong>96.8%</strong><p>连续 6 周增长</p></article></div><section><h2>季度增长趋势</h2><svg viewBox="0 0 900 240" role="img" aria-label="演示营收逐月增长"><path d="M20 205 L175 175 L330 182 L485 108 L640 125 L870 32" fill="none" stroke="var(--accent)" stroke-width="5"/><path d="M20 205 L175 175 L330 182 L485 108 L640 125 L870 32 L870 230 L20 230Z" fill="var(--accent)" opacity=".12"/></svg></section>', ''),
        ('项目交付路线图', '项目计划', '<section class="timeline"><h2>从验证到交付</h2><article><h3>01 · 需求确认</h3><p>梳理业务目标、菜单结构与数据边界。状态：已完成。</p></article><article><h3>02 · 核心能力建设</h3><p>完成上传发布、版本管理和多租户隔离。状态：已完成。</p></article><article><h3>03 · 体验优化</h3><p>验证桌面与手机布局，完善空间管理。状态：进行中。</p></article><article><h3>04 · 生产验收</h3><p>计划开展持续混合负载、备份恢复与对象存储验收。</p></article></section>', ''),
        ('项目报价计算器', '交互工具', '<section><h2>估算项目费用</h2><div class="grid"><label>人天单价<input id="price" type="number" min="0" value="1800"></label><label>投入人天<input id="days" type="number" min="0" value="20"></label><label>折扣（%）<input id="discount" type="number" min="0" max="100" value="95"></label></div><p>仅用于演示估算，不构成实际报价。</p><output class="result" id="quote" aria-live="polite"></output></section>', 'function calculate(){const p=Math.max(0,Number(document.getElementById("price").value)||0),d=Math.max(0,Number(document.getElementById("days").value)||0),r=Math.min(100,Math.max(0,Number(document.getElementById("discount").value)||0));document.getElementById("quote").textContent="估算总价：¥"+(p*d*r/100).toLocaleString("zh-CN",{maximumFractionDigits:2})}document.querySelectorAll("input").forEach(el=>el.addEventListener("input",calculate));calculate();'),
        ('客户服务工单台账', '表格报表', '<section><h2>服务工单 · 虚构样例</h2><label>筛选工单<input id="search" type="text" placeholder="输入客户、事项或状态"></label><div class="table-wrap"><table><thead><tr><th>编号</th><th>客户</th><th>事项</th><th>状态</th></tr></thead><tbody><tr><td>SR-1001</td><td>星河团队</td><td>部署咨询</td><td>已完成</td></tr><tr><td>SR-1002</td><td>远山工作室</td><td>菜单迁移</td><td>进行中</td></tr><tr><td>SR-1003</td><td>青禾实验室</td><td>空间配额</td><td>待处理</td></tr><tr><td>SR-1004</td><td>云帆团队</td><td>版本回滚</td><td>已完成</td></tr></tbody></table></div><p id="count" aria-live="polite">共 4 条工单</p></section>', 'document.getElementById("search").addEventListener("input",e=>{let n=0;document.querySelectorAll("tbody tr").forEach(row=>{row.hidden=!row.textContent.includes(e.target.value);if(!row.hidden)n++});document.getElementById("count").textContent="匹配 "+n+" 条工单"});'),
        ('接口接入使用手册', '技术文档', '<section><h2>01 · 创建凭证</h2><p>在 PageHub 顶部 MCP 接入中创建凭证，并及时保存。此示例不包含真实密钥。</p><pre>{"headers":{"Authorization":"Bearer &lt;your-token&gt;"}}</pre></section><section><h2>02 · 上传页面</h2><p>智能体调用 upload_html，传入 HTML、文件名和菜单路径。</p><pre>{"name":"upload_html","arguments":{"filename":"report.htm","menu_path":["业务","报告"],"html":"&lt;html&gt;...&lt;/html&gt;"}}</pre></section><section><h2>03 · 查询结果</h2><p>轮询 get_upload_status。成功后返回稳定访问链接，访问者仍需登录对应租户。</p></section><section><h2>04 · 页面较多时</h2><p>使用 search_pages 按名称搜索并分页，减少智能体上下文占用。</p></section>', ''),
        ('运营周报与复盘', '长页汇报', '<section><h2>本周摘要</h2><p>演示团队围绕发布体验、文件清理和菜单可用性进行了三轮迭代。所有数值均为虚构样例。</p><div class="grid"><article class="card"><small>已完成任务</small><strong>24</strong></article><article class="card"><small>需求响应</small><strong>98%</strong></article></div></section><section><h2>关键成果</h2><p>统一工作空间入口；优化自适应页面卡片；验证不同租户独立发布内容。</p><h3>发布链路</h3><progress value="90" max="100">90%</progress><h3>体验验证</h3><progress value="78" max="100">78%</progress></section><section><h2>问题与行动</h2><p>生产环境还需验证短信、对象存储权限及持续混合并发。负责人和时间以实际项目计划为准。</p></section><section><h2>下周重点</h2><p>补齐验收用例、复核移动端交互、整理业务演示材料，并按真实使用量规划容量。</p></section>', ''),
    ],
    'test': [
        ('周末旅行行程单', '行程规划', '<section class="timeline"><h2>山海之间 · 两日计划</h2><article><h3>周六 09:00 · 城市出发</h3><p>轻装出行，准备饮水、雨具和离线地图。</p></article><article><h3>周六 14:00 · 山间步道</h3><p>沿溪漫步，预留摄影和休息时间。</p></article><article><h3>周日 08:30 · 海边日出</h3><p>散步、早餐与自由活动。</p></article><article><h3>周日 16:00 · 返程</h3><p>行程仅为示例，请按天气和交通安排调整。</p></article></section>', ''),
        ('每日习惯打卡板', '清单交互', '<section class="checks"><h2>让小事慢慢发生</h2><label><input type="checkbox"><span>阅读 20 分钟</span></label><label><input type="checkbox"><span>散步 30 分钟</span></label><label><input type="checkbox"><span>整理桌面</span></label><label><input type="checkbox"><span>记录今天的收获</span></label><output id="progress" class="result" aria-live="polite">今日完成 0 / 4</output><p>状态仅保留在本次页面内存中，刷新后重新开始。</p></section>', 'document.querySelectorAll("input").forEach(el=>el.addEventListener("change",()=>{document.getElementById("progress").textContent="今日完成 "+document.querySelectorAll("input:checked").length+" / 4"}));'),
        ('旅行预算分摊器', '计算工具', '<section><h2>轻松算清旅行预算</h2><div class="grid"><label>交通费用<input id="transport" type="number" min="0" value="800"></label><label>住宿费用<input id="hotel" type="number" min="0" value="1200"></label><label>餐饮费用<input id="food" type="number" min="0" value="600"></label><label>出行人数<input id="people" type="number" min="1" value="4"></label></div><output id="budget" class="result" aria-live="polite"></output><p>示例预算，不包含真实订单或支付。</p></section>', 'function calc(){const total=["transport","hotel","food"].reduce((n,id)=>n+Math.max(0,Number(document.getElementById(id).value)||0),0);const people=Math.max(1,Math.floor(Number(document.getElementById("people").value)||1));document.getElementById("budget").textContent="合计 ¥"+total.toFixed(2)+" · 人均 ¥"+(total/people).toFixed(2)}document.querySelectorAll("input").forEach(el=>el.addEventListener("input",calc));calc();'),
        ('灵感配色实验室', '视觉交互', '<section><h2>挑一个今天的颜色</h2><div class="grid"><label>主色<input id="color" type="color" value="#a78bfa"></label><article class="card" id="preview"><small>实时预览</small><strong id="hex">#A78BFA</strong><p>色彩让日常更有趣。</p></article></div><p>点击颜色选择器，右侧预览会同步变化。</p></section>', 'document.getElementById("color").addEventListener("input",e=>{document.documentElement.style.setProperty("--accent",e.target.value);document.getElementById("hex").textContent=e.target.value.toUpperCase();document.getElementById("preview").style.borderColor=e.target.value});'),
        ('个人阅读书架', '阅读目录', '<section><h2>下一本想读的书</h2><label>按主题筛选<select id="genre"><option value="all">全部主题</option><option value="design">设计</option><option value="life">生活</option><option value="science">科学</option></select></label><div class="grid" id="books"><article class="card" data-genre="design"><h3>日常设计观察</h3><p>从一个按钮到一条街道，重新发现秩序。</p><small>虚构书目 · 设计</small></article><article class="card" data-genre="life"><h3>慢下来的一年</h3><p>为阅读、步行与朋友留一点时间。</p><small>虚构书目 · 生活</small></article><article class="card" data-genre="science"><h3>星空的来信</h3><p>从身边的问题开始认识宇宙。</p><small>虚构书目 · 科学</small></article></div></section>', 'document.getElementById("genre").addEventListener("change",e=>document.querySelectorAll("#books article").forEach(card=>card.hidden=e.target.value!=="all"&&card.dataset.genre!==e.target.value));'),
        ('厨房小百科', '折叠知识页', '<section><h2>做饭前的小问题</h2><details open><summary>如何安排一顿简单晚餐？</summary><p>选择一份主食、一份蛋白质和两种蔬菜；提前准备食材，让做饭更从容。</p></details><details><summary>剩余食材怎样利用？</summary><p>先检查保存状态，按食材特点尽快使用；不确定是否安全时不要食用。</p></details><details><summary>怎样减少厨房忙乱？</summary><p>先阅读步骤，再清洗、切配，按烹饪时间长短依次开火。</p></details><details><summary>这是真实菜谱吗？</summary><p>这是页面交互演示，内容仅作日常整理示例。</p></details></section>', ''),
    ],
}


def build_samples():
    samples = {}
    for username, definitions in SAMPLES.items():
        directory = ROOT / 'examples' / 'isolation' / username
        directory.mkdir(parents=True, exist_ok=True)
        samples[username] = []
        for index, (title, kind, body, script) in enumerate(definitions, 1):
            full_title = ('管理演示 · ' if username == 'admin' else '个人演示 · ') + title
            accent = '#64d8f4' if username == 'admin' else '#bda2ff'
            marker = f'PAGEHUB-DEMO-{username.upper()}-{index:02d}'
            html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(full_title)}</title><style>:root{{--accent:{accent}}}{CSS}</style></head><body><main><header><span class="eyebrow">PAGEHUB / {username.upper()} / {escape(kind)}</span><h1>{escape(title)}</h1><p>{"企业经营与团队协作" if username == "admin" else "个人生活与创意工具"} · 此页面属于 {username} 所在租户</p></header>{body}<footer>{marker} · 演示内容与虚构数据 · 与真实业务统计无关</footer></main><script>{script}</script></body></html>'''
            path = directory / f'{index:02d}-{title}.htm'
            path.write_text(html, encoding='utf-8')
            samples[username].append({'title': full_title, 'kind': kind, 'source': path, 'marker': marker, 'menu_path': ['MCP 隔离演示', kind, title], 'zip': index == 6})
    return samples


def unpack(result):
    if result.isError:
        raise AssertionError('MCP tool failed: ' + ' '.join(block.text for block in result.content if hasattr(block, 'text')))
    if result.structuredContent is not None:
        value = result.structuredContent
        return value['result'] if isinstance(value, dict) and set(value) == {'result'} else value
    chunks = [json.loads(block.text) for block in result.content if hasattr(block, 'text')]
    return chunks[0] if len(chunks) == 1 else chunks


async def run():
    samples = build_samples()
    credentials = []
    accounts = []
    timings = []
    pool.open(wait=True)
    try:
        with transaction(admin=True) as conn:
            for username in samples:
                account = conn.execute('''SELECT u.id,u.username,u.tenant_id,t.active,t.deleted_at FROM users u
                    JOIN tenants t ON t.id=u.tenant_id WHERE u.username=%s AND u.active''', (username,)).fetchone()
                assert account and account['active'] and not account['deleted_at'], 'Account unavailable: ' + username
                secret = 'hub_' + token()
                credential = conn.execute('''INSERT INTO mcp_credentials(tenant_id,user_id,name,token_hash,expires_at)
                    VALUES (%s,%s,'MCP 隔离演示临时验证',%s,now()+interval '20 minutes') RETURNING id''',
                    (account['tenant_id'], account['id'], digest(secret))).fetchone()['id']
                credentials.append(credential)
                accounts.append({**account, 'secret': secret, 'pages': []})
        assert accounts[0]['tenant_id'] != accounts[1]['tenant_id'], 'admin/test need different tenants'
        for account in accounts:
            headers = {'Authorization': 'Bearer ' + account['secret']}
            async with httpx.AsyncClient(headers=headers, timeout=60, trust_env=False) as http:
                async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        assert init.serverInfo.name == 'PageHub'
                        tools = await session.list_tools()
                        assert {'upload_html', 'upload_zip', 'search_pages', 'get_upload_status'} <= {tool.name for tool in tools.tools}

                        async def call(name, arguments):
                            start = time.perf_counter()
                            raw = await session.call_tool(name, arguments)
                            timings.append({'account': account['username'], 'tool': name, 'ms': round((time.perf_counter() - start) * 1000, 2), 'response_bytes': len(raw.model_dump_json().encode())})
                            return unpack(raw)

                        existing = await call('search_pages', {'query': '演示 · ', 'page_size': 100})
                        for sample in samples[account['username']]:
                            matches = [page for page in existing['items'] if page['title'] == sample['title']]
                            assert len(matches) <= 1, 'Duplicate demo title'
                            if matches:
                                result = {'page_id': matches[0]['id'], 'version_id': matches[0]['current_version'], 'url': settings.web_origin + '/?page=' + matches[0]['id']}
                            else:
                                args = {'title': sample['title'], 'menu_path': sample['menu_path']}
                                if sample['zip']:
                                    raw_zip = io.BytesIO()
                                    with zipfile.ZipFile(raw_zip, 'w', zipfile.ZIP_DEFLATED) as archive:
                                        archive.writestr('site/index.htm', sample['source'].read_bytes())
                                        archive.writestr('manifest.json', json.dumps({'entry': 'site/index.htm', 'title': sample['title'], 'menu': sample['menu_path']}, ensure_ascii=False))
                                        archive.writestr('README.txt', 'Offline PageHub demo; generated sample data only.')
                                    args['zip_base64'] = base64.b64encode(raw_zip.getvalue()).decode()
                                    job = await call('upload_zip', args)
                                else:
                                    args.update({'html': sample['source'].read_text(encoding='utf-8'), 'filename': sample['source'].name})
                                    job = await call('upload_html', args)
                                for _ in range(120):
                                    status = await call('get_upload_status', {'upload_id': job['id']})
                                    if status['status'] not in ('queued', 'processing'):
                                        break
                                    await asyncio.sleep(.5)
                                assert status['status'] == 'success', status
                                result = status['result']
                            url = await call('get_page_url', {'page_id': result['page_id']})
                            assert url['url'] == result['url']
                            preview = await http.get('http://127.0.0.1:8000/api/pages/' + result['page_id'] + '/preview')
                            preview.raise_for_status()
                            # 内容服务不接受平台凭证；只使用短期授权链接。
                            async with httpx.AsyncClient(trust_env=False, timeout=30) as content_client:
                                content = await content_client.get(preview.json()['url'])
                            assert content.status_code == 200 and sample['marker'] in content.text
                            account['pages'].append({'title': sample['title'], 'kind': sample['kind'], 'marker': sample['marker'], 'menu_path': sample['menu_path'], 'source': str(sample['source'].relative_to(ROOT)), 'transport': 'upload_zip' if sample['zip'] else 'upload_html', **result})
                            print(account['username'] + ' / ' + sample['title'] + ' / published', flush=True)
        checks = []
        for index, account in enumerate(accounts):
            foreign_pages = accounts[1 - index]['pages']
            headers = {'Authorization': 'Bearer ' + account['secret']}
            async with httpx.AsyncClient(headers=headers, timeout=60, trust_env=False) as http:
                async with streamable_http_client(settings.mcp_url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = unpack(await session.call_tool('search_pages', {'query': '演示 · ', 'page_size': 100}))
                        ids = {page['id'] for page in listed['items']}
                        assert {p['page_id'] for p in account['pages']} <= ids
                        assert not ids & {p['page_id'] for p in foreign_pages}
                        for foreign in foreign_pages:
                            denied = await session.call_tool('get_page_url', {'page_id': foreign['page_id']})
                            assert denied.isError
                            denied_status = await session.call_tool('get_upload_status', {'upload_id': foreign['version_id']})
                            assert denied_status.isError
                            for suffix in ('', '/preview'):
                                response = await http.get('http://127.0.0.1:8000/api/pages/' + foreign['page_id'] + suffix)
                                assert response.status_code == 404
                        checks.append({'account': account['username'], 'own_pages_visible': len(account['pages']), 'foreign_pages_hidden': len(foreign_pages), 'foreign_mcp_page_and_job_denied': True, 'foreign_api_page_and_preview_denied': True})
        public = {'endpoint': settings.mcp_url, 'accounts': [{'username': account['username'], 'tenant_id': str(account['tenant_id']), 'pages': account['pages']} for account in accounts], 'isolation': checks, 'calls': timings}
        runtime = ROOT / '.runtime'
        runtime.mkdir(exist_ok=True)
        (runtime / 'mcp-isolation-results.json').write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding='utf-8')
        values = sorted(item['ms'] for item in timings)
        print(json.dumps({'published': sum(len(a['pages']) for a in accounts), 'mcp_calls': len(values), 'median_ms': values[len(values)//2], 'max_ms': max(values), 'isolation_passed': True}, ensure_ascii=False), flush=True)
    finally:
        with transaction(admin=True) as conn:
            for credential in credentials:
                conn.execute('UPDATE mcp_credentials SET active=false WHERE id=%s', (credential,))
        pool.close()


if __name__ == '__main__':
    asyncio.run(run())
