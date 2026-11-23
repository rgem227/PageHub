import { test, expect } from '@playwright/test'

test('菜单管理显示入口相对路径并支持拖动菜单树', async ({ page }) => {
  test.setTimeout(60000)
  const suffix = Date.now().toString().slice(-8)
  const clientIp = '127.' + Array.from({ length: 3 }, () => 1 + Math.floor(Math.random() * 254)).join('.')
  const appOrigin = new URL(process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5173')
  const apiOrigin = `${appOrigin.protocol}//${appOrigin.hostname}:8000`
  const headers = { 'x-forwarded-for': clientIp }
  const phone = '137' + suffix
  const sms = await page.request.post(apiOrigin + '/api/auth/sms', { headers, data: { phone } })
  expect(sms.status(), await sms.text()).toBe(200)
  const code = (await sms.json()).development_code
  const registered = await page.request.post(apiOrigin + '/api/auth/register', {
    headers,
    data: { username: 'menu_' + suffix, full_name: '菜单拖动测试', phone, code, password: 'MenuDragTestPassword!' },
  })
  expect(registered.status(), await registered.text()).toBe(200)

  const branchPath = ['拖动验证A', '拖动验证分组', '拖动验证页面']
  const branchCreated = await page.request.post(apiOrigin + '/api/menus', { data: { path: branchPath } })
  expect(branchCreated.ok(), await branchCreated.text()).toBe(true)
  const rootCreated = await page.request.post(apiOrigin + '/api/menus', { data: { path: ['拖动验证B'] } })
  expect(rootCreated.ok(), await rootCreated.text()).toBe(true)
  const upload = await page.request.post(apiOrigin + '/api/uploads', { multipart: {
    file: { name: 'relative-path.html', mimeType: 'text/html', buffer: Buffer.from('<!doctype html><title>入口路径验证</title>') },
    menu_path: JSON.stringify(branchPath),
  } })
  expect(upload.status(), await upload.text()).toBe(202)
  const jobId = (await upload.json()).id
  await expect.poll(async () => (await (await page.request.get(apiOrigin + '/api/uploads/' + jobId)).json()).status,
    { timeout: 15000 }).toBe('success')

  await page.goto('/')
  await page.getByRole('button', { name: '工作台设置', exact: true }).click()
  await page.getByRole('navigation', { name: '工作台功能' }).getByRole('button', { name: '菜单管理', exact: true }).click()
  const rows = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
  const rootA = rows.find((menu: any) => menu.name === '拖动验证A')
  const rootB = rows.find((menu: any) => menu.name === '拖动验证B')
  const branch = rows.find((menu: any) => menu.name === '拖动验证分组')
  const leaf = rows.find((menu: any) => menu.name === '拖动验证页面')
  const pathRow = page.locator(`.el-table__row:has(.menu-drop-target[data-menu-id="${leaf.id}"])`)
  await expect(pathRow.locator('.menu-file-path code')).toHaveText('index.html')

  const x = async (id: string) => (await page.locator(`.menu-drop-target[data-menu-id="${id}"]`).boundingBox())!.x
  expect(await x(branch.id)).toBeGreaterThan(await x(rootA.id) + 20)
  expect(await x(leaf.id)).toBeGreaterThan(await x(branch.id) + 20)

  async function createMenu(level: number, name: string, parentId = '') {
    await page.getByRole('button', { name: '新增菜单', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '新增菜单' })
    await expect(dialog.getByRole('radio', { name: '一级菜单', exact: true })).toBeChecked()
    if (level > 1) {
      const levelName = level === 2 ? '二级菜单' : '三级菜单'
      await dialog.getByText(levelName, { exact: true }).click()
      await expect(dialog.getByRole('radio', { name: levelName, exact: true })).toBeChecked()
      await dialog.locator('.el-select__wrapper').click()
      await page.locator(`.menu-parent-option[data-menu-id="${parentId}"]`).locator('..').click()
    }
    await dialog.getByPlaceholder('输入新菜单名称').fill(name)
    await dialog.getByRole('button', { name: '创建菜单', exact: true }).click()
    await expect(dialog).toBeHidden()
  }
  await createMenu(1, '界面创建一级')
  let createdMenus = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
  const createdRoot = createdMenus.find((menu: any) => menu.name === '界面创建一级')
  expect(createdRoot.parent_id).toBeNull()
  expect(Number(createdRoot.position)).toBe(Math.max(-1, ...rows.filter((menu: any) => menu.parent_id === null).map((menu: any) => Number(menu.position))) + 1)
  await createMenu(2, '界面创建二级', rootA.id)
  createdMenus = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
  const createdSecond = createdMenus.find((menu: any) => menu.name === '界面创建二级')
  expect(createdSecond.parent_id).toBe(rootA.id)
  expect(Number(createdSecond.position)).toBe(Math.max(-1, ...rows.filter((menu: any) => menu.parent_id === rootA.id).map((menu: any) => Number(menu.position))) + 1)
  await createMenu(3, '界面创建三级', createdSecond.id)
  createdMenus = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
  expect(createdMenus.find((menu: any) => menu.name === '界面创建三级').parent_id).toBe(createdSecond.id)

  const rootsBefore = createdMenus.filter((menu: any) => menu.parent_id === null).map((menu: any) => menu.id)
  const indicesBefore = new Map(rootsBefore.map((id: string, index: number) => [id, index]))
  await page.getByRole('button', { name: '拖动菜单：拖动验证A' }).dragTo(
    page.locator(`.menu-drop-target[data-menu-id="${rootB.id}"]`), { targetPosition: { x: 180, y: 2 } })
  await expect.poll(async () => {
    const current = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
    const roots = current.filter((menu: any) => menu.parent_id === null).map((menu: any) => menu.id)
    return roots.indexOf(rootA.id) === indicesBefore.get(rootB.id)
      && roots.indexOf(rootB.id) === indicesBefore.get(rootA.id)
  }).toBe(true)

  await page.getByRole('button', { name: '拖动菜单：拖动验证分组' }).dragTo(
    page.locator(`.menu-drop-target[data-menu-id="${rootB.id}"]`), { targetPosition: { x: 180, y: 20 } })
  await expect.poll(async () => (await page.request.get(apiOrigin + '/api/menus').then(response => response.json()))
    .find((menu: any) => menu.id === branch.id).parent_id).toBe(rootB.id)
  const moved = await page.request.get(apiOrigin + '/api/menus').then(response => response.json())
  expect(moved.find((menu: any) => menu.id === leaf.id).parent_id).toBe(branch.id)
  expect((await page.request.get(apiOrigin + '/api/pages').then(response => response.json()))
    .find((item: any) => item.menu_id === leaf.id)).toBeTruthy()
})
