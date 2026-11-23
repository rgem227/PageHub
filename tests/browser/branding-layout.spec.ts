import { test, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { resolve } from 'node:path'
import { existsSync, readFileSync } from 'node:fs'

test('PageHub 品牌、默认首页及页面卡片自适应', async ({ page }) => {
  test.setTimeout(120000)
  const suffix = Date.now().toString().slice(-8)
  const username = 'layout_' + suffix
  const password = 'LayoutCheckPassword!'
  await page.route('**/api/auth/**', route => route.continue({ headers: { ...route.request().headers(), 'x-forwarded-for': '127.91.' + Number(suffix.slice(0, 2)) + '.' + Number(suffix.slice(2, 4)) } }))
  await page.goto('/?view=pages')
  await expect(page).toHaveTitle('PageHub')
  await expect(page.locator('body')).not.toContainText('页集')
  await expect(page.locator('.brand-wordmark')).toHaveText('PAGE HUB')
  const favicon = await page.request.get('/pagehub.svg')
  expect(favicon.ok()).toBe(true)
  expect(await favicon.text()).toContain('如')
  await page.getByRole('tab', { name: '注册', exact: true }).click()
  await page.getByPlaceholder('3–40 位汉字、字母、数字或下划线').fill(username)
  await page.getByPlaceholder('请输入姓名').fill('布局测试用户')
  await page.getByPlaceholder('中国大陆手机号').fill('139' + suffix)
  await page.getByRole('button', { name: '获取验证码', exact: true }).click()
  const code = (await page.getByRole('alert').filter({ hasText: '本地开发验证码：' }).innerText()).match(/\d{6}/)![0]
  await page.getByPlaceholder('6 位验证码').fill(code)
  await page.getByPlaceholder('至少 10 位字符').fill(password)
  await page.getByPlaceholder('再次输入密码').fill(password)
  await page.getByRole('button', { name: '注册并立即使用', exact: true }).click()
  await expect(page.getByRole('heading', { name: /^工作空间/ })).toBeVisible()
  expect(new URL(page.url()).searchParams.has('view')).toBe(false)
  let pageId = ''
  for (let i = 1; i <= 6; i++) {
    const response = await page.request.post('/api/uploads', { multipart: {
      file: { name: `layout-${i}.htm`, mimeType: 'text/html', buffer: Buffer.from(`<!doctype html><title>企业经营数据分析与智能决策综合管理云平台${i}</title><h1>布局检查${i}</h1>`) },
      menu_path: JSON.stringify(['布局检查', `页面${i}`]),
    } })
    expect(response.status()).toBe(202)
    const job = await response.json()
    await expect.poll(async () => (await (await page.request.get('/api/uploads/' + job.id)).json()).status, { timeout: 15000 }).toBe('success')
    pageId = (await (await page.request.get('/api/uploads/' + job.id)).json()).result.page_id
  }
  for (const layout of ['side', 'top', 'flat']) {
    expect((await page.request.patch('/api/preferences', { data: { layout } })).ok()).toBe(true)
    await page.goto('/?view=pages')
    await expect(page.locator('.page-card')).toHaveCount(6)
    const widths = layout === 'side' ? [[1440, 4], [1100, 3], [800, 2], [390, 1], [1920, 5]] : [[1440, 4], [1200, 4], [1000, 3], [650, 2], [390, 1], [1920, 5]]
    for (const [width, columns] of widths) {
      await page.setViewportSize({ width, height: 960 })
      await expect.poll(() => page.locator('.page-grid').evaluate(el => getComputedStyle(el).gridTemplateColumns.split(' ').length)).toBe(columns)
      await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
      expect(await page.locator('.page-title').first().evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
      await page.screenshot({ path: `.runtime/pagehub-cards-${layout}-${width}.png`, fullPage: true })
    }
  }
  await page.setViewportSize({ width: 1440, height: 960 })
  await page.getByRole('button', { name: '切换深色主题' }).click()
  await expect(page.locator('html')).toHaveClass('dark')
  await page.screenshot({ path: '.runtime/pagehub-cards-dark.png', fullPage: true })
  await page.getByRole('button', { name: '账号菜单', exact: true }).click()
  await page.getByText('退出登录', { exact: true }).click()
  await page.goto('/?view=pages')
  await page.getByPlaceholder('请输入账号').fill(username)
  await page.getByPlaceholder('至少 10 位字符').fill(password)
  await page.getByRole('button', { name: '登录工作空间', exact: true }).click()
  await expect(page.getByRole('heading', { name: /^工作空间/ })).toBeVisible()
  await page.getByRole('button', { name: '账号菜单', exact: true }).click()
  await page.getByText('退出登录', { exact: true }).click()
  await page.goto('/?page=' + pageId)
  await page.getByPlaceholder('请输入账号').fill(username)
  await page.getByPlaceholder('至少 10 位字符').fill(password)
  await page.getByRole('button', { name: '登录工作空间', exact: true }).click()
  await expect(page.frameLocator('iframe').getByRole('heading', { name: '布局检查6' })).toBeVisible()
  expect((await page.locator('iframe').boundingBox())!.height).toBeGreaterThan(700)
  await expect(page.locator('body')).not.toContainText('页集')
})

test('admin 与 test 实际 MCP 示例的展示、交互及浏览器账号隔离', async ({ browser }) => {
  test.skip(!existsSync('.runtime/mcp-isolation-results.json'), '先运行 scripts/publish_mcp_demos.py 发布实际演示数据')
  const report = JSON.parse(readFileSync('.runtime/mcp-isolation-results.json', 'utf8'))
  const python = resolve('.venv/Scripts/python.exe')
  for (const account of report.accounts) {
    const session = execFileSync(python, ['-c', "import sys;sys.path.insert(0,'backend');from app.db import pool,transaction;from app.security import token,digest;pool.open(wait=True);secret=token()\nwith transaction(admin=True) as c:c.execute(\"INSERT INTO sessions(token_hash,user_id,expires_at) SELECT %s,id,now()+interval '10 minutes' FROM users WHERE username=%s\",(digest(secret),sys.argv[1]))\nprint(secret);pool.close()", account.username], { encoding: 'utf8' }).trim()
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 } })
    try {
      await context.addCookies([{ name: 'hub_session', value: session, url: 'http://127.0.0.1:5173' }])
      const page = await context.newPage()
      const errors: string[] = []
      page.on('pageerror', e => errors.push(e.message))
      await page.goto('/?view=pages&pq=' + encodeURIComponent(account.username === 'admin' ? '管理演示 · ' : '个人演示 · '))
      await expect(page.locator('.page-card')).toHaveCount(6)
      await page.screenshot({ path: `.runtime/pagehub-${account.username}-mcp-pages.png`, fullPage: true })
      if (existsSync('.runtime/bookmark-demos.json')) {
        const demos = JSON.parse(readFileSync('.runtime/bookmark-demos.json', 'utf8'))
        const own = demos.find((a: any) => a.username === account.username)
        const foreign = demos.find((a: any) => a.username !== account.username)
        await page.goto('/?view=bookmarks')
        for (const bookmark of own.bookmarks) await expect(page.getByRole('button', { name: '打开收藏：' + bookmark.title, exact: true })).toBeVisible()
        for (const bookmark of foreign.bookmarks) expect((await page.request.get('/api/bookmarks/' + bookmark.id)).status()).toBe(404)
        await page.screenshot({ path: `.runtime/bookmarks-${account.username}-actual.png`, fullPage: true })
      }
      const workspace = await (await page.request.get('/api/workspace')).json()
      for (const sample of account.pages.filter((_: unknown, index: number) => index === 2 || index === 5 || (account.username === 'test' && index === 1))) {
        await page.goto('http://127.0.0.1:5173/?page=' + sample.page_id)
        const frame = workspace.tenant.open_mode === 'external' ? page : page.frameLocator('iframe')
        await expect(frame.locator('footer')).toContainText(sample.marker)
        if (sample.title.includes('报价')) {
          await frame.locator('#days').fill('10')
          await expect(frame.locator('#quote')).toContainText('17,100')
        } else if (sample.title.includes('预算')) {
          await frame.locator('#people').fill('2')
          await expect(frame.locator('#budget')).toContainText('人均 ¥1300.00')
        } else if (sample.title.includes('打卡')) {
          await frame.locator('input[type=checkbox]').first().check()
          await expect(frame.locator('#progress')).toHaveText('今日完成 1 / 4')
        }
      }
      const foreign = report.accounts.find((a: any) => a.username !== account.username).pages[0]
      expect((await page.request.get('http://127.0.0.1:5173/api/pages/' + foreign.page_id)).status()).toBe(404)
      await page.goto('http://127.0.0.1:5173/?page=' + foreign.page_id)
      await expect(page.locator('.el-message--error')).toBeVisible()
      await expect(page.locator('iframe')).toHaveCount(0)
      expect(errors).toEqual([])
    } finally {
      await context.close()
      execFileSync(python, ['-c', "import sys;sys.path.insert(0,'backend');from app.db import pool,transaction;from app.security import digest;pool.open(wait=True)\nwith transaction(admin=True) as c:c.execute('DELETE FROM sessions WHERE token_hash=%s',(digest(sys.argv[1]),))\npool.close()", session])
    }
  }
})
