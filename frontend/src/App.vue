<script setup lang="ts">
import { computed, h, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox, ClickOutside as vClickOutside } from 'element-plus'
import type { FormInstance, FormRules, MenuInstance } from 'element-plus'
import { Grid, FolderOpened, UploadFilled, Connection, Setting, Monitor, Sunny, Moon, ArrowRight, Plus, Search, Document, SwitchButton, Refresh, User, Fold, Expand, More, Star, Share, QuestionFilled } from '@element-plus/icons-vue'
import { api, bytes, date, json } from './api'
import MenuBranch from './MenuBranch.vue'
import Catalogue from './Catalogue.vue'
import AdminConsole from './AdminConsole.vue'
import UploadHistory from './UploadHistory.vue'
import MenuManager from './MenuManager.vue'
import FileManager from './FileManager.vue'
import BookmarkManager from './BookmarkManager.vue'
import BookmarkViewer from './BookmarkViewer.vue'

const platformOrigin = location.origin
const moreMenuIcon = () => h(More, { role: 'img', 'aria-label': '更多菜单' })

const user = ref<any>(null)
const ready = ref(false)
const busy = ref(false)
const authTab = ref(new URLSearchParams(location.search).has('register') ? 'register' : 'login')
const auth = reactive({ account: '', username: '', full_name: '', password: '', phone: '', code: '', confirm: '' })
const authForm = ref<FormInstance>()
const authRules = computed<FormRules>(() => ({
  account: [{ required: true, whitespace: true, message: '请输入用户名或手机号', trigger: 'blur' }],
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }, { min: 3, max: 40, message: '用户名需为 3–40 个字符', trigger: 'blur' }],
  full_name: [{ required: true, whitespace: true, message: '请输入姓名', trigger: 'blur' }, { max: 60, message: '姓名不能超过 60 个字符', trigger: 'blur' }],
  phone: [{ required: true, message: '请输入手机号', trigger: 'blur' }, { transform: value => String(value).trim().replace(/[ -]/g, '').replace(/^\+86/, ''), pattern: /^1[3-9]\d{9}$/, message: '请输入有效的中国大陆手机号', trigger: 'blur' }],
  code: [{ required: true, message: '请输入短信验证码', trigger: 'blur' }, { pattern: /^\d{6}$/, message: '短信验证码须为 6 位数字', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }, { min: authTab.value === 'register' ? 10 : 1, max: 128, message: '密码需为 10–128 个字符', trigger: 'blur' }],
  confirm: [{ required: true, message: '请再次输入密码', trigger: 'blur' }, { validator: (_rule, value, callback) => callback(value === auth.password ? undefined : new Error('两次密码输入不一致')), trigger: 'blur' }],
}))
const developmentCode = ref('')
const developmentCodeLabel = ref('本地开发验证码')
const routeParams = new URLSearchParams(location.search)
const oauthEncoded = routeParams.get('oauth_request')
function decodeOAuthRequest(value: string | null) {
  if (!value) return null
  try {
    const binary = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4))
    return JSON.parse(new TextDecoder().decode(Uint8Array.from(binary, char => char.charCodeAt(0))))
  } catch { return null }
}
const oauthRequest = ref<any>(decodeOAuthRequest(oauthEncoded))
const oauthInfo = ref<any>(null)
const oauthError = ref(oauthEncoded && !oauthRequest.value ? 'OAuth 授权请求格式无效，请从智能体客户端重新发起。' : '')
const oauthExpiry = ref<number | null>(90)
const oauthBusy = ref(false)
const section = ref(routeParams.get('view') || 'overview')
const filePage = ref(routeParams.get('file_page') || '')
const workbenchOpen = ref(false)
const sidebarHidden = ref(false)
const workspace = ref<any>({ tenant: {}, stats: {}, storage: {}, limits: {} })
const externalPreviewMinutes = ref<number | undefined>(5)
watch(() => workspace.value.external_preview_minutes, value => { externalPreviewMinutes.value = value ?? 5 })
const menus = ref<any[]>([])
const pages = ref<any[]>([])
const uploads = ref<any[]>([])
const credentials = ref<any[]>([])
const myShares = ref<any[]>([])
const receivedShares = ref<any[]>([])
const query = ref(routeParams.get('pq') || '')
const selectedMenu = ref(routeParams.get('menu') || '')
const pageIndex = ref(Math.max(1, parseInt(routeParams.get('pp') || '1') || 1))
const pageTotal = ref(0)
const pageLoading = ref(false)
const pageError = ref('')
const routeKey = ref(0)
const catalogueState = reactive({ q: routeParams.get('q') || '', category: routeParams.get('category') || '', sort: routeParams.get('sort') || 'position', page: Math.max(1, parseInt(routeParams.get('p') || '1') || 1), scroll: 0 })
const bookmarkState = reactive({ q: routeParams.get('bq') || '', category: routeParams.get('bc') || '', sort: routeParams.get('bs') || 'position', page: Math.max(1, parseInt(routeParams.get('bp') || '1') || 1), view: routeParams.get('bv') === 'list' ? 'list' : 'cards' })
let pageRequest = 0
let previewRequest = 0
let draggedPageId = ''
let pageSearchTimer: ReturnType<typeof setTimeout> | undefined
const uploadDialog = ref(false)
const uploadForm = reactive({ title: '', path: '', entry: '', page_id: '' })
const chosenFile = ref<File>()
const viewer = ref<any>(null)
const preview = ref('')
const versionsDialog = ref(false)
const versions = ref<any[]>([])
const versionPage = ref<any>(null)
const freshToken = ref('')
const freshCredentialId = ref('')
const mcpUrl = ref('')
const oauthMetadataUrl = ref('')
const oauthResourceMetadataUrl = ref('')
const oauthTokenUrl = ref('')
const oauthGrants = ref<any[]>([])
const mcpConfig = computed(() => JSON.stringify({ mcpServers: { 'page-hub': {
  url: mcpUrl.value, headers: { Authorization: 'Bearer ' + (freshToken.value || '<创建凭证后自动填入>') },
} } }, null, 2))
const mcpAgentPrompt = computed(() => [
  '你是 PageHub 页面发布助手。用户明确要求发布并提供 HTML/HTM 文件或 ZIP 文件时，通过 PageHub MCP 工具完成发布。',
  '',
  '发布要求：',
  '1. HTML/HTM：读取用户提供文件的完整文本，调用 upload_html，传入 html、title、menu_path 和原始 filename。不要只凭截图或描述虚构文件内容。',
  '2. ZIP：将整个 ZIP 文件按标准 Base64 编码（不要添加 data URI 前缀或截断），调用 upload_zip，传入 zip_base64、title、menu_path。保留 ZIP 内的相对目录和资源路径；默认入口是根目录 index.html，入口不同则传 entry_file。可检查根目录 manifest.json，但不要猜测入口文件。',
  '3. 上传前调用 list_menus，读取 depth、has_children、can_attach_page、can_create_child。can_attach_page=true 表示可直接挂接；如果用户指定的路径停在 has_children=true 且 can_create_child=true 的空父菜单上，上传时可直接使用该父级路径，PageHub 会按实际页面标题创建下一级菜单并挂接，最多三级。若父节点没有子菜单且 can_attach_page=true，则直接挂接该节点。已挂页面的节点不能添加子菜单；can_create_child=false 或层级已达三级时，不要猜菜单名称，应询问用户希望使用的新菜单名称，或提示其到后台“菜单管理”创建空闲末级菜单。用户未指定菜单时先询问；用户同意默认位置后可使用 ["未分类"]。',
  '4. 上传新页面时，缺少的路径会自动创建。若用户指定自定义的完整路径，可将完整路径传给上传工具；名称冲突时由系统为自动创建的末级菜单追加序号。',
  '5. 只有用户明确要求更新已有页面并提供或确认 page_id 时才传 page_id；更新前用 list_pages 确认该页面，允许保留它自己的原菜单。否则新建页面。',
  '6. 上传工具返回任务 ID 后，调用 get_upload_status 轮询到 success 或 failed。成功时使用结果中的最终 menu_path 和 menu_path_auto_extended，并在自动延伸菜单路径后调用 list_menus 确认新末级菜单；再调用 get_page_url 获取最终链接，返回标题、完整菜单路径和访问链接。失败时如实说明 error，不要声称发布成功。',
  '7. 上传需要读写凭证；只读凭证不能上传页面或创建菜单。',
  '',
  'OAuth 2.1：支持动态客户端注册、授权码 + PKCE S256 和 refresh token 轮换。支持 OAuth 的 MCP 客户端可使用 MCP URL 自动发现授权信息；用户网页登录并同意后，客户端应自动刷新 access token 并保存轮换后的 refresh token，无需将 refresh token 交给模型。授权有效期可选 7、30、90、180、365 天或永久；永久授权直到用户撤销、账号停用或租户停用。scope 未提供时默认申请 read write；客户端显式申请 read 时为只读，read write 为读写；用户确认页会显示申请权限。授权服务发现地址：' + oauthMetadataUrl.value,
  'OAuth access token 有效 3600 秒。令牌端点：' + oauthTokenUrl.value + '。客户端续期时以 application/x-www-form-urlencoded POST grant_type=refresh_token、client_id 和当前 refresh_token；必须保存响应中新的 refresh_token，旧 refresh token 只能使用一次，重复使用会撤销该授权。响应会返回 expires_in 和 authorization_expires_at。',
  '',
  '将下面的 MCP 配置添加到智能体支持的 MCP 客户端：',
  '',
  '```json',
  mcpConfig.value,
  '```',
].join('\n'))
const credentialForm = reactive({ name: '', readonly: false, expires_in_days: 90 as number | null })
const passwordDialog = ref(false)
const passwordForm = reactive({ old_password: '', new_password: '' })
const menuDialog = ref(false)
const menuEditing = ref<any>(null)
const menuForm = reactive({ level: 1, name: '', parent_id: '', position: 0, visible: true })
const pageDialog = ref(false)
const pageEditing = ref<any>(null)
const pageForm = reactive({ title: '', menu_id: '', target_menu_id: '' })
const shareDialog = ref(false)
const shareMenu = ref<any>(null)
const sharePages = ref<any[]>([])
const shares = ref<any[]>([])
const sharePageId = ref('')
const shareDays = ref<number | null>(7)
const shareMode = ref<'link' | 'account'>('link')
const shareRecipientId = ref('')
const shareRecipientQuery = ref('')
const shareRecipients = ref<any[]>([])
const shareBusy = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let presenceTimer: ReturnType<typeof setInterval> | undefined
let menuSyncTimer: ReturnType<typeof setInterval> | undefined

