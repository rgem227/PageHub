import { test, expect } from '@playwright/test'

test('网站收藏使用独立平级分类且只展示外链', async ({ page, context }) => {
  test.setTimeout(60000)
  const suffix = Date.now().toString().slice(-8)
  const phone = '137' + suffix
  const headers = { 'x-forwarded-for': '127.82.' + Number(suffix.slice(0, 2)) + '.' + Number(suffix.slice(2, 4)) }
  const sms = await page.request.post('/api/auth/sms', { headers, data: { phone } })
  expect(sms.ok()).toBe(true)
  const code = (await sms.json()).development_code
  const username = 'bookmarks_' + suffix
  expect((await page.request.post('/api/auth/register', { headers, data: { username, full_name: '收藏测试用户', phone, code, password: 'BookmarksCheckPassword!' } })).ok()).toBe(true)
  expect((await page.request.post('/api/auth/login', { data: { account: username, password: 'BookmarksCheckPassword!' } })).ok()).toBe(true)

  const external = await page.request.post('/api/bookmarks', { data: {
    title: '独立收藏网站', url: 'https://example.com/docs', category_name: '开发文档', open_mode: 'embedded',
  } })
  expect(external.status()).toBe(201)
  const category = (await external.json()).category_id
  const empty = await page.request.post('/api/bookmark-categories', { data: { name: '空分类' } })
  expect(empty.status()).toBe(201)
  expect((await (await page.request.get('/api/bookmark-categories')).json()).map((item: any) => item.name)).toEqual(['开发文档'])
  expect((await (await page.request.get('/api/menus')).json())).toEqual([])
  await context.route('https://example.com/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>收藏网站内容</h1>' }))
  expect((await page.request.post('/api/bookmarks', { data: {
    title: '常用开发工具', url: 'https://example.com/tools', category_id: category,
  } })).status()).toBe(201)
  for (const title of ['交换验证甲', '交换验证乙']) {
    expect((await page.request.post('/api/bookmarks', { data: { title, url: 'https://example.com/' + title, category_id: category } })).status()).toBe(201)
  }

  const upload = await page.request.post('/api/uploads', { multipart: {
    file: { name: 'demo.htm', mimeType: 'text/html', buffer: Buffer.from('<!doctype html><title>演示中心页面</title>') },
    menu_path: JSON.stringify(['演示中心', '页面']),
  } })
  expect(upload.status()).toBe(202)
  await expect.poll(async () => (await (await page.request.get('/api/uploads/' + (await upload.json()).id)).json()).status, { timeout: 15000 }).toBe('success')
  expect((await (await page.request.get('/api/bookmark-categories')).json()).map((item: any) => item.name)).toEqual(['开发文档'])

  await page.goto('/')
  const shortcuts = page.locator('.topbar-actions')
  await expect(shortcuts.getByRole('button', { name: '网站收藏', exact: true })).toBeVisible()
  await expect(shortcuts.getByRole('button', { name: 'MCP 接入', exact: true })).toBeVisible()
  const favoriteBox = await shortcuts.getByRole('button', { name: '网站收藏', exact: true }).boundingBox()
  const mcpBox = await shortcuts.getByRole('button', { name: 'MCP 接入', exact: true }).boundingBox()
  expect(favoriteBox!.x).toBeLessThan(mcpBox!.x)
  await shortcuts.getByRole('button', { name: '网站收藏', exact: true }).click()
  await expect(page.getByRole('heading', { name: '网站收藏', exact: true })).toBeVisible()
  await expect(page.locator('.bookmark-card')).toHaveCount(4)
  await expect(page.locator('.bookmark-library')).toContainText('开发文档')
  await expect(page.locator('.bookmark-library')).not.toContainText('演示中心')
  await expect(page.locator('.bookmark-library')).not.toContainText('空分类')
  const bookmarkExport = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载清单', exact: true }).click()
  expect((await bookmarkExport).suggestedFilename()).toBe('PageHub-bookmarks.csv')
  const actions = page.locator('.bookmark-actions')
  await expect(actions.getByRole('button', { name: '新增分类', exact: true })).toBeVisible()
  await expect(actions.getByRole('button', { name: '管理分类', exact: true })).toBeVisible()
  await expect(actions.getByRole('button', { name: '新增收藏', exact: true })).toBeVisible()
  await expect(page.locator('.bookmark-category-bar').getByRole('button', { name: '新增分类' })).toHaveCount(0)
  const firstCard = page.locator('.bookmark-card').filter({ hasText: '常用开发工具' })
  const secondCard = page.locator('.bookmark-card').filter({ hasText: '独立收藏网站' })
  await secondCard.dragTo(page.locator('.bookmark-card').filter({ hasText: '交换验证乙' }))
  await expect.poll(() => page.locator('.bookmark-card').first().innerText()).toContain('常用开发工具')
  await expect.poll(() => page.locator('.bookmark-card').nth(2).innerText()).toContain('交换验证甲')
  await expect.poll(() => page.locator('.bookmark-card').nth(3).innerText()).toContain('独立收藏网站')
  await page.setViewportSize({ width: 800, height: 1000 })
  await page.locator('.bookmark-card').first().dragTo(page.locator('.bookmark-card').nth(3))
  await expect.poll(() => page.locator('.bookmark-card').first().innerText()).toContain('独立收藏网站')
  await expect.poll(() => page.locator('.bookmark-card').nth(3).innerText()).toContain('常用开发工具')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: '打开收藏：独立收藏网站', exact: true }).click()
  await expect(page.locator('.preview-toolbar h1')).toHaveText('独立收藏网站')
  await expect(page.locator('iframe.page-viewer')).toHaveAttribute('src', 'https://example.com/docs')
  await page.getByRole('button', { name: '返回', exact: true }).click()
  await page.getByRole('button', { name: '管理分类', exact: true }).click()
  const manager = page.getByRole('dialog', { name: '管理收藏分类', exact: true })
  await expect(manager.getByRole('row')).toHaveCount(3)
  await manager.getByRole('row').filter({ hasText: '开发文档' }).getByRole('button', { name: '编辑', exact: true }).click()
  const edit = page.getByRole('dialog', { name: '编辑收藏分类', exact: true })
  await edit.getByLabel('平级分类名称', { exact: true }).fill('开发资料')
  await edit.getByRole('button', { name: '保存分类', exact: true }).click()
  await expect(page.locator('.bookmark-library')).toContainText('开发资料')
  expect(category).toBeTruthy()

  const flatUpload = await page.request.post('/api/uploads', { multipart: {
    file: { name: 'flat-demo.htm', mimeType: 'text/html', buffer: Buffer.from('<!doctype html><title>平铺顺序演示</title>') },
    menu_path: JSON.stringify(['工具中心', '图表']),
  } })
  expect(flatUpload.status()).toBe(202)
  await expect.poll(async () => (await (await page.request.get('/api/uploads/' + (await flatUpload.json()).id)).json()).status, { timeout: 15000 }).toBe('success')
  await page.request.patch('/api/preferences', { data: { layout: 'flat' } })
  await page.goto('/?view=overview')
  const flatCard = page.locator('.directory-entry').filter({ hasText: '工具中心 / 图表' })
  const otherFlatCard = page.locator('.directory-entry').filter({ hasText: '演示中心 / 页面' })
  await expect(flatCard).toBeVisible()
  await flatCard.dragTo(otherFlatCard)
  await expect.poll(() => page.locator('.directory-entry').first().innerText()).toContain('工具中心 / 图表')
  await page.reload()
  await expect.poll(() => page.locator('.directory-entry').first().innerText()).toContain('工具中心 / 图表')
  await page.locator('.directory-entry').filter({ hasText: '工具中心 / 图表' }).dragTo(page.locator('.directory-entry').filter({ hasText: '演示中心 / 页面' }))
  await expect.poll(() => page.locator('.directory-entry').first().innerText()).toContain('演示中心 / 页面')
  await expect.poll(() => page.locator('.directory-entry').nth(1).innerText()).toContain('工具中心 / 图表')
})
