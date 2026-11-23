// Refresh the user guide screenshots against the local development service only.
import { chromium, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { resolve } from 'node:path'

const baseURL = 'http://127.0.0.1:5173'
const assets = resolve('frontend/public/help/assets')
mkdirSync(assets, { recursive: true })
const python = resolve('.venv/Scripts/python.exe')
const dbCode = `
import sys,json
sys.path.insert(0,'backend')
from app.db import pool,transaction
from app.config import settings
from app.security import token,digest
assert settings.environment=='development', 'Screenshots must use development data'
data=json.load(sys.stdin)
pool.open(wait=True)
try:
 with transaction(admin=True) as c:
  u=c.execute("SELECT u.id,u.tenant_id,u.theme,u.directory_view,u.external_preview_minutes,u.must_change_password,t.layout,t.open_mode FROM users u JOIN tenants t ON t.id=u.tenant_id WHERE u.username='test' AND u.active AND t.active").fetchone()
  assert u, 'Enabled test account is required'
  if data['action']=='start':
   secret=token()
   c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES (%s,%s,now()+interval '30 minutes')",(digest(secret),u['id']))
   c.execute('UPDATE users SET must_change_password=false WHERE id=%s',(u['id'],))
   print(json.dumps({'session':secret,'cookie':settings.session_cookie_name,'state':u},default=str))
  elif data['action']=='layout':
   c.execute('UPDATE tenants SET layout=%s,open_mode=%s WHERE id=%s',(data['layout'],data.get('open_mode','embedded'),u['tenant_id']))
   c.execute('UPDATE users SET theme=%s,directory_view=%s WHERE id=%s',(data.get('theme','light'),data.get('directory_view','compact'),u['id']))
  elif data['action']=='restore':
   s=data['state']
   c.execute('UPDATE tenants SET layout=%s,open_mode=%s WHERE id=%s',(s['layout'],s['open_mode'],s['tenant_id']))
   c.execute('UPDATE users SET theme=%s,directory_view=%s,external_preview_minutes=%s,must_change_password=%s WHERE id=%s',(s['theme'],s['directory_view'],s['external_preview_minutes'],s['must_change_password'],s['id']))
   c.execute('DELETE FROM sessions WHERE token_hash=%s',(digest(data['session']),))
finally:
 pool.close()
`
function db(data) {
  return execFileSync(python, ['-c', dbCode], { input: JSON.stringify(data), encoding: 'utf8', windowsHide: true }).trim()
}
const browser = await chromium.launch({ channel: 'msedge', headless: true })
let auth
const captures = []
try {
  const anonymous = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } })
  const login = await anonymous.newPage()
  await login.goto('/')
  await expect(login.getByRole('button', { name: '登录工作空间' })).toBeVisible()
  async function snap(page, name, locator) {
    await page.evaluate(() => document.fonts.ready)
    await page.locator('.el-loading-mask:visible').waitFor({ state: 'hidden', timeout: 20000 }).catch(() => {})
    await (locator || page).screenshot({ path: resolve(assets, name + '.png'), animations: 'disabled' })
    captures.push(name)
    console.log('Captured ' + name)
  }
  await snap(login, 'login')
  await login.getByRole('tab', { name: '注册', exact: true }).click()
  await expect(login.getByPlaceholder('请输入姓名')).toBeVisible()
  await snap(login, 'register')
  await anonymous.close()
  auth = JSON.parse(db({ action: 'start' }))
  writeFileSync('.runtime/help-capture-state.json', JSON.stringify(auth.state, null, 2))
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } })
  await context.addCookies([{ name: auth.cookie, value: auth.session, url: baseURL }])
  const page = await context.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(e.message))
  await page.goto('/')
  await expect(page.getByRole('link', { name: '使用帮助' })).toHaveAttribute('href', '/help/index.html')
  const [helpPage] = await Promise.all([
    context.waitForEvent('page'),
    page.getByRole('link', { name: '使用帮助' }).click(),
  ])
  await expect(helpPage).toHaveURL(/\/help\/index\.html$/)
  await expect(helpPage.getByRole('heading', { name: '普通用户使用帮助' })).toBeVisible()
  const imageStatus = await helpPage.locator('.shot img').evaluateAll(images => images.map(image => image.complete && image.naturalWidth > 0))
  if (imageStatus.some(ok => !ok)) throw new Error('One or more help screenshots failed to load')
  await helpPage.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => helpPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await helpPage.getByRole('button', { name: '展开功能目录' }).click()
  await expect(helpPage.locator('#sidebar')).toHaveClass(/open/)
  await helpPage.locator('a[href="#start"]').click()
  await expect(helpPage).toHaveURL(/#start$/)
  await expect(helpPage.locator('#sidebar')).not.toHaveClass(/open/)
  await helpPage.close()
  async function view(section = 'overview') {
    await page.goto('/?view=' + section)
    await expect(page.getByRole('button', { name: '账号菜单', exact: true })).toBeVisible()
    await expect(page.locator('.el-skeleton')).toHaveCount(0, { timeout: 20000 })
    await page.locator('.el-loading-mask:visible').waitFor({ state: 'hidden', timeout: 20000 }).catch(() => {})
  }
  async function layout(value, extra = {}) { db({ action: 'layout', layout: value, ...extra }); await view() }
  async function dialogShot(name) { await snap(page, name, page.getByRole('dialog')) }
  async function closeDialog() {
    const close = page.locator('.el-dialog__headerbtn:visible, .el-drawer__close-btn:visible').last()
    await close.click()
    await expect(page.getByRole('dialog')).toHaveCount(0)
    await expect(page.locator('.el-drawer__body:visible')).toHaveCount(0)
  }
  await layout('side')
  await expect(page.locator('.page-card').first()).toBeVisible()
  await snap(page, 'workspace-side')
  await page.getByRole('button', { name: '工作台设置', exact: true }).click()
  await snap(page, 'workbench')
  await page.getByRole('button', { name: '工作台设置', exact: true }).click()
  await page.getByRole('button', { name: '上传页面', exact: true }).click()
  await page.getByRole('dialog').locator('.el-input__inner').nth(0).fill('项目周报')
  await page.getByPlaceholder('业务工具 / 报表 / 月报').fill('工作文档 / 周报 / 项目周报')
  await dialogShot('upload')
  await closeDialog()
  await view('pages')
  await expect(page.locator('.page-card').first()).toBeVisible()
  await snap(page, 'pages')
  async function cardAction(name) {
    await page.locator('.page-card').first().getByRole('button', { name: '页面操作' }).click()
    await page.getByRole('menuitem', { name, exact: true }).click()
  }
  await cardAction('修改信息')
  await dialogShot('page-info')
  await closeDialog()
  await cardAction('版本记录')
  await dialogShot('versions')
  await closeDialog()
  await page.locator('.page-card').first().getByRole('button', { name: '分享', exact: true }).click()
  await expect(page.getByRole('dialog').getByText('公开链接', { exact: true })).toBeVisible()
  await dialogShot('share-public')
  await page.getByRole('dialog').getByText('指定账号', { exact: true }).click()
  await dialogShot('share-account')
  await closeDialog()
  const pageId = (await (await page.request.get('/api/page-list?page=1&size=1')).json()).items[0].id
  const preview = await (await page.request.get('/api/pages/' + pageId + '/preview')).json()
  await page.goto(preview.url)
  await page.waitForLoadState('domcontentloaded')
  await snap(page, 'preview')
  await view('menus')
  await expect(page.locator('.menu-drop-target').first()).toBeVisible()
  await snap(page, 'menus')
  await page.getByRole('button', { name: '新增菜单', exact: true }).click()
  await page.getByRole('dialog').getByText('二级菜单', { exact: true }).click()
  await page.getByPlaceholder('输入新菜单名称').fill('项目资料')
  await dialogShot('menu-create')
  await closeDialog()
  await page.getByRole('button', { name: '编辑', exact: true }).first().click()
  await dialogShot('menu-edit')
  await closeDialog()
  await view('uploads')
  await expect(page.locator('.el-table__body tbody tr').first()).toBeVisible()
  await snap(page, 'uploads')
  await view('files')
  await expect(page.getByRole('button', { name: '文件清单', exact: true }).first()).toBeVisible()
  await snap(page, 'files')
  await page.getByRole('button', { name: '文件清单', exact: true }).first().click()
  await expect(page.getByRole('dialog').locator('.el-table__body tbody tr').first()).toBeVisible()
  await dialogShot('file-entries')
  await closeDialog()
  await page.getByText('清理任务', { exact: true }).click()
  await snap(page, 'cleanup')
  await view('bookmarks')
  await expect(page.locator('.bookmark-card').first()).toBeVisible()
  await snap(page, 'bookmarks')
  await page.getByRole('button', { name: '新增收藏', exact: true }).click()
  await page.getByPlaceholder('例如：团队知识库').fill('团队知识库')
  await page.getByPlaceholder('https://example.com').fill('https://example.com')
  await dialogShot('bookmark-form')
  await closeDialog()
  await page.getByRole('button', { name: '管理分类', exact: true }).click()
  await dialogShot('bookmark-categories')
  await closeDialog()
  await page.getByRole('button', { name: '收藏管理列表' }).click()
  await snap(page, 'bookmarks-list')
  await view('shares')
  await snap(page, 'my-shares')
  await view('received-shares')
  await snap(page, 'received-shares')
  await view('settings')
  await page.locator('.setting-row').last().locator('p').evaluate(el => { el.textContent = '已绑定手机号（已脱敏）' })
  await snap(page, 'settings')
  await layout('side', { open_mode: 'external' })
  await view('settings')
  await page.locator('.setting-row').last().locator('p').evaluate(el => { el.textContent = '已绑定手机号（已脱敏）' })
  await snap(page, 'settings-external')
  await view('mcp')
  await expect(page.getByRole('button', { name: '创建凭证', exact: true })).toBeVisible()
  await snap(page, 'mcp')
  await page.getByRole('button', { name: '账号菜单', exact: true }).click()
  await snap(page, 'account-menu')
  await page.getByText('修改密码', { exact: true }).click()
  await dialogShot('password')
  await closeDialog()
  await layout('top')
  await expect(page.locator('.page-card').first()).toBeVisible()
  await snap(page, 'workspace-top')
  await layout('flat')
  await expect(page.locator('.directory-entry').first()).toBeVisible()
  await snap(page, 'workspace-flat')
  await layout('flat', { directory_view: 'list' })
  await snap(page, 'workspace-list')
  await layout('flat', { theme: 'dark' })
  await expect(page.locator('html')).toHaveClass('dark')
  await snap(page, 'workspace-dark')
  expect(errors).toEqual([])
  writeFileSync(resolve(assets, 'capture-manifest.json'), JSON.stringify({ capturedAt: new Date().toISOString(), account: 'test', viewport: '1440 × 1000', images: captures }, null, 2))
  console.log('Saved ' + captures.length + ' screenshots; no uploads, shares or credentials created.')
} finally {
  await browser.close()
  if (auth) {
    db({ action: 'restore', state: auth.state, session: auth.session })
    rmSync('.runtime/help-capture-state.json', { force: true })
    console.log('Restored test preferences and removed temporary session.')
  }
}