const isAdmin = computed(() => user.value?.role === 'superadmin')
const topLayout = computed(() => section.value !== 'admin' && workspace.value.tenant.layout === 'top')
const flatLayout = computed(() => section.value !== 'admin' && workspace.value.tenant.layout === 'flat')
const sideLayout = computed(() => section.value !== 'admin' && workspace.value.tenant.layout === 'side')
const flatHome = computed(() => flatLayout.value && section.value === 'overview' && !viewer.value)
const nav = computed(() => [
  { id: 'overview', title: '工作空间', icon: Grid }, { id: 'pages', title: '全部页面', icon: Document },
  { id: 'menus', title: '菜单管理', icon: FolderOpened }, { id: 'uploads', title: '上传记录', icon: UploadFilled },
  { id: 'files', title: '文件管理', icon: FolderOpened },
  { id: 'shares', title: '我的分享', icon: Share },
  { id: 'received-shares', title: '共享给我', icon: Share },
  { id: 'bookmarks', title: '网站收藏', icon: Star },
  { id: 'settings', title: '空间设置', icon: Setting },
  ...(isAdmin.value ? [{ id: 'admin', title: '平台管理', icon: Monitor }] : []),
])
const heading = computed(() => section.value === 'mcp' ? 'MCP 接入' : nav.value.find(n => n.id === section.value)?.title || '工作空间')
function descendants(id: string): string[] { return [id, ...menus.value.filter(m => m.parent_id === id).flatMap(m => descendants(m.id))] }
function tree(parent: string | null = null): any[] {
  const children = new Map<string | null, any[]>()
  for (const menu of menus.value) {
    if (!children.has(menu.parent_id)) children.set(menu.parent_id, [])
    children.get(menu.parent_id)!.push(menu)
  }
  function branch(id: string | null): any[] { return (children.get(id) || []).filter(m => m.visible).map(m => ({ ...m, children: branch(m.id) })) }
  return branch(parent)
}
const menuTree = computed(() => tree())
const tenantMenu = ref<MenuInstance>()
watch(menuTree, async () => {
  await nextTick()
  if (topLayout.value) tenantMenu.value?.handleResize()
})
function closeTopMenus(mouseup: MouseEvent, mousedown: MouseEvent) {
  if (!topLayout.value || [mouseup, mousedown].some(event => (event.target as Element)?.closest('.tenant-top-menu-popover'))) return
  tenantMenu.value?.close('sub-menu-more')
  for (const menu of menus.value) tenantMenu.value?.close(menu.id)
}
function menuDepth(id: string): number {
  const menu = menus.value.find(m => m.id === id)
  return menu ? 1 + (menu.parent_id ? menuDepth(menu.parent_id) : 0) : 0
}
function menuIsVisible(id: string | null): boolean {
  const menu = id ? menus.value.find(m => m.id === id) : null
  return !menu || (menu.visible && menuIsVisible(menu.parent_id))
}
const menuParentOptions = computed(() => {
  if (!menuEditing.value) return []
  const subtree = descendants(menuEditing.value.id)
  const height = Math.max(...subtree.map(id => menuDepth(id))) - menuDepth(menuEditing.value.id) + 1
  const options: any[] = []
  function append(parentId: string | null, depth: number) {
    const children = menus.value.filter(m => m.parent_id === parentId)
      .sort((a, b) => Number(a.position) - Number(b.position) || a.name.localeCompare(b.name))
    for (const menu of children) {
      const inSubtree = subtree.includes(menu.id)
      const tooDeep = depth + height > 3
      const occupied = Number(menu.page_count) > 0
      options.push({ ...menu, depth, disabled: inSubtree || tooDeep || occupied,
        reason: inSubtree ? '自身或子菜单不能作为上级' : tooDeep ? '移动后菜单层级会超过三级' : occupied ? '已有页面，请先移到其他末级菜单' : '' })
      append(menu.id, depth + 1)
    }
  }
  append(null, 1)
  return options
})
const menuCreateParentOptions = computed(() => menus.value
  .filter(menu => menuDepth(menu.id) === menuForm.level - 1)
  .sort((a, b) => Number(a.position) - Number(b.position) || a.name.localeCompare(b.name))
  .map(menu => ({ ...menu, depth: menuForm.level - 1, disabled: Number(menu.page_count) > 0,
    reason: Number(menu.page_count) > 0 ? '此菜单已挂接页面，不能作为上级' : '' })))
