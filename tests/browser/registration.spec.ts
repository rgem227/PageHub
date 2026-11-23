import { test, expect } from '@playwright/test'

test('注册要求姓名并保存到用户资料', async ({ page }) => {
  const suffix = Date.now().toString().slice(-8)
  const username = 'name_' + suffix
  const phone = '135' + suffix
  const clientIp = '127.93.' + Number(suffix.slice(0, 2)) + '.' + Number(suffix.slice(2, 4))
  await page.route('**/api/auth/**', route => route.continue({ headers: { ...route.request().headers(), 'x-forwarded-for': clientIp } }))
  await page.goto('/?register=1')
  const fullName = page.getByPlaceholder('请输入姓名')
  const fullNameField = page.locator('.el-form-item').filter({ has: fullName })
  await expect(fullNameField).toHaveClass(/is-required/)
  await page.getByRole('button', { name: '注册并立即使用', exact: true }).click()
  await expect(fullNameField.locator('.el-form-item__error')).toHaveText('请输入姓名')
  await page.getByPlaceholder('3–40 位汉字、字母、数字或下划线').fill(username)
  await fullName.fill('姓名验证用户')
  await page.getByPlaceholder('中国大陆手机号').fill(phone)
  await page.getByRole('button', { name: '获取验证码' }).click()
  const alert = page.getByRole('alert').filter({ hasText: '本地开发验证码：' })
  const code = (await alert.innerText()).match(/\d{6}/)![0]
  await page.getByPlaceholder('6 位验证码').fill(code)
  await page.getByPlaceholder('至少 10 位字符').fill('NameFieldPassword!')
  await page.getByPlaceholder('再次输入密码').fill('NameFieldPassword!')
  await page.getByRole('button', { name: '注册并立即使用', exact: true }).click()
  await expect(page.getByRole('heading', { name: '工作空间', exact: true })).toBeVisible()
  const profile = await page.request.get('/api/auth/me')
  expect((await profile.json()).full_name).toBe('姓名验证用户')
})