const menuCreatePathPreview = computed(() => {
  const name = menuForm.name.trim() || '菜单名称'
  if (menuForm.level === 1) return name
  const parent = menus.value.find(menu => menu.id === menuForm.parent_id)
  return parent ? [...menuPath(parent.id), name].join(' / ') : `请选择${menuForm.level === 2 ? '一级' : '二级'}菜单 / ${name}`
})
const pageMenuOptions = computed(() => {
  const options: any[] = []
  function append(parentId: string | null, depth: number) {
    const children = menus.value.filter(m => m.parent_id === parentId)
      .sort((a, b) => Number(a.position) - Number(b.position) || a.name.localeCompare(b.name))
    for (const menu of children) {
      if (!Number(menu.page_count))
        options.push({ ...menu, depth, group: Boolean(menu.has_children), hidden: !menuIsVisible(menu.id) })
      append(menu.id, depth + 1)
    }
  }
  append(null, 1)
  return options
})
function menuName(id: string): string {
  const m = menus.value.find(m => m.id === id)
  return m ? (m.parent_id ? menuName(m.parent_id) + ' / ' : '') + m.name : '—'
}
function menuPath(id: string): string[] {
  const menu = menus.value.find(item => item.id === id)
  return menu ? [...(menu.parent_id ? menuPath(menu.parent_id) : []), menu.name] : []
}
function cardTone(id: string) {
  let hash = 0
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash % 4
}
function theme(value: string) { document.documentElement.classList.toggle('dark', value === 'dark') }
function sendPresenceHeartbeat() {
  if (user.value && !user.value.must_change_password && document.visibilityState === 'visible')
    void api('/auth/heartbeat', json('POST')).catch(() => {})
}
function presenceVisibilityChanged() { if (document.visibilityState === 'visible') { sendPresenceHeartbeat(); void syncMenus() } }
async function syncMenus() {
  if (!user.value || user.value.must_change_password || document.visibilityState !== 'visible') return
  try {
    const latest = await api<any[]>('/menus')
    if (JSON.stringify(latest) !== JSON.stringify(menus.value)) {
      menus.value = latest
      if (['overview', 'pages'].includes(section.value)) await fetchPages()
    }
  } catch { /* keep the current menu state until the next sync */ }
}
async function attempt(fn: () => Promise<void>) { try { await fn() } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '操作失败') } }
function syncRoute(replace = false) {
  const url = new URL(location.href)
  const admin = url.searchParams.get('admin')
  url.search = ''
  if (section.value !== 'overview') url.searchParams.set('view', section.value)
  if (section.value === 'files' && filePage.value) url.searchParams.set('file_page', filePage.value)
  if (section.value === 'admin' && admin) url.searchParams.set('admin', admin)
  if (selectedMenu.value) url.searchParams.set('menu', selectedMenu.value)
  if (query.value) url.searchParams.set('pq', query.value)
  if (pageIndex.value > 1) url.searchParams.set('pp', String(pageIndex.value))
  if (viewer.value) url.searchParams.set(viewer.value.kind === 'bookmark' ? 'bookmark' : viewer.value.kind === 'account-share' ? 'received_share' : 'page', viewer.value.kind === 'account-share' ? viewer.value.share_id : viewer.value.id)
  if (section.value === 'bookmarks') {
    if (bookmarkState.q) url.searchParams.set('bq', bookmarkState.q)
    if (bookmarkState.category) url.searchParams.set('bc', bookmarkState.category)
    if (bookmarkState.sort !== 'position') url.searchParams.set('bs', bookmarkState.sort)
    if (bookmarkState.page > 1) url.searchParams.set('bp', String(bookmarkState.page))
    if (bookmarkState.view === 'list') url.searchParams.set('bv', 'list')
  }
  if (catalogueState.q) url.searchParams.set('q', catalogueState.q)
  if (catalogueState.category) url.searchParams.set('category', catalogueState.category)
  if (catalogueState.sort !== 'position') url.searchParams.set('sort', catalogueState.sort)
  if (catalogueState.page > 1) url.searchParams.set('p', String(catalogueState.page))
  if (url.href !== location.href) history[replace ? 'replaceState' : 'pushState'](null, '', url)
}
async function fetchPages() {
  const serial = ++pageRequest
  if (!['overview','pages'].includes(section.value) || flatHome.value || viewer.value) return
  pageLoading.value = true; pageError.value = ''
  try {
    const params = new URLSearchParams({ q: query.value, page: String(pageIndex.value), size: section.value === 'overview' ? '6' : '50' })
    if (selectedMenu.value) params.set('menu', selectedMenu.value)
    const result = await api('/page-list?' + params)
    if (serial !== pageRequest) return
    pages.value = result.items; pageTotal.value = result.total; pageIndex.value = result.page
  } catch (e: any) { if (serial === pageRequest) pageError.value = e.message }
  finally { if (serial === pageRequest) pageLoading.value = false }
}
async function movePageCard(targetId: string) {
  const sourceId = draggedPageId
  draggedPageId = ''
  if (!sourceId || sourceId === targetId) return
  await attempt(async () => { await api('/pages/' + sourceId + '/move', json('POST', { target_id: targetId, before: true })); await fetchPages() })
}
watch([query, selectedMenu], () => {
  ++pageRequest; pageIndex.value = 1
  if (pageSearchTimer) clearTimeout(pageSearchTimer)
  if (section.value === 'pages' && !viewer.value) pageSearchTimer = setTimeout(() => { syncRoute(true); void fetchPages() }, 250)
})
async function refresh() {
  const result = await Promise.all([api('/workspace'), api<any[]>('/menus'), api<any[]>('/uploads')])
  workspace.value = result[0]; menus.value = result[1]; uploads.value = result[2]
  await fetchPages()
}
async function navigate(id: string, pageId = '') {
  ++previewRequest
  workbenchOpen.value = false
  filePage.value = id === 'files' ? pageId : ''
  if (id !== 'mcp') { freshToken.value = ''; freshCredentialId.value = '' }
  section.value = id; selectedMenu.value = ''; query.value = ''; pageIndex.value = 1; viewer.value = null; preview.value = ''; pages.value = []
  Object.assign(bookmarkState, { q: '', category: '', sort: 'position', page: 1 })
  syncRoute()
  await loadSection()
}
async function loadSection() {
  await attempt(async () => {
    if (section.value === 'mcp') {
      const result = await Promise.all([api('/credentials'), api('/mcp/config'), api('/oauth/grants')])
      credentials.value = result[0]; mcpUrl.value = result[1].url
      oauthMetadataUrl.value = result[1].authorization_server_metadata_url
      oauthResourceMetadataUrl.value = result[1].protected_resource_metadata_url
      oauthTokenUrl.value = result[1].token_endpoint_url
      oauthGrants.value = result[2]
    }
    if (section.value === 'shares') myShares.value = await api('/shares')
    if (section.value === 'received-shares') receivedShares.value = await api('/shares/received')
  })
  await fetchPages()
}
function filterMenu(id: string) {
  ++previewRequest
  freshToken.value = ''; freshCredentialId.value = ''
  const menu = menus.value.find(m => m.id === id)
  const leaf = !menus.value.some(m => m.parent_id === id)
  const singlePage = Number(menu?.page_count) === 1 && leaf
  const page = { id: menu?.single_page_id, title: menu?.single_page_title, menu_id: id }
  if (flatLayout.value && singlePage && workspace.value.tenant.open_mode === 'external') { void openPage(page); return }
  section.value = 'pages'; selectedMenu.value = id; query.value = ''; pageIndex.value = 1; viewer.value = null; preview.value = ''; pages.value = []; pageTotal.value = 0; pageError.value = ''
  Object.assign(bookmarkState, { q: '', category: '', sort: 'position', page: 1 })
  if (singlePage) void openPage(page)
  else { syncRoute(); void fetchPages() }
}
async function restoreRoute() {
  ++previewRequest
  const params = new URLSearchParams(location.search)
  const requested = params.get('view') || 'overview'
  section.value = ['overview','pages','menus','uploads','files','shares','received-shares','bookmarks','settings','mcp', ...(isAdmin.value ? ['admin'] : [])].includes(requested) ? requested : 'overview'; viewer.value = null; preview.value = ''
  filePage.value = params.get('file_page') || ''
  if (section.value !== 'mcp') { freshToken.value = ''; freshCredentialId.value = '' }
  selectedMenu.value = params.get('menu') || ''; query.value = params.get('pq') || ''
  await nextTick()
  pageIndex.value = Math.max(1, parseInt(params.get('pp') || '1') || 1)
  Object.assign(catalogueState, { q: params.get('q') || '', category: params.get('category') || '', sort: params.get('sort') || 'position', page: Math.max(1, parseInt(params.get('p') || '1') || 1) })
  Object.assign(bookmarkState, { q: params.get('bq') || '', category: params.get('bc') || '', sort: ['position','name','updated'].includes(params.get('bs') || '') ? params.get('bs') : 'position', page: Math.max(1, parseInt(params.get('bp') || '1') || 1), view: params.get('bv') === 'list' ? 'list' : 'cards' })
  ++routeKey.value
  const id = params.get('page')
  const bookmarkId = params.get('bookmark')
  const receivedShareId = params.get('received_share')
  if (receivedShareId) {
    section.value = 'received-shares'
    receivedShares.value = await api('/shares/received')
    const row = receivedShares.value.find((item: any) => item.id === receivedShareId)
    if (row) await openReceivedShare(row, true, false)
    else ElMessage.warning('此账号没有这条页面分享')
  }
  else if (bookmarkId) await attempt(async () => { await openEntry(await api('/bookmarks/' + bookmarkId), true, false) })
  else if (id) await attempt(async () => { await openPage(await api('/pages/' + id), true, false) })
  else await loadSection()
}
async function returnList() { ++previewRequest; viewer.value = null; preview.value = ''; syncRoute(); if (section.value === 'received-shares') receivedShares.value = await api('/shares/received'); else await fetchPages() }
function changePage(value: number) { pageIndex.value = value; syncRoute(true); void fetchPages() }
async function authenticated(account: any, signingIn = false) {
  user.value = account; theme(account.theme)
  if (account.must_change_password) { passwordDialog.value = true; return }
  if (oauthRequest.value) { await loadOAuthRequest(); return }
  sendPresenceHeartbeat()
  if (signingIn && !oauthRequest.value && !['page', 'bookmark', 'received_share'].some(key => new URLSearchParams(location.search).has(key))) history.replaceState(null, '', location.pathname)
  await refresh()
  await restoreRoute()
}
async function loadOAuthRequest() {
  if (!oauthRequest.value || !user.value || user.value.must_change_password) return
  oauthBusy.value = true; oauthError.value = ''
  try {
    const query = new URLSearchParams(oauthRequest.value).toString()
    oauthInfo.value = await api('/oauth/authorize/details?' + query)
  } catch (error) { oauthError.value = error instanceof Error ? error.message : '无法读取 OAuth 授权请求' }
  oauthBusy.value = false
}
async function approveOAuth() {
  if (!oauthRequest.value) return
  oauthBusy.value = true
  await attempt(async () => {
    const result = await api('/oauth/authorize', json('POST', { ...oauthRequest.value, expires_in_days: oauthExpiry.value }))
    location.assign(result.redirect_uri)
  })
  oauthBusy.value = false
}
async function denyOAuth() {
  if (!oauthRequest.value) { location.assign('/'); return }
  oauthBusy.value = true
  await attempt(async () => {
    const result = await api('/oauth/authorize/deny', json('POST', oauthRequest.value))
    location.assign(result.redirect_uri)
  })
  oauthBusy.value = false
}
async function submitAuth() {
  if (!await authForm.value?.validate().catch(() => false)) return
  busy.value = true
  await attempt(async () => {
    if (authTab.value === 'register' && auth.password !== auth.confirm) throw new Error('两次密码输入不一致')
    const data = authTab.value === 'login' ? { account: auth.account, password: auth.password } : { username: auth.username, full_name: auth.full_name.trim(), password: auth.password, phone: auth.phone, code: auth.code }
    await authenticated(await api('/auth/' + authTab.value, json('POST', data)), true)
    auth.password = ''; auth.confirm = ''; developmentCode.value = ''
  })
  busy.value = false
}
async function sendCode() {
  if (!await authForm.value?.validateField('phone').catch(() => false)) return
  await attempt(async () => { const r = await api('/auth/sms', json('POST', { phone: auth.phone })); developmentCode.value = r.development_code || ''; developmentCodeLabel.value = r.verification_mode === 'temporary' ? '临时验证码（短信未发送）' : '本地开发验证码'; ElMessage.success(r.message) })
}
async function logout() { await attempt(async () => { await api('/auth/logout', json('POST')); user.value = null; viewer.value = null; freshToken.value = ''; freshCredentialId.value = ''; theme('light') }) }
async function savePreferences(values: any) {
  await attempt(async () => { await api('/preferences', json('PATCH', values)); if (values.theme) { user.value.theme = values.theme; theme(values.theme) } if (values.directory_view) user.value.directory_view = values.directory_view; await refresh(); ElMessage.success('设置已保存') })
}
function showUpload(page?: any) {
  Object.assign(uploadForm, { title: page?.title || '', path: page ? menuName(page.menu_id) : '', entry: '', page_id: page?.id || '' })
  chosenFile.value = undefined; uploadDialog.value = true
}
async function submitUpload() {
  if (!chosenFile.value) { ElMessage.warning('请选择 HTML 或 ZIP 文件'); return }
  busy.value = true
  await attempt(async () => {
    const data = new FormData(); data.append('file', chosenFile.value!)
    data.append('title', uploadForm.title); data.append('entry_file', uploadForm.entry); data.append('page_id', uploadForm.page_id)
    data.append('menu_path', JSON.stringify(uploadForm.path ? uploadForm.path.split('/').map(s => s.trim()) : []))
    await api('/uploads', { method: 'POST', body: data }); uploadDialog.value = false; await navigate('uploads'); await refresh(); ElMessage.success('上传已接收，正在解析')
  }); busy.value = false
}
async function openPage(page: any, direct = false, updateHistory = true) {
  const serial = ++previewRequest
  const external = workspace.value.tenant.open_mode === 'external'
  // 先在点击时打开空白页，避免等待授权请求后被浏览器当成弹窗拦截。
  const tab = external && !direct ? window.open('about:blank', '_blank') : null
  if (external && !direct && !tab) { ElMessage.warning('浏览器阻止了新标签页，请允许此站点打开弹窗'); return }
  if (tab) { tab.opener = null; tab.document.title = '正在打开页面…'; tab.document.body.textContent = '正在获取页面访问授权…' }
  try {
    const result = await api('/pages/' + page.id + '/preview')
    if (external) {
      if (tab) tab.location.replace(result.url)
      else window.location.replace(result.url)
    } else if (serial === previewRequest) { viewer.value = page; preview.value = result.url; if (updateHistory) syncRoute() }
  } catch (error: any) { tab?.close(); ElMessage.error(error.message || '页面打开失败') }
}
async function openReceivedShare(row: any, direct = false, updateHistory = true) {
  const serial = ++previewRequest
  try {
    const result = await api('/shares/received/' + row.id + '/preview')
    if (serial === previewRequest) {
      viewer.value = { kind: 'account-share', id: row.page_id, share_id: row.id, title: row.title }
      preview.value = result.url
      if (updateHistory) syncRoute()
    }
  } catch (error: any) { if (!direct) ElMessage.error(error.message || '页面打开失败') }
}
async function refreshPreview() {
  if (viewer.value?.kind === 'account-share') await openReceivedShare({ id: viewer.value.share_id, page_id: viewer.value.id, title: viewer.value.title })
  else if (viewer.value) await openPage(viewer.value)
}
async function openEntry(entry: any, direct = false, updateHistory = true) {
  if (entry.kind !== 'bookmark') { await openPage(entry, direct, updateHistory); return }
  ++previewRequest
  if (entry.open_mode === 'external') {
    if (direct) window.location.assign(entry.url)
    else window.open(entry.url, '_blank', 'noopener,noreferrer')
    return
  }
  viewer.value = entry; preview.value = ''
  if (updateHistory) syncRoute()
}
async function showVersions(page: any) { await attempt(async () => { versionPage.value = page; versions.value = await api('/pages/' + page.id + '/versions'); versionsDialog.value = true }) }
async function rollback(version: any) {
  await attempt(async () => { await ElMessageBox.confirm('将当前发布版本切换至此版本？', '回滚版本'); await api(`/pages/${versionPage.value.id}/rollback/${version.id}`, json('POST')); await refresh(); versionsDialog.value = false; ElMessage.success('版本已回滚') })
}
async function removePage(page: any) {
  await attempt(async () => { await ElMessageBox.confirm('页面将停止访问，文件和版本继续保留并计入存储。如需释放空间，请前往文件管理彻底删除。', '下架页面', { type: 'warning' }); await api('/pages/' + page.id, json('DELETE')); await refresh(); ElMessage.success('页面已下架') })
}
function manageFiles(pageId = '') { versionsDialog.value = false; void navigate('files', pageId) }
async function deleteVersion(version: any) {
  await attempt(async () => {
    await ElMessageBox.confirm(`将永久删除 V${version.version_no} 的 ${version.file_count} 个文件，预计释放 ${bytes(version.size_bytes)}。此操作无法撤销。`, '彻底删除历史版本', { type: 'warning', confirmButtonText: '彻底删除', cancelButtonText: '取消' })
    await api('/files/versions/delete', json('POST', { ids: [version.id] }))
    await showVersions(versionPage.value); await refresh(); ElMessage.success('清理任务已提交，完成后释放空间')
  })
}
function editPage(page: any) { pageEditing.value = page; Object.assign(pageForm, { title: page.title, menu_id: page.menu_id, target_menu_id: '' }); pageDialog.value = true }
async function savePage() {
  await attempt(async () => {
    const target = pageMenuOptions.value.find(menu => menu.id === pageForm.target_menu_id)
    let confirmHidden = false
    const confirmHiddenMove = () => ElMessageBox.confirm('目标菜单或其上级菜单已隐藏。移动后，此页面会从工作空间导航中隐藏；页面和文件不会删除。仍要移动吗？', '确认移动到隐藏菜单', { type: 'warning', confirmButtonText: '仍要移动', cancelButtonText: '取消' })
    if (target && !menuIsVisible(target.id)) {
      await confirmHiddenMove()
      confirmHidden = true
    }
    const payload: Record<string, unknown> = { title: pageForm.title }
    if (target) payload[target.group ? 'parent_menu_id' : 'menu_id'] = target.id
    if (confirmHidden) payload.confirm_hidden = true
    try { await api('/pages/' + pageEditing.value.id, json('PATCH', payload)) }
    catch (e: any) {
      if (confirmHidden || !e.message?.includes('confirm_hidden=true')) throw e
      await confirmHiddenMove()
      await api('/pages/' + pageEditing.value.id, json('PATCH', { ...payload, confirm_hidden: true }))
    }
    pageDialog.value = false; await refresh()
  })
}
function editMenu(menu?: any) { menuEditing.value = menu; Object.assign(menuForm, { level: 1, name: menu?.name || '', parent_id: menu?.parent_id || '', position: menu?.position || 0, visible: menu?.visible ?? true }); menuDialog.value = true }
function setNewMenuLevel(level: number) { menuForm.level = level; menuForm.parent_id = '' }
async function saveMenu() {
  await attempt(async () => {
    if (menuEditing.value) {
      const parentId = menuForm.parent_id || null
      const movingToHiddenParent = parentId !== menuEditing.value.parent_id && menuIsVisible(menuEditing.value.id) && !menuIsVisible(parentId)
      let confirmHidden = false
      if (movingToHiddenParent) {
        await ElMessageBox.confirm('目标菜单或其上级菜单已隐藏。移动后，此菜单及其下属页面会从工作空间导航中隐藏；文件和页面数据不会删除。仍要移动吗？', '确认移动到隐藏菜单', { type: 'warning', confirmButtonText: '仍要移动', cancelButtonText: '取消' })
        confirmHidden = true
      }
      await api('/menus/' + menuEditing.value.id, json('PATCH', { name: menuForm.name, parent_id: parentId, position: menuForm.position, visible: menuForm.visible, confirm_hidden: confirmHidden }))
    } else {
      const name = menuForm.name.trim()
      if (!name) throw new Error('菜单名称不能为空')
      if (name.length > 60) throw new Error('菜单名称不能超过 60 个字符')
      const parentId = menuForm.level === 1 ? null : menuForm.parent_id
      const parent = parentId ? menus.value.find(menu => menu.id === parentId) : null
      if (menuForm.level > 1 && (!parent || menuDepth(parent.id) !== menuForm.level - 1))
        throw new Error(`请选择一个现有的${menuForm.level === 2 ? '一级' : '二级'}菜单`)
      if (parent && Number(parent.page_count)) throw new Error('已挂接页面的菜单不能作为上级')
      if (menus.value.some(menu => menu.parent_id === parentId && menu.name === name))
        throw new Error('同级菜单已存在此名称，请直接使用现有菜单或修改名称')
      if (parent && !menuIsVisible(parent.id))
        await ElMessageBox.confirm('所选上级菜单或其祖先已隐藏，新菜单也会在工作空间导航中隐藏。仍要创建吗？', '确认在隐藏菜单下创建', { type: 'warning', confirmButtonText: '仍要创建', cancelButtonText: '取消' })
      const created = await api<{ id: string }>('/menus', json('POST', { path: [...(parent ? menuPath(parent.id) : []), name] }))
      const position = Math.max(-1, ...menus.value.filter(menu => menu.parent_id === parentId).map(menu => Number(menu.position))) + 1
      await api(`/menus/${created.id}`, json('PATCH', { position }))
    }
    menuDialog.value = false; await refresh(); ElMessage.success('菜单已保存')
  })
}
async function openShare(menu: any, pageId = '') {
  if (!menu?.id) { ElMessage.warning('此页面没有挂接菜单，暂不能分享'); return }
  shareMenu.value = menu; shareDialog.value = true; shareBusy.value = true
  try {
    const result = await api('/menus/' + menu.id + '/shares')
    sharePages.value = result.pages; shares.value = result.shares
    sharePageId.value = result.pages.find((page: any) => page.id === pageId)?.id || result.pages[0]?.id || ''
    shareMode.value = 'link'; shareRecipientId.value = ''; shareRecipientQuery.value = ''; shareRecipients.value = []
  } catch (e: any) { shareDialog.value = false; ElMessage.error(e.message || '加载分享失败') }
  finally { shareBusy.value = false }
}
async function searchShareRecipients(query: string) {
  const q = query.trim()
  shareRecipientQuery.value = q
  shareRecipientId.value = ''
  shareRecipients.value = []
  if (q.length < 2) return
  await attempt(async () => {
    const matches = await api('/share-recipients', json('POST', { q }))
    if (shareRecipientQuery.value === q) shareRecipients.value = matches
  })
}
async function createShare() {
  await attempt(async () => {
    if (shareMode.value === 'account') {
      const row = await api('/menus/' + shareMenu.value.id + '/account-shares', json('POST', { page_id: sharePageId.value, recipient_user_id: shareRecipientId.value, days: shareDays.value }))
      myShares.value = await api('/shares')
      ElMessage.success('已分享给 ' + (row.recipient_name || row.recipient_username))
    } else {
      const row = await api('/menus/' + shareMenu.value.id + '/shares', json('POST', { page_id: sharePageId.value, days: shareDays.value }))
      shares.value.unshift(row); await copy(row.url); ElMessage.success('分享链接已创建并复制')
      myShares.value = await api('/shares')
    }
  })
}
async function revokeShare(row: any) {
  await attempt(async () => {
    await ElMessageBox.confirm('撤销后接收方将立即无法访问。', '撤销分享')
    await api(row.share_type === 'account' ? '/shares/accounts/' + row.id : '/shares/' + row.id, json('DELETE'))
    if (row.share_type !== 'account') shares.value = shares.value.map(share => share.id === row.id ? { ...share, revoked_at: new Date().toISOString(), url: null } : share)
    myShares.value = await api('/shares')
  })
}
async function deleteShare(row: any) {
  await attempt(async () => {
    await ElMessageBox.confirm('删除后该记录将从列表移除，原分享无法再访问，原页面和文件会保留。', '删除分享', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
    await api('/shares/' + row.share_type + '/' + row.id + '/record', json('DELETE'))
    myShares.value = myShares.value.filter(share => share.id !== row.id || share.share_type !== row.share_type)
    ElMessage.success('分享记录已删除')
  })
}
function receivedStatus(row: any) {
  return row.revoked_at ? '已撤销' : row.expires_at && new Date(row.expires_at) <= new Date() ? '已过期' : row.available ? row.expires_at ? '有效' : '永久有效' : '页面不可用'
}
async function removeReceivedShare(row: any) {
  await attempt(async () => {
    await ElMessageBox.confirm('移除后此账号将无法再打开该页面。', '移除分享')
    await api('/shares/received/' + row.id, json('DELETE'))
    receivedShares.value = receivedShares.value.filter((item: any) => item.id !== row.id)
  })
}
async function removeMenu(menu: any) { await attempt(async () => { await ElMessageBox.confirm('仅空菜单可以删除，确认继续？', '删除菜单'); await api('/menus/' + menu.id, json('DELETE')); await refresh() }) }
async function createCredential() {
  await attempt(async () => { const r = await api('/credentials', json('POST', credentialForm)); freshToken.value = r.token; freshCredentialId.value = r.id; credentials.value = await api('/credentials'); credentialForm.name = '' })
}
async function removeCredential(id: string) { await attempt(async () => { await ElMessageBox.confirm('删除后该凭证立即失效，凭证记录将被永久移除。此操作无法撤销。', '删除 MCP 凭证', { type: 'warning', confirmButtonText: '永久删除', cancelButtonText: '取消' }); await api('/credentials/' + id, json('DELETE')); if (freshCredentialId.value === id) { freshToken.value = ''; freshCredentialId.value = '' } credentials.value = await api('/credentials') }) }
async function revokeOAuthGrant(row: any) {
  await attempt(async () => {
    await ElMessageBox.confirm('撤销后，此应用的 access token 和 refresh token 会立即失效。', '撤销 OAuth 授权', { type: 'warning', confirmButtonText: '撤销授权', cancelButtonText: '取消' })
    await api('/oauth/grants/' + row.id, json('DELETE'))
    oauthGrants.value = await api('/oauth/grants')
  })
}
async function changePassword() {
  await attempt(async () => { await api('/auth/password', json('POST', passwordForm)); passwordDialog.value = false; user.value.must_change_password = false; passwordForm.old_password = ''; passwordForm.new_password = ''; await refresh(); ElMessage.success('密码已修改，原 MCP 凭证已撤销') })
}
async function copy(value: string) {
  await attempt(async () => {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value)
    else {
      const input = document.createElement('textarea')
      input.value = value; input.setAttribute('readonly', ''); input.style.position = 'fixed'; input.style.opacity = '0'
      document.body.appendChild(input); input.select()
      const copied = document.execCommand('copy')
      input.remove()
      if (!copied) throw new Error('复制失败，请手动复制文本框内容')
    }
    ElMessage.success('已复制')
  })
}
onMounted(async () => {
  window.addEventListener('popstate', restoreRoute)
  document.addEventListener('visibilitychange', presenceVisibilityChanged)
  try { await authenticated(await api('/auth/me')) } catch { user.value = null }
  ready.value = true
  timer = setInterval(() => { if (user.value && !user.value.must_change_password && uploads.value.some(u => ['queued', 'processing'].includes(u.status))) void attempt(refresh) }, 2500)
  menuSyncTimer = setInterval(syncMenus, 15000)
  presenceTimer = setInterval(sendPresenceHeartbeat, 60000)
})
onUnmounted(() => { if (timer) clearInterval(timer); if (presenceTimer) clearInterval(presenceTimer); if (menuSyncTimer) clearInterval(menuSyncTimer); if (pageSearchTimer) clearTimeout(pageSearchTimer); document.removeEventListener('visibilitychange', presenceVisibilityChanged); window.removeEventListener('popstate', restoreRoute) })
</script>

<template>
  <div v-if="!ready" class="loading">正在连接工作空间…</div>
  <main v-else-if="!user" class="auth-shell">
    <section class="auth-story">
      <div class="brand"><img class="brand-symbol" src="/pagehub.svg" alt="如" /><span class="brand-wordmark">PAGE HUB</span></div>
      <div class="story-content"><span class="eyebrow">A HOME FOR YOUR IDEAS</span><h1>让每一个灵感，<br>都有自己的位置。</h1><p>把智能体生成的页面，收进有序的工作空间。<br>上传、归类、分享访问，一处完成。</p>
        <div class="story-card"><el-icon :size="24"><Document /></el-icon><div><b>从一个 HTML 开始</b><span>你的工具、报表与创意，都能在这里落地</span></div><el-icon><ArrowRight /></el-icon></div>
      </div><small>统一页面 · 独立空间 · 智能体直连</small>
    </section>
    <section class="auth-panel"><div class="auth-form"><span class="eyebrow">YOUR WORKSPACE</span><h2>{{ authTab === 'login' ? '欢迎回来' : '创建你的工作空间' }}</h2><p class="muted">{{ authTab === 'login' ? '登录后，继续管理你的页面与工具。' : '填写账号信息并完成验证码，即可创建独立工作空间。' }}</p>
      <el-alert v-if="oauthRequest" title="智能体正在请求 OAuth 授权。登录后可查看它申请的权限和有效期；你也可以取消本次授权。" type="info" :closable="false" show-icon />
      <el-tabs v-model="authTab"><el-tab-pane label="登录" name="login" /><el-tab-pane label="注册" name="register" /></el-tabs>
      <el-form ref="authForm" :model="auth" :rules="authRules" label-position="top" @submit.prevent="submitAuth">
        <el-form-item v-if="authTab === 'login'" key="login-account" label="用户名 / 手机号" prop="account"><el-input v-model="auth.account" size="large" autocomplete="username" placeholder="请输入账号" /></el-form-item>
        <template v-else><el-form-item key="register-username" label="用户名" prop="username"><el-input v-model="auth.username" size="large" placeholder="3–40 位汉字、字母、数字或下划线" autocomplete="username" /></el-form-item>
          <el-form-item key="register-full-name" label="姓名" prop="full_name"><el-input v-model="auth.full_name" size="large" maxlength="60" autocomplete="name" placeholder="请输入姓名" /></el-form-item>
          <el-form-item label="手机号" prop="phone"><el-input v-model="auth.phone" size="large" placeholder="中国大陆手机号" autocomplete="tel" aria-required="true" /></el-form-item>
          <el-form-item label="短信验证码" prop="code"><div class="inline"><el-input v-model="auth.code" size="large" placeholder="6 位验证码" /><el-button size="large" @click="sendCode">获取验证码</el-button></div></el-form-item>
          <el-alert v-if="developmentCode" :title="developmentCodeLabel + '：' + developmentCode" type="warning" :closable="false" show-icon />
        </template>
        <el-form-item label="密码" prop="password"><el-input v-model="auth.password" type="password" show-password size="large" :autocomplete="authTab === 'login' ? 'current-password' : 'new-password'" placeholder="至少 10 位字符" /></el-form-item>
        <el-form-item v-if="authTab === 'register'" label="确认密码" prop="confirm"><el-input v-model="auth.confirm" type="password" show-password size="large" autocomplete="new-password" placeholder="再次输入密码" /></el-form-item>
        <el-button type="primary" size="large" class="wide" native-type="submit" :loading="busy">{{ authTab === 'login' ? '登录工作空间' : '注册并立即使用' }} <el-icon class="button-arrow"><ArrowRight /></el-icon></el-button>
      </el-form><div class="inline"><p class="auth-note">每个账号拥有独立租户，页面与数据彼此隔离。</p><el-button v-if="oauthRequest" text @click="denyOAuth">取消授权</el-button></div>
    </div></section>
  </main>

  <main v-else-if="oauthRequest" class="auth-shell oauth-shell">
    <section class="auth-story">
      <div class="brand"><img class="brand-symbol" src="/pagehub.svg" alt="如" /><span class="brand-wordmark">PAGE HUB</span></div>
      <div class="story-content"><span class="eyebrow">SECURE MCP AUTHORIZATION</span><h1>让智能体按需访问，<br>授权清晰可控。</h1><p>OAuth 2.1 授权码 + PKCE。访问令牌短期有效，客户端可自动续期。</p>
        <div class="story-card"><el-icon :size="24"><Connection /></el-icon><div><b>你始终可以撤销授权</b><span>在 MCP 接入页查看应用并随时终止访问</span></div><el-icon><ArrowRight /></el-icon></div>
      </div><small>PageHub · MCP OAuth 2.1</small>
    </section>
    <section class="auth-panel"><div class="auth-form"><span class="eyebrow">OAUTH CONSENT</span><h2>授权智能体访问 PageHub</h2>
      <el-alert v-if="oauthError" :title="oauthError" type="error" :closable="false" show-icon />
      <el-alert v-else-if="user.must_change_password" title="请先完成密码修改，再继续 OAuth 授权。" type="warning" :closable="false" show-icon />
      <el-skeleton v-else-if="oauthBusy || !oauthInfo" :rows="4" animated />
      <template v-else>
        <div class="oauth-client"><span class="document-icon"><el-icon :size="22"><Connection /></el-icon></span><div><b>{{ oauthInfo.client_name }}</b><small>当前账号：{{ oauthInfo.account }}</small></div></div>
        <div class="oauth-permissions"><b>申请权限</b><div v-for="scope in oauthInfo.scopes" :key="scope"><el-tag size="small" type="success">{{ scope === 'read' ? '读取' : '写入' }}</el-tag><span>{{ scope === 'read' ? '读取菜单、页面和上传状态' : '上传页面、创建菜单及管理收藏' }}</span></div></div>
        <el-form label-position="top"><el-form-item label="授权有效期"><el-select v-model="oauthExpiry" style="width:100%"><el-option :value="7" label="7 天"/><el-option :value="30" label="30 天"/><el-option :value="90" label="90 天（默认）"/><el-option :value="180" label="180 天"/><el-option :value="365" label="1 年"/><el-option :value="null" label="永久，直到手动撤销或账号停用"/></el-select></el-form-item></el-form>
        <el-alert title="Access token 每小时更新。兼容的 MCP 客户端会自动使用刷新令牌续期并保存轮换后的令牌，模型无需接触刷新令牌。" type="info" :closable="false" />
      </template>
      <div class="inline oauth-actions"><el-button @click="denyOAuth">拒绝</el-button><el-button v-if="oauthInfo && !oauthError && !user.must_change_password" type="primary" :loading="oauthBusy" @click="approveOAuth">同意并连接</el-button></div>
    </div></section>
  </main>

  <div v-else class="workspace" :class="{ 'top-layout': topLayout, 'flat-layout': flatLayout, 'admin-layout': section === 'admin', 'preview-layout': viewer && section !== 'admin', 'sidebar-hidden': sideLayout && sidebarHidden }">
    <header class="topbar">
      <button class="brand" aria-label="PageHub 首页" @click="navigate('overview')"><img class="brand-symbol" src="/pagehub.svg" alt="如" /><span class="brand-wordmark">PAGE HUB</span></button>
      <el-button v-if="sideLayout" class="sidebar-toggle" text circle :icon="sidebarHidden ? Expand : Fold" :aria-label="sidebarHidden ? '展开左侧菜单' : '收起左侧菜单'" :title="sidebarHidden ? '展开左侧菜单' : '收起左侧菜单'" :aria-expanded="!sidebarHidden" aria-controls="tenant-menu" @click="sidebarHidden = !sidebarHidden" />
      <div class="breadcrumb">{{ workspace.tenant.name || '我的空间' }} <span>/</span> <b>{{ heading }}</b></div>
      <div class="topbar-actions">
        <el-button v-if="section === 'admin'" @click="navigate('overview')">返回我的空间</el-button>
        <el-button v-if="section !== 'admin'" circle :icon="Star" :type="section === 'bookmarks' ? 'primary' : 'default'" aria-label="网站收藏" title="网站收藏" :aria-current="section === 'bookmarks' ? 'page' : undefined" @click="navigate('bookmarks')" />
        <el-button v-if="section !== 'admin'" :icon="Connection" :type="section === 'mcp' ? 'primary' : 'default'" :aria-current="section === 'mcp' ? 'page' : undefined" @click="navigate('mcp')">MCP 接入</el-button>
        <el-popover v-if="section !== 'admin'" v-model:visible="workbenchOpen" trigger="click" placement="bottom-end" :width="210">
          <template #reference><el-button :icon="Setting" :aria-expanded="workbenchOpen" aria-haspopup="true">工作台设置</el-button></template>
          <nav class="main-nav" aria-label="工作台功能"><button v-for="n in nav" :key="n.id" :class="{ active: section === n.id }" :aria-current="section === n.id ? 'page' : undefined" @click="navigate(n.id)"><el-icon :size="18"><component :is="n.icon" /></el-icon>{{ n.title }}</button></nav>
        </el-popover>
        <el-button tag="a" href="/help/index.html" target="_blank" rel="noopener noreferrer" circle :icon="QuestionFilled" aria-label="使用帮助" title="使用帮助（新标签页打开）" />
        <el-button text circle :aria-label="user.theme === 'dark' ? '切换浅色主题' : '切换深色主题'" @click="savePreferences({ theme: user.theme === 'dark' ? 'light' : 'dark' })"><el-icon :size="19"><Sunny v-if="user.theme === 'dark'" /><Moon v-else /></el-icon></el-button>
        <el-dropdown trigger="click"><el-button class="account-button" text :icon="User" aria-label="账号菜单"><span class="account-name">{{ user.username }}</span></el-button><template #dropdown><el-dropdown-menu><el-dropdown-item disabled><span class="remaining-storage">剩余 {{ bytes(workspace.storage.remaining_bytes) }}</span></el-dropdown-item><el-dropdown-item @click="navigate('settings')">账号与空间设置</el-dropdown-item><el-dropdown-item @click="passwordDialog = true">修改密码</el-dropdown-item><el-dropdown-item divided :icon="SwitchButton" @click="logout">退出登录</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
      </div>
    </header>
    <aside v-if="!flatLayout && section !== 'admin'" v-show="!sideLayout || !sidebarHidden" id="tenant-menu" class="sidebar" aria-label="租户菜单">
      <div v-click-outside="closeTopMenus" class="tenant-nav"><el-menu ref="tenantMenu" :key="workspace.tenant.layout" :mode="topLayout ? 'horizontal' : 'vertical'" :ellipsis="topLayout" :ellipsis-icon="moreMenuIcon" menu-trigger="click" :popper-class="topLayout ? 'tenant-top-menu-popover' : undefined"><MenuBranch v-for="(node, index) in menuTree" :key="node.id" :node="node" :position="index" :show-icons="sideLayout" @choose="filterMenu" /></el-menu><p v-if="!menuTree.length" class="empty-nav">上传页面后自动创建菜单</p></div>
    </aside>
    <div class="workspace-main">
      <main class="content">
        <AdminConsole v-if="section === 'admin'" :key="routeKey" />
        <template v-else>
        <Catalogue v-if="flatHome" :key="routeKey" :menus="menus" :state="catalogueState" :view="user.directory_view || 'compact'" :external="workspace.tenant.open_mode === 'external'" @choose="filterMenu" @open="openEntry" @share="openShare" @view="savePreferences({ directory_view: $event })" @change="syncRoute(true)" />
        <BookmarkViewer v-else-if="viewer?.kind === 'bookmark'" :key="viewer.id" :bookmark="viewer" @back="flatLayout && section !== 'bookmarks' ? navigate('overview') : returnList()" />
        <template v-else-if="viewer"><div class="preview-toolbar"><h1 :title="viewer.title">{{ viewer.title }}</h1><div class="inline preview-actions"><el-button v-if="viewer.menu_id && viewer.kind !== 'account-share'" text size="small" @click="openShare({ id: viewer.menu_id, name: menuName(viewer.menu_id) }, viewer.id)" :icon="Share">分享</el-button><el-button text size="small" @click="refreshPreview" :icon="Refresh" :title="viewer.kind === 'account-share' ? '重新检查分享权限' : '重新获取页面访问授权，有效期 5 分钟'">{{ viewer.kind === 'account-share' ? '刷新' : '刷新授权' }}</el-button><el-button text size="small" @click="viewer.kind === 'account-share' ? returnList() : flatLayout ? navigate('overview') : returnList()">{{ flatLayout && viewer.kind !== 'account-share' ? '返回菜单' : '返回列表' }}</el-button></div></div><iframe class="page-viewer" :src="preview" sandbox="allow-scripts" referrerpolicy="no-referrer" :title="viewer.title" /></template>
        <template v-else>
          <div class="page-heading"><h1>{{ heading }}</h1><div class="inline heading-actions"><el-button v-if="flatLayout" text size="small" @click="navigate('overview')">返回菜单</el-button><el-button v-if="['overview','pages'].includes(section)" type="primary" size="small" :icon="Plus" @click="showUpload()">上传页面</el-button><el-button v-else text size="small" :icon="Refresh" @click="attempt(refresh)">刷新</el-button></div></div>
          <template v-if="section === 'overview'">
            <div class="stat-grid"><article class="stat-card"><span>已发布页面</span><strong>{{ workspace.stats.pages || 0 }}<small>个</small></strong><div>集中管理你的所有创作</div></article><article class="stat-card"><span>累计上传</span><strong>{{ workspace.stats.uploads || 0 }}<small>次</small></strong><div>包含页面更新与新版本</div></article><article class="stat-card"><span>历史版本</span><strong>{{ workspace.stats.versions || 0 }}<small>个</small></strong><div>每次更新，都有迹可循</div></article><article class="stat-card"><span>存储用量</span><strong class="storage-stat">{{ bytes(workspace.stats.storage_bytes) }}</strong><div>配额 {{ bytes(workspace.tenant.quota_bytes) }}</div></article></div>
            <div class="quick-start"><div><span class="eyebrow">FROM IDEA TO PAGE</span><h2>你的下一个工具，从这里开始。</h2><p>上传 HTML 或 ZIP，自动发布到菜单；也可以让智能体通过 MCP 完成。</p><div class="inline"><el-button type="primary" @click="showUpload()" :icon="UploadFilled">上传第一个页面</el-button><el-button @click="navigate('mcp')">连接智能体 <el-icon><ArrowRight /></el-icon></el-button></div></div><div class="code-art" aria-hidden="true"><span>&lt;html&gt;</span><b>每个想法<br>都有一页</b><span>&lt;/html&gt;</span></div></div>
            <div class="section-heading"><h2>最近页面 <small>{{ pageTotal }}</small></h2><el-button text @click="navigate('pages')">查看全部 <el-icon><ArrowRight /></el-icon></el-button></div>
          </template>
          <BookmarkManager v-if="section === 'bookmarks'" :key="section + routeKey" :state="bookmarkState" @open="openEntry" @changed="attempt(refresh)" @change="syncRoute(true)" />
          <template v-if="['overview','pages'].includes(section)">
            <el-alert v-if="pageError" :title="pageError" type="error" :closable="false"><el-button text @click="fetchPages">重试</el-button></el-alert>
            <div v-if="section === 'pages'" class="list-toolbar"><el-input v-model="query" :prefix-icon="Search" placeholder="搜索页面名称" clearable style="max-width: 320px" /><el-tag v-if="selectedMenu" :closable="!flatLayout" @close="selectedMenu = ''">{{ menuName(selectedMenu) }}</el-tag><span class="muted">共 {{ pageTotal }} 个页面</span></div>
            <TransitionGroup v-if="pages.length" v-loading="pageLoading" tag="div" name="card-swap" class="page-grid"><article v-for="p in (section === 'overview' ? pages.slice(0,6) : pages)" :key="p.id" class="page-card" :data-tone="cardTone(p.id)" draggable="true" title="拖动卡片调整顺序" @dragstart="draggedPageId = p.id" @dragend="draggedPageId = ''" @dragover.prevent @drop.prevent="movePageCard(p.id)"><div class="page-card-top"><span class="document-icon"><el-icon :size="25"><Document /></el-icon></span><el-tag size="small" type="success" effect="plain">已发布</el-tag></div><button class="page-title" @click="openPage(p)">{{ p.title }}</button><p class="page-category">{{ menuName(p.menu_id) }}</p><div class="page-meta"><span>{{ bytes(p.size_bytes) }}</span><span>{{ date(p.created_at).split(' ')[0] }}</span></div><div class="card-actions"><el-button type="primary" text @click="openPage(p)">打开页面 <el-icon><ArrowRight /></el-icon></el-button><el-button v-if="p.menu_id" text @click="openShare({ id: p.menu_id, name: menuName(p.menu_id) }, p.id)" :icon="Share">分享</el-button><el-dropdown trigger="click"><el-button text aria-label="页面操作">•••</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item @click="editPage(p)">修改信息</el-dropdown-item><el-dropdown-item @click="showUpload(p)">上传新版本</el-dropdown-item><el-dropdown-item @click="showVersions(p)">版本记录</el-dropdown-item><el-dropdown-item @click="manageFiles(p.id)">管理文件</el-dropdown-item><el-dropdown-item @click="copy(platformOrigin + '/?page=' + p.id)">复制访问链接</el-dropdown-item><el-dropdown-item divided @click="removePage(p)">下架页面</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></article></TransitionGroup>
            <el-skeleton v-else-if="pageLoading" :rows="4" animated /><div v-else-if="!pageError" class="empty-panel"><div class="empty-icon"><el-icon :size="35"><FolderOpened /></el-icon></div><h3>{{ query || selectedMenu ? '还没有匹配的页面' : '空间已就绪，等待你的第一个页面' }}</h3><p class="muted">支持 HTML 与 ZIP，菜单将随上传自动创建。</p><el-button type="primary" plain @click="showUpload()">选择文件上传</el-button></div>
          </template>
          <el-pagination v-if="section === 'pages' && pageTotal > 50" class="list-pagination" background layout="total, prev, pager, next" :total="pageTotal" :page-size="50" :current-page="pageIndex" @current-change="changePage" />
          <MenuManager v-if="section === 'menus'" :menus="menus" @edit="editMenu" @remove="removeMenu" @changed="refresh" />
          <UploadHistory v-if="section === 'uploads'" @files="manageFiles" />
          <FileManager v-if="section === 'files'" :page-id="filePage" :refresh-key="workspace" @changed="refresh" @focus="filePage = $event; syncRoute(true)" />
          <section v-if="section === 'shares'" class="panel"><el-table :data="myShares" row-key="id" empty-text="你还没有创建分享"><el-table-column prop="title" label="页面" min-width="150"/><el-table-column prop="menu_name" label="菜单" min-width="100"/><el-table-column label="分享方式" width="105"><template #default="{ row }">{{ row.share_type === 'account' ? '账号分享' : '公开链接' }}</template></el-table-column><el-table-column label="接收账号" min-width="150"><template #default="{ row }">{{ row.share_type === 'account' ? (row.recipient_name || row.recipient_username) + '（' + row.recipient_username + '）' : '持有链接者' }}</template></el-table-column><el-table-column label="分享时间" min-width="165"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="有效期" min-width="130"><template #default="{ row }">{{ row.expires_at ? date(row.expires_at) : '永久' }}</template></el-table-column><el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="['有效','永久有效'].includes(row.status) ? 'success' : row.status === '已过期' ? 'warning' : row.status === '页面不可用' ? 'danger' : 'info'">{{ row.status }}</el-tag></template></el-table-column><el-table-column label="操作" width="220" fixed="right"><template #default="{ row }"><el-button v-if="row.url" text @click="copy(row.url)">复制链接</el-button><el-button v-if="['有效','永久有效'].includes(row.status)" text type="danger" @click="revokeShare(row)">撤销</el-button><el-button text type="danger" @click="deleteShare(row)">删除</el-button></template></el-table-column></el-table></section>
          <section v-if="section === 'received-shares'" class="panel"><el-table :data="receivedShares" row-key="id" empty-text="还没有收到页面分享"><el-table-column prop="title" label="页面" min-width="170"/><el-table-column label="分享人" min-width="160"><template #default="{ row }">{{ row.sender_name || row.sender_username }}（{{ row.sender_username }}）</template></el-table-column><el-table-column label="分享时间" min-width="170"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="有效期" min-width="135"><template #default="{ row }">{{ row.expires_at ? date(row.expires_at) : '永久' }}</template></el-table-column><el-table-column label="状态" width="120"><template #default="{ row }"><el-tag :type="['有效','永久有效'].includes(receivedStatus(row)) ? 'success' : receivedStatus(row) === '已过期' ? 'warning' : 'info'">{{ receivedStatus(row) }}</el-tag></template></el-table-column><el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-button text type="primary" :disabled="receivedStatus(row) !== '有效' && receivedStatus(row) !== '永久有效'" @click="openReceivedShare(row)">打开</el-button><el-button text @click="removeReceivedShare(row)">移除</el-button></template></el-table-column></el-table></section>
          <section v-if="section === 'mcp'" class="panel">
            <h2>连接你的智能体</h2><p class="muted">手动凭证适用于直接配置 Bearer token 的客户端；OAuth 适用于支持 MCP OAuth 的客户端。</p>
            <el-input class="mcp-agent-prompt" type="textarea" :model-value="mcpAgentPrompt" :autosize="{ minRows: 18, maxRows: 32 }" readonly aria-label="智能体操作说明与 MCP 配置" />
            <el-alert v-if="freshToken" class="mcp-token-warning" title="内容包含 MCP 凭证，请只发送给可信的智能体。刷新页面后需重新创建凭证。" type="warning" :closable="false" />
            <el-form inline>
              <el-form-item label="凭证名称" required><el-input v-model="credentialForm.name" placeholder="例如：我的智能体" /></el-form-item>
              <el-form-item><el-checkbox v-model="credentialForm.readonly">只读权限</el-checkbox></el-form-item>
              <el-form-item label="有效期"><el-select v-model="credentialForm.expires_in_days" style="width:115px"><el-option :value="7" label="7 天"/><el-option :value="30" label="30 天"/><el-option :value="90" label="90 天"/><el-option :value="180" label="180 天"/><el-option :value="365" label="1 年"/><el-option :value="null" label="永久"/></el-select></el-form-item>
              <el-form-item><el-button type="primary" @click="createCredential">创建凭证</el-button></el-form-item>
              <el-form-item><el-button :disabled="!freshToken" @click="copy(mcpAgentPrompt)">复制给智能体</el-button></el-form-item>
            </el-form>
            <el-table :data="credentials"><el-table-column prop="name" label="名称"/><el-table-column label="权限"><template #default="{ row }">{{ row.scopes.includes('write') ? '读写' : '只读' }}</template></el-table-column><el-table-column label="到期时间"><template #default="{ row }">{{ row.expires_at ? date(row.expires_at) : '永久' }}</template></el-table-column><el-table-column label="状态"><template #default="{ row }"><el-tag :type="!row.active ? 'info' : row.expires_at && new Date(row.expires_at) <= new Date() ? 'warning' : 'success'">{{ !row.active ? '已撤销' : row.expires_at && new Date(row.expires_at) <= new Date() ? '已过期' : '启用' }}</el-tag></template></el-table-column><el-table-column label="操作"><template #default="{ row }"><el-button v-if="row.active" text type="danger" @click="removeCredential(row.id)">删除</el-button></template></el-table-column></el-table>
          </section>
          <section v-if="section === 'mcp'" class="panel oauth-grants-panel">
            <div class="section-heading"><div><h2>OAuth 2.1 授权</h2><p class="muted">客户端通过发现文档注册并请求授权；授权有效期由你在浏览器确认，refresh token 自动轮换。</p></div></div>
            <div class="oauth-discovery"><span>授权服务发现</span><code>{{ oauthMetadataUrl }}</code><span>保护资源元数据</span><code>{{ oauthResourceMetadataUrl }}</code><span>令牌及自动刷新端点</span><code>{{ oauthTokenUrl }}</code></div>
            <el-table :data="oauthGrants" empty-text="还没有 OAuth 客户端获得授权"><el-table-column prop="client_name" label="已授权应用" min-width="150"/><el-table-column label="权限" min-width="100"><template #default="{ row }">{{ row.scopes.includes('write') ? '读写' : '只读' }}</template></el-table-column><el-table-column label="授权时间" min-width="165"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="有效期至" min-width="165"><template #default="{ row }">{{ row.expires_at ? date(row.expires_at) : '永久' }}</template></el-table-column><el-table-column label="最近使用" min-width="165"><template #default="{ row }">{{ row.last_used_at ? date(row.last_used_at) : '尚未使用' }}</template></el-table-column><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'active' ? 'success' : row.status === 'expired' ? 'warning' : 'info'">{{ row.status === 'active' ? '有效' : row.status === 'expired' ? '已过期' : '已撤销' }}</el-tag></template></el-table-column><el-table-column label="操作" width="100"><template #default="{ row }"><el-button v-if="row.status === 'active'" text type="danger" @click="revokeOAuthGrant(row)">撤销</el-button></template></el-table-column></el-table>
          </section>
          <section v-if="section === 'settings'" class="panel settings-panel"><div class="setting-row"><div><b>账号存储空间</b><p class="muted">含所有保留版本；下架不释放空间，彻底删除完成后归还。</p><el-button text type="primary" @click="manageFiles()">管理文件 / 清理空间</el-button></div><div class="account-storage"><b>剩余 {{ bytes(workspace.storage.remaining_bytes) }}</b><p class="muted">已用 {{ bytes(workspace.storage.used_bytes) }} / 配额 {{ bytes(workspace.storage.quota_bytes) }}</p><el-progress :percentage="Math.min(100, Math.round(Number(workspace.storage.used_bytes || 0) / Number(workspace.storage.quota_bytes || 1) * 100))" :show-text="false" /></div></div><div class="setting-row"><div><b>导航布局</b><p class="muted">所有本租户用户共用此设置；平铺仅展示末级菜单名称，不展开二、三级，原有结构保留。</p></div><el-radio-group :model-value="workspace.tenant.layout" @change="savePreferences({ layout: $event })"><el-radio-button value="side">左侧导航</el-radio-button><el-radio-button value="top">顶部导航</el-radio-button><el-radio-button value="flat">平铺</el-radio-button></el-radio-group></div><div class="setting-row"><div><b>页面打开方式</b><p class="muted">本租户共用；框架外在新标签页独立打开，仍需访问授权。</p></div><el-radio-group :model-value="workspace.tenant.open_mode" @change="savePreferences({ open_mode: $event })"><el-radio-button value="embedded">框架内显示</el-radio-button><el-radio-button value="external">框架外打开</el-radio-button></el-radio-group></div><div v-if="workspace.tenant.open_mode === 'external'" class="setting-row">
            <div><b>框架外授权有效期</b><p class="muted">仅影响此账号之后打开的页面，默认 5 分钟，最长 24 小时。到期后请回到工作空间重新打开。</p></div>
            <div class="inline">
              <el-input-number v-model="externalPreviewMinutes" aria-label="框架外授权有效期（分钟）" :min="1" :max="1440" :step="1" :precision="0" />
              <span>分钟</span><el-button :disabled="externalPreviewMinutes == null || externalPreviewMinutes === workspace.external_preview_minutes" @click="savePreferences({ external_preview_minutes: externalPreviewMinutes })">保存</el-button>
            </div>
          </div><div class="setting-row"><div><b>界面主题</b><p class="muted">仅影响你的平台界面</p></div><el-radio-group :model-value="user.theme" @change="savePreferences({ theme: $event })"><el-radio-button value="light">浅色</el-radio-button><el-radio-button value="dark">深色</el-radio-button></el-radio-group></div><div class="setting-row"><div><b>账号安全</b><p class="muted">{{ user.phone || '平台管理员账号' }}</p></div><el-button @click="passwordDialog = true">修改密码</el-button></div></section>
        </template>
        </template>
        <footer v-if="section !== 'admin' && !viewer" class="footer">PAGE HUB <span>让创意有序，让工具常在。</span></footer>
      </main>
    </div>
  </div>

  <el-dialog v-model="uploadDialog" :title="uploadForm.page_id ? '上传新版本' : '发布一个新页面'" width="min(560px, 94vw)"><el-form label-position="top"><el-form-item label="HTML / HTM 或 ZIP 文件"><input :key="String(uploadDialog)" type="file" accept=".html,.htm,.zip" aria-label="选择页面文件" @change="chosenFile = ($event.target as HTMLInputElement).files?.[0]" /></el-form-item><el-form-item label="页面标题（可自动读取）"><el-input v-model="uploadForm.title" /></el-form-item><el-form-item label="菜单路径（最多三级，使用 / 分隔）"><el-input v-model="uploadForm.path" placeholder="业务工具 / 报表 / 月报" /></el-form-item><el-form-item label="ZIP 入口文件（可选）"><el-input v-model="uploadForm.entry" placeholder="默认 index.html，也可由 manifest.json 指定" /></el-form-item><el-alert :title="'单文件上限 ' + (workspace.limits.max_upload_mb || 20) + ' MB；只能挂接到空闲末级菜单。路径中缺少的菜单会自动创建；若目标菜单已有页面，请换一个菜单名称或先在菜单管理中创建菜单。'" type="info" :closable="false" /></el-form><template #footer><el-button @click="uploadDialog = false">取消</el-button><el-button type="primary" :loading="busy" @click="submitUpload">上传并发布</el-button></template></el-dialog>
  <el-dialog v-model="menuDialog" :title="menuEditing ? '编辑菜单' : '新增菜单'" width="min(480px, 94vw)">
    <el-form label-position="top">
      <template v-if="!menuEditing">
        <el-form-item label="菜单级别" required><el-radio-group :model-value="menuForm.level" @change="setNewMenuLevel"><el-radio-button :value="1">一级菜单</el-radio-button><el-radio-button :value="2">二级菜单</el-radio-button><el-radio-button :value="3">三级菜单</el-radio-button></el-radio-group></el-form-item>
        <el-form-item v-if="menuForm.level > 1" :label="menuForm.level === 2 ? '上级一级菜单' : '上级二级菜单'" required><el-select v-model="menuForm.parent_id" class="menu-parent-select" popper-class="menu-parent-select-popper" style="width:100%" aria-label="上级菜单" filterable :placeholder="`选择现有${menuForm.level === 2 ? '一级' : '二级'}菜单`"><el-option v-for="m in menuCreateParentOptions" :key="m.id" :label="menuName(m.id)" :value="m.id" :disabled="m.disabled"><div class="menu-parent-option" :data-menu-id="m.id" :data-depth="m.depth" :style="{ paddingLeft: `${(m.depth - 1) * 20}px` }"><span class="menu-parent-option-name">{{ menuName(m.id) }}</span><el-tag v-if="Number(m.page_count)" size="small" type="warning" effect="plain">已挂页面</el-tag><el-tag v-if="!menuIsVisible(m.id)" size="small" type="info" effect="plain">导航隐藏</el-tag><small v-if="m.reason">{{ m.reason }}</small></div></el-option></el-select></el-form-item>
        <el-form-item label="菜单名称" required><el-input v-model="menuForm.name" maxlength="60" show-word-limit placeholder="输入新菜单名称" @keyup.enter="saveMenu" /></el-form-item>
        <div class="menu-create-preview"><span>创建路径</span><b>{{ menuCreatePathPreview }}</b></div>
      </template>
      <template v-else>
        <el-form-item label="名称" required><el-input v-model="menuForm.name" maxlength="60" /></el-form-item>
        <el-form-item label="上级菜单"><el-select v-model="menuForm.parent_id" class="menu-parent-select" popper-class="menu-parent-select-popper" style="width:100%" aria-label="上级菜单" filterable><el-option label="无（设为一级菜单）" value="" /><el-option v-for="m in menuParentOptions" :key="m.id" :label="menuName(m.id)" :value="m.id" :disabled="m.disabled"><div class="menu-parent-option" :data-menu-id="m.id" :data-depth="m.depth" :style="{ paddingLeft: `${(m.depth - 1) * 20}px` }"><span class="menu-parent-option-name">{{ m.name }}</span><el-tag size="small" effect="plain">{{ ['','一级','二级','三级'][m.depth] }}</el-tag><el-tag v-if="Number(m.page_count)" size="small" type="warning" effect="plain">已挂 {{ m.page_count }} 页</el-tag><small v-if="m.reason">{{ m.reason }}</small></div></el-option></el-select></el-form-item>
        <p class="muted">菜单按层级缩进显示；父级可有多个子菜单，移动后最多三级。已挂页面的节点不能作为上级，请先将页面移到其他末级菜单。</p>
        <el-form-item label="排序值（越小越靠前）"><el-input-number v-model="menuForm.position" :precision="0" /></el-form-item>
        <el-form-item label="在导航中显示"><el-switch v-model="menuForm.visible" /></el-form-item>
      </template>
    </el-form>
    <template #footer><el-button @click="menuDialog = false">取消</el-button><el-button type="primary" @click="saveMenu">{{ menuEditing ? '保存' : '创建菜单' }}</el-button></template>
  </el-dialog>
  <el-dialog v-model="pageDialog" title="页面信息" width="min(480px, 94vw)"><el-form label-position="top"><el-form-item label="标题"><el-input v-model="pageForm.title" /></el-form-item><el-form-item label="当前菜单"><span>{{ menuName(pageForm.menu_id) }}</span></el-form-item><el-form-item label="移动到"><el-select v-model="pageForm.target_menu_id" filterable clearable placeholder="不移动，保留当前菜单" style="width:100%"><el-option v-for="m in pageMenuOptions" :key="m.id" :value="m.id" :label="menuName(m.id)"><div class="menu-parent-option" :data-menu-id="m.id" :data-depth="m.depth" :style="{ paddingLeft: `${(m.depth - 1) * 18}px` }"><span class="menu-parent-option-name">{{ m.name }}</span><el-tag size="small" effect="plain">{{ ['','一级','二级','三级'][m.depth] }}</el-tag><el-tag size="small" :type="m.group ? 'info' : 'success'" effect="plain">{{ m.group ? '移动当前菜单到其下' : '直接挂接' }}</el-tag><el-tag v-if="m.hidden" size="small" type="warning" effect="plain">导航隐藏</el-tag></div></el-option></el-select><small class="muted">选择有子菜单的节点，会将当前页面菜单移到该节点下；选择空闲末级菜单，则直接挂接到该菜单。</small></el-form-item></el-form><template #footer><el-button type="primary" @click="savePage">保存</el-button></template></el-dialog>
  <el-dialog v-model="shareDialog" :title="'分享：' + (shareMenu?.name || '')" width="min(680px, 94vw)" destroy-on-close>
    <el-skeleton v-if="shareBusy" :rows="3" animated />
    <template v-else>
      <el-radio-group v-model="shareMode" class="share-mode"><el-radio-button value="link">公开链接</el-radio-button><el-radio-button value="account">指定账号</el-radio-button></el-radio-group>
      <el-form inline class="share-form"><el-form-item label="页面"><el-select v-model="sharePageId" style="width:220px" :disabled="sharePages.length === 1"><el-option v-for="p in sharePages" :key="p.id" :label="p.title" :value="p.id" /></el-select></el-form-item><el-form-item v-if="shareMode === 'account'" label="接收账号"><el-select v-model="shareRecipientId" filterable remote clearable reserve-keyword :remote-method="searchShareRecipients" :loading="shareBusy" placeholder="用户名、姓名或完整手机号" style="width:240px"><el-option v-for="person in shareRecipients" :key="person.id" :label="`${person.full_name || person.username}（${person.username}）· ${person.tenant_name}`" :value="person.id" /></el-select></el-form-item><el-form-item label="有效期"><el-select v-model="shareDays" style="width:120px"><el-option :value="1" label="1 天"/><el-option :value="7" label="7 天"/><el-option :value="30" label="30 天"/><el-option :value="null" label="永久"/></el-select></el-form-item><el-form-item><el-button type="primary" :disabled="!sharePageId || (shareMode === 'account' && !shareRecipientId)" @click="createShare">{{ shareMode === 'account' ? '分享给账号' : '生成链接' }}</el-button></el-form-item></el-form>
      <el-alert v-if="shareMode === 'account'" title="接收人登录后可在“共享给我”查看；页面及其资源均按接收账号校验，不生成匿名链接。" type="info" :closable="false" />
      <el-table v-if="shareMode === 'link'" :data="shares" empty-text="暂无公开链接"><el-table-column prop="title" label="页面" min-width="130"/><el-table-column label="有效期至" width="170"><template #default="{ row }">{{ row.expires_at ? date(row.expires_at) : '永久' }}</template></el-table-column><el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.revoked_at ? 'info' : !row.available ? 'danger' : row.expires_at && new Date(row.expires_at) <= new Date() ? 'warning' : 'success'">{{ row.revoked_at ? '已撤销' : !row.available ? '页面不可用' : row.expires_at && new Date(row.expires_at) <= new Date() ? '已过期' : '有效' }}</el-tag></template></el-table-column><el-table-column label="操作" width="140"><template #default="{ row }"><el-button v-if="row.url" text @click="copy(row.url)">复制链接</el-button><el-button v-if="!row.revoked_at && row.available && (!row.expires_at || new Date(row.expires_at) > new Date())" text type="danger" @click="revokeShare({ ...row, share_type: 'link' })">撤销</el-button></template></el-table-column></el-table>
      <p v-if="shareMode === 'link'" class="muted">公开链接无需登录，任何持有链接的人都可预览；可随时撤销。</p>
    </template>
  </el-dialog>
  <el-dialog v-model="versionsDialog" title="版本记录" width="min(820px, 94vw)"><el-table :data="versions"><el-table-column label="版本" width="80"><template #default="{ row }">V{{ row.version_no }}</template></el-table-column><el-table-column label="上传时间" min-width="170"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column prop="username" label="上传者" min-width="110" /><el-table-column label="大小" width="110"><template #default="{ row }">{{ bytes(row.size_bytes) }}</template></el-table-column><el-table-column label="操作" min-width="210"><template #default="{ row }"><el-tag v-if="row.id === versionPage?.current_version" type="success">当前版本</el-tag><el-tag v-else-if="row.deletion_id" :type="row.deletion_status === 'failed' ? 'danger' : 'info'">{{ row.deletion_status === 'failed' ? '删除失败' : '清理已提交' }}</el-tag><template v-else><el-button text @click="rollback(row)">回滚至此版本</el-button><el-button text type="danger" @click="deleteVersion(row)">删除版本</el-button></template></template></el-table-column></el-table><template #footer><el-button @click="manageFiles(versionPage.id)">管理文件与清理任务</el-button></template></el-dialog>
  <el-dialog v-model="passwordDialog" title="修改密码" width="min(480px, 94vw)" :show-close="!user?.must_change_password" :close-on-click-modal="false" :close-on-press-escape="false"><el-alert v-if="user?.must_change_password" title="首次登录请先修改临时密码。" type="warning" :closable="false" /><el-form label-position="top"><el-form-item label="原密码"><el-input v-model="passwordForm.old_password" type="password" show-password /></el-form-item><el-form-item label="新密码（至少 10 位）"><el-input v-model="passwordForm.new_password" type="password" show-password /></el-form-item></el-form><template #footer><el-button type="primary" @click="changePassword">保存新密码</el-button></template></el-dialog>
</template>
