<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Grid, OfficeBuilding, User, UploadFilled, Monitor, Document, Search, Refresh, Plus, Menu, FolderOpened } from '@element-plus/icons-vue'
import { api, bytes, date, json } from './api'
import FileManager from './FileManager.vue'

const navigation = [
  { id: 'overview', name: '平台概览', icon: Grid }, { id: 'tenants', name: '租户管理', icon: OfficeBuilding },
  { id: 'users', name: '用户管理', icon: User }, { id: 'uploads', name: '上传任务', icon: UploadFilled },
  { id: 'resources', name: '资源监控', icon: Monitor }, { id: 'audit', name: '审计日志', icon: Document },
  { id: 'files', name: '文件管理', icon: FolderOpened },
]
const initial = new URLSearchParams(location.search).get('admin') || 'overview'
const section = ref(navigation.some(n => n.id === initial) ? initial : 'overview')
const filters = reactive<Record<string, any>>(Object.fromEntries(navigation.map(n => [n.id, { q: '', status: 'all', activity: 'all', tenant: '', page: 1, action: '', dates: [] }])))
const filter = computed(() => filters[section.value])
const title = computed(() => navigation.find(n => n.id === section.value)!.name)
const rows = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const fileRefresh = ref(0)
const error = ref('')
const overview = ref<any>({})
const userActivity = ref<any>({})
const selectedUsers = ref<any[]>([])
const resources = ref<any>({})
const resourceAutoRefresh = ref(false)
const resourceRefreshing = ref(false)
const failedDrawer = ref(false)
const failedUploads = ref<any[]>([])
const failedTotal = ref(0)
const failedPage = ref(1)
const failedLoading = ref(false)
const failedError = ref('')
const mobileNav = ref(false)
const tenantOptions = ref<any[]>([])
const detail = ref<any>(null)
const detailKind = ref('')
const recent = ref<any[]>([])
const detailError = ref('')
const editKind = ref('')
const editing = ref<any>(null)
const saving = ref(false)
const provisioned = ref('')
const form = reactive({ name: '', username: '', full_name: '', phone: '', tenant_id: '', quota_mb: 1024, active: true })
const statuses = computed(() => section.value === 'uploads'
  ? [{ value: 'pending', label: '待处理' }, { value: 'queued', label: '排队中' }, { value: 'processing', label: '处理中' }, { value: 'success', label: '已发布' }, { value: 'failed', label: '失败' }]
  : [{ value: 'active', label: '启用' }, { value: 'inactive', label: '停用' }, ...(section.value === 'tenants' ? [{ value: 'deleted', label: '已删除' }] : [{ value: 'near_quota', label: '配额接近上限' }])])
const uploadLabel: Record<string, string> = { queued: '排队中', processing: '处理中', success: '已发布', failed: '失败' }
let request = 0
let searchTimer: ReturnType<typeof setTimeout> | undefined
let optionsRequest = 0
let detailRequest = 0
let failedRequest = 0
let disposed = false
let activityTimer: ReturnType<typeof setInterval> | undefined
let resourceTimer: ReturnType<typeof setInterval> | undefined
let resourceRequestBusy = false
let lastStorageRefresh = 0
const usage = (row: any) => Math.min(100, Math.round(Number(row.storage_bytes || 0) / Number(row.quota_bytes || 1) * 100))
const usageColor = (row: any) => usage(row) >= 95 ? '#f87171' : usage(row) >= 80 ? '#fbbf24' : '#38bdf8'
const activityCards = computed(() => [
  { id: 'online', label: '当前在线', value: userActivity.value.online ?? 0, note: '5 分钟内有工作台心跳' },
  { id: 'active_7d', label: '近 7 天活跃', value: userActivity.value.active_7d ?? 0, note: '包含当前在线用户' },
  { id: 'inactive_8_30d', label: '8–30 天未活跃', value: userActivity.value.inactive_8_30d ?? 0, note: '可筛选查看账号' },
  { id: 'inactive_30d', label: '超过 30 天未活跃', value: userActivity.value.inactive_30d ?? 0, note: '可筛选后批量停用' },
  { id: 'never', label: '从未活跃', value: userActivity.value.never_active ?? 0, note: '账号创建后未登录' },
])

async function load() {
  if (section.value === 'files') { fileRefresh.value++; return }
  const serial = ++request
  loading.value = true; error.value = ''
  try {
    if (section.value === 'overview' || section.value === 'resources') {
      const result = await api('/admin/' + section.value)
      if (serial !== request || disposed) return
      if (section.value === 'overview') overview.value = result
      else { resources.value = { ...resources.value, ...result }; lastStorageRefresh = Date.now() }
    } else {
      const f = filter.value
      const params = new URLSearchParams({ q: f.q, status: f.status, page: String(f.page), size: '20', action: f.action })
      if (section.value === 'users') params.set('activity', f.activity)
      if (f.tenant) params.set('tenant', f.tenant)
      if (f.dates?.length === 2) { params.set('after', new Date(f.dates[0]).toISOString()); params.set('before', new Date(new Date(f.dates[1]).setHours(23, 59, 59, 999)).toISOString()) }
      const result = await api('/admin/records/' + section.value + '?' + params)
      if (serial !== request || disposed) return
      rows.value = result.items; total.value = result.total; f.page = result.page
      if (section.value === 'users') userActivity.value = await api('/admin/user-activity')
    }
  } catch (e: any) { if (serial === request && !disposed) error.value = e.message }
  finally { if (serial === request && !disposed) loading.value = false }
}
async function refreshResourcesAutomatically() {
  if (section.value !== 'resources' || document.visibilityState !== 'visible' || loading.value || resourceRequestBusy) return
  resourceRequestBusy = true; resourceRefreshing.value = true
  const includeStorage = Date.now() - lastStorageRefresh >= 30000
  try {
    const result = await api('/admin/resources?include_storage=' + includeStorage)
    if (section.value !== 'resources' || disposed) return
    resources.value = { ...resources.value, ...result }
    if (includeStorage) lastStorageRefresh = Date.now()
    error.value = ''
  } catch (e: any) { if (!disposed && section.value === 'resources') error.value = e.message }
  finally { resourceRequestBusy = false; resourceRefreshing.value = false }
}
async function showFailedUploads(page = 1) {
  const serial = ++failedRequest
  failedDrawer.value = true; failedLoading.value = true; failedError.value = ''
  try {
    const result = await api('/admin/records/uploads?' + new URLSearchParams({ status: 'failed', page: String(page), size: '20' }))
    if (serial !== failedRequest || disposed) return
    failedUploads.value = result.items; failedTotal.value = result.total; failedPage.value = result.page
  } catch (e: any) {
    if (serial === failedRequest && !disposed) failedError.value = e.message
  } finally {
    if (serial === failedRequest && !disposed) failedLoading.value = false
  }
}
function syncResourceRefresh() {
  if (resourceTimer) clearInterval(resourceTimer)
  resourceTimer = undefined
  if (resourceAutoRefresh.value && section.value === 'resources' && document.visibilityState === 'visible') {
    void refreshResourcesAutomatically()
    resourceTimer = setInterval(refreshResourcesAutomatically, 5000)
  }
}
function resourceVisibilityChanged() {
  if (document.visibilityState === 'visible') syncResourceRefresh()
  else if (resourceTimer) { clearInterval(resourceTimer); resourceTimer = undefined }
}
async function searchTenants(value = '', include = '') {
  const serial = ++optionsRequest
  try {
    const result = await api('/admin/tenant-options?' + new URLSearchParams({ q: value, ...(include ? { include } : {}) }))
    if (serial === optionsRequest && !disposed) tenantOptions.value = result
  } catch (e: any) { if (!disposed) ElMessage.error(e.message) }
}
function selectSection(value: string) {
  if (searchTimer) clearTimeout(searchTimer)
  section.value = value; mobileNav.value = false; rows.value = []; total.value = 0; selectedUsers.value = []
  const url = new URL(location.href); url.searchParams.set('admin', value); history.replaceState(null, '', url)
  void load()
}
function search() { filter.value.page = 1; selectedUsers.value = []; ++request; rows.value = []; loading.value = true; if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(load, 250) }
function paginate(page: number) { filter.value.page = page; void load() }
function drill(value: string, status = 'all') { filters[value].status = status; filters[value].page = 1; selectSection(value) }
function filterActivity(value: string) { filters.users.activity = filters.users.activity === value ? 'all' : value; filters.users.page = 1; selectSection('users') }
function setSelectedUsers(value: any[]) { selectedUsers.value = value }
function refreshVisibleUsers() { if (section.value === 'users' && document.visibilityState === 'visible') void load() }
async function deactivateSelected() {
  if (!selectedUsers.value.length) return
  try {
    const count = selectedUsers.value.length
    await ElMessageBox.confirm(`将停用选中的 ${count} 个普通用户，立即结束登录会话并停用 MCP 凭证；页面和文件会保留。`, '批量停用用户', { type: 'warning', confirmButtonText: '停用账号', cancelButtonText: '取消' })
    const result = await api('/admin/users/deactivate', json('POST', { user_ids: selectedUsers.value.map(row => row.id) }))
    selectedUsers.value = []
    ElMessage.success(`已停用 ${result.count} 个账号`)
    await load()
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message) }
}
async function showDetail(kind: string, row: any) {
  const serial = ++detailRequest
  detail.value = row; detailKind.value = kind; recent.value = []; detailError.value = ''
  if (kind === 'audit' || kind === 'uploads') return
  try {
    const param = kind === 'users' ? 'entity' : 'tenant'
    const result = await api('/admin/records/uploads?' + new URLSearchParams({ [param]: row.id, size: '5' }))
    if (serial === detailRequest && !disposed) recent.value = result.items
  } catch (e: any) { if (serial === detailRequest && !disposed) detailError.value = e.message }
}
function edit(kind: string, row?: any) {
  editing.value = row || null; provisioned.value = ''
  Object.assign(form, { name: row?.name || '', username: row?.username || '', full_name: row?.full_name || '', phone: row?.phone || '', tenant_id: row?.tenant_id || '', quota_mb: row ? Number(row.quota_bytes) / 1024 ** 2 : 1024, active: row?.active ?? true })
  editKind.value = kind
  if (kind === 'users') void searchTenants('', form.tenant_id)
}
async function save() {
  if (saving.value) return
  if (editKind.value === 'tenants' && !form.name.trim()) { ElMessage.warning('请输入租户名称'); return }
  if (editKind.value === 'users' && (!form.tenant_id || !form.full_name.trim() || (!editing.value && (!form.username.trim() || !form.phone.trim())))) { ElMessage.warning('请填写姓名、用户名、手机号并选择租户'); return }
  saving.value = true
  try {
    const kind = editKind.value
    const body = kind === 'tenants' ? { name: form.name.trim(), quota_mb: form.quota_mb, active: form.active } : { username: form.username.trim(), full_name: form.full_name.trim(), phone: form.phone.trim(), tenant_id: form.tenant_id, quota_mb: form.quota_mb, active: form.active }
    if (kind === 'users' && editing.value && (editing.value.tenant_id !== form.tenant_id || editing.value.active !== form.active)) await ElMessageBox.confirm('调整归属或状态后，此账号需重新登录，现有 MCP 凭证失效；原页面保留在原租户。', '确认账号调整', { type: 'warning' })
    const result = await api('/admin/' + kind + (editing.value ? '/' + editing.value.id : ''), json(editing.value ? 'PATCH' : 'POST', body))
    if (result.temporary_password) provisioned.value = `用户名：${result.username}\n临时密码：${result.temporary_password}\n访问地址：${result.url}\n请保存，仅展示一次。`
    else editKind.value = ''
    detail.value = null; await load(); ElMessage.success('设置已保存')
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message) }
  finally { saving.value = false }
}
async function removeTenant(row: any) {
  try {
    await ElMessageBox.confirm('租户将被逻辑删除，所有用户和 MCP 停止访问，已有数据继续保留。', '删除租户', { type: 'warning' })
    await api('/admin/tenants/' + row.id, json('DELETE')); detail.value = null; await load(); ElMessage.success('租户已删除，数据保留')
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message) }
}
onMounted(() => {
  void load()
  activityTimer = setInterval(refreshVisibleUsers, 60000)
  document.addEventListener('visibilitychange', refreshVisibleUsers)
  document.addEventListener('visibilitychange', resourceVisibilityChanged)
})
watch([resourceAutoRefresh, section], syncResourceRefresh)
onBeforeUnmount(() => { disposed = true; ++request; ++detailRequest; if (searchTimer) clearTimeout(searchTimer); if (activityTimer) clearInterval(activityTimer); if (resourceTimer) clearInterval(resourceTimer); document.removeEventListener('visibilitychange', refreshVisibleUsers); document.removeEventListener('visibilitychange', resourceVisibilityChanged) })
</script>

<template>
  <section class="console-layout">
    <aside class="console-sidebar"><span class="eyebrow">PLATFORM CONSOLE</span><h2>平台控制台</h2><nav aria-label="平台管理导航"><button v-for="item in navigation" :key="item.id" :class="{ active: section === item.id }" :aria-current="section === item.id ? 'page' : undefined" @click="selectSection(item.id)"><el-icon><component :is="item.icon" /></el-icon>{{ item.name }}</button></nav><div class="console-scope">平台超级管理员<span>跨租户操作均记录审计</span></div></aside>
    <main class="console-content">
      <div class="console-heading"><div><span class="eyebrow">{{ section === 'overview' ? 'A CLEAR VIEW OF YOUR PLATFORM' : 'PLATFORM MANAGEMENT' }}</span><h1>{{ title }}</h1><p class="muted">{{ section === 'overview' ? '掌握平台运行，关注需要处理的事项。' : '清晰管理每一项数据与操作。' }}</p></div><div class="inline"><el-button class="console-mobile-nav" :icon="Menu" aria-label="展开管理导航" @click="mobileNav = true" /><el-button :icon="Refresh" :loading="loading" @click="load">刷新</el-button><el-button v-if="section === 'resources'" :type="resourceAutoRefresh ? 'primary' : 'default'" :icon="Refresh" :loading="resourceRefreshing" :aria-pressed="resourceAutoRefresh" :title="resourceAutoRefresh ? '每 5 秒更新主机指标，每 30 秒更新数据库与存储统计' : '开启实时资源更新'" @click="resourceAutoRefresh = !resourceAutoRefresh">{{ resourceAutoRefresh ? '停止自动刷新' : '自动刷新' }}</el-button><el-button v-if="section === 'tenants' || section === 'users'" type="primary" :icon="Plus" @click="edit(section)">{{ section === 'tenants' ? '创建租户' : '创建用户' }}</el-button></div></div>
      <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button text @click="load">重试</el-button></el-alert>
      <div v-loading="loading" :aria-busy="loading">
        <template v-if="section === 'overview'">
          <div class="console-stat-grid"><button @click="drill('tenants')"><span>租户总数</span><strong>{{ overview.tenants ?? '—' }}</strong><small>查看租户 →</small></button><button @click="drill('users')"><span>注册用户</span><strong>{{ overview.users ?? '—' }}</strong><small>查看用户 →</small></button><article><span>已发布页面</span><strong>{{ overview.pages ?? '—' }}</strong><small>平台当前有效页面</small></article><article><span>版本存储用量</span><strong>{{ bytes(overview.storage_bytes) }}</strong><small>含保留的历史版本</small></article></div>
          <div class="console-overview-panels"><section class="panel"><h2>需要关注</h2><button class="attention-row" @click="drill('uploads', 'failed')"><span><i class="status-dot danger" />失败上传<small>查看处理原因</small></span><b>{{ overview.failed ?? 0 }} →</b></button><button class="attention-row" @click="drill('uploads', 'pending')"><span><i class="status-dot warning" />待处理任务<small>排队及处理中</small></span><b>{{ overview.pending ?? 0 }} →</b></button><button class="attention-row" @click="drill('users', 'near_quota')"><span><i class="status-dot" />账号配额接近上限<small>已使用 80% 及以上</small></span><b>{{ overview.near_quota ?? 0 }} →</b></button></section><section class="panel console-guide"><span class="eyebrow">CONNECTED WORKSPACES</span><h2>从整体，看到每一个空间。</h2><p class="muted">租户集中管理，账号用量独立统计。查看详情可了解上传、版本和存储情况。</p><el-button @click="selectSection('resources')">查看主机资源</el-button><p class="muted">账号用量按上传者累计；租户用量按当前文件归属统计。</p></section></div>
        </template>
        <template v-else-if="section === 'resources'">
          <p class="muted">{{ resources.scope }} · 主机采集于 {{ date(resources.sampled_at) }}<span v-if="resources.storage_sampled_at"> · 数据库与存储统计于 {{ date(resources.storage_sampled_at) }}</span><span v-if="resourceAutoRefresh"> · 自动刷新已开启（5 秒）</span></p>
          <div class="console-stat-grid"><article><span>CPU 使用率</span><strong>{{ resources.cpu_percent ?? '—' }}<small>%</small></strong><el-progress :percentage="Number(resources.cpu_percent || 0)" :show-text="false" /></article><article><span>内存使用率</span><strong>{{ resources.memory_percent ?? '—' }}<small>%</small></strong><el-progress :percentage="Number(resources.memory_percent || 0)" :show-text="false" /><small>{{ bytes(resources.memory_used) }} / {{ bytes(resources.memory_total) }}</small></article><article><span>磁盘已用</span><strong>{{ bytes(resources.disk_used) }}</strong><small>剩余 {{ bytes(resources.disk_free) }}</small></article><article><span>数据库大小</span><strong>{{ bytes(resources.database_bytes) }}</strong><small>PostgreSQL 物理用量</small></article></div>
          <div class="console-overview-panels"><section class="panel"><h2>上传处理队列</h2><div v-for="state in ['queued','processing','success','failed']" :key="state" class="metric-row"><button v-if="state === 'failed'" class="queue-failure-link" :disabled="!(resources.jobs?.find((j: any) => j.status === state)?.count || 0)" @click="showFailedUploads()">{{ uploadLabel[state] }}</button><span v-else>{{ uploadLabel[state] }}</span><b>{{ resources.jobs?.find((j: any) => j.status === state)?.count || 0 }}</b></div></section><section class="panel"><h2>页面版本存储</h2><strong class="resource-value">{{ bytes(resources.page_storage_bytes) }}</strong><p class="muted">{{ resources.storage_note }}</p><p class="muted">本页为即时采集数据。HTML 在用户浏览器中运行，浏览器 CPU 与内存不计入主机用户用量。</p></section></div>
        </template>
        <FileManager v-else-if="section === 'files'" platform :refresh-key="fileRefresh" />
        <template v-else>
          <div v-if="section === 'users'" class="console-user-activity">
            <button v-for="card in activityCards" :key="card.id" :class="{ active: filter.activity === card.id }" @click="filterActivity(card.id)">
              <span>{{ card.label }}</span><strong>{{ card.value }}</strong><small>{{ card.note }}</small>
            </button>
          </div>
          <section class="panel console-table-panel">
          <div class="console-filters">
            <el-input v-model="filter.q" :prefix-icon="Search" clearable :placeholder="section === 'users' ? '搜索用户名或手机号' : section === 'tenants' ? '搜索租户名称' : section === 'uploads' ? '搜索文件、用户名或手机号' : '搜索操作者、手机号或动作'" @input="search" />
            <el-select v-if="section === 'users'" v-model="filter.activity" aria-label="活跃情况筛选" @change="search">
              <el-option label="全部活跃情况" value="all" /><el-option label="当前在线" value="online" /><el-option label="近 7 天活跃" value="active_7d" />
              <el-option label="8–30 天未活跃" value="inactive_8_30d" /><el-option label="超过 30 天未活跃" value="inactive_30d" /><el-option label="从未活跃" value="never" />
            </el-select>
            <el-select v-if="section !== 'audit'" v-model="filter.status" :aria-label="section === 'users' ? '账号状态筛选' : '状态筛选'" @change="search"><el-option label="全部状态" value="all" /><el-option v-for="state in statuses" :key="state.value" :label="state.label" :value="state.value" /></el-select>
            <el-select v-if="section !== 'tenants'" v-model="filter.tenant" filterable remote clearable :remote-method="searchTenants" placeholder="全部租户" aria-label="筛选租户" @visible-change="(value: boolean) => value && searchTenants()" @change="search"><el-option v-for="tenant in tenantOptions" :key="tenant.id" :label="tenant.name" :value="tenant.id" /></el-select>
            <el-input v-if="section === 'audit'" v-model="filter.action" placeholder="操作类型，如 menu.update" clearable @input="search" />
            <el-date-picker v-if="section === 'audit'" v-model="filter.dates" type="daterange" start-placeholder="开始日期" end-placeholder="结束日期" @change="search" />
          </div>
          <div v-if="section === 'users' && selectedUsers.length" class="console-selection-bar"><span>已选 {{ selectedUsers.length }} 个账号</span><el-button type="danger" plain :loading="saving" @click="deactivateSelected">批量停用</el-button></div>
          <el-table :data="rows" row-key="id" empty-text="没有匹配的数据" @selection-change="setSelectedUsers">
            <el-table-column v-if="section === 'users'" type="selection" width="48" :selectable="(row: any) => row.role === 'user' && row.active" />
            <template v-if="section === 'tenants'">
              <el-table-column label="租户" min-width="190"><template #default="{ row }"><button class="table-name" @click="showDetail('tenants', row)">{{ row.name }}</button><small class="table-secondary">{{ date(row.created_at) }}</small></template></el-table-column><el-table-column prop="users" label="用户数" width="85" /><el-table-column prop="pages" label="页面数" width="85" />
            </template>
            <template v-if="section === 'users'">
              <el-table-column label="用户名" min-width="140"><template #default="{ row }"><button class="table-name" @click="showDetail('users', row)">{{ row.username }}</button></template></el-table-column><el-table-column prop="full_name" label="姓名" min-width="115"><template #default="{ row }">{{ row.full_name || '—' }}</template></el-table-column><el-table-column prop="phone" label="手机号" min-width="130"><template #default="{ row }">{{ row.phone || '—' }}</template></el-table-column><el-table-column prop="tenant_name" label="所属租户" min-width="150" /><el-table-column label="活跃状态" width="120"><template #default="{ row }"><el-tag v-if="row.online_sessions" type="success" size="small">在线 · {{ row.online_sessions }}</el-tag><el-tag v-else-if="!row.last_activity_at" type="info" size="small">从未活跃</el-tag><el-tag v-else type="info" size="small">离线</el-tag></template></el-table-column><el-table-column label="最近活动" min-width="165"><template #default="{ row }">{{ date(row.last_activity_at) }}</template></el-table-column><el-table-column prop="pages" label="页面数" width="85" />
            </template>
            <template v-if="section === 'tenants' || section === 'users'">
              <el-table-column label="存储用量 / 配额" min-width="210"><template #default="{ row }"><span class="quota-label">{{ bytes(row.storage_bytes) }} / {{ bytes(row.quota_bytes) }}</span><el-progress :percentage="usage(row)" :color="usageColor(row)" :show-text="false" :stroke-width="4" /><small class="table-secondary">剩余 {{ bytes(Math.max(0, Number(row.quota_bytes) - Number(row.storage_bytes))) }}</small></template></el-table-column><el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="row.active && !row.deleted_at ? 'success' : 'info'" size="small">{{ row.deleted_at ? '已删除' : row.active ? '启用' : '停用' }}</el-tag></template></el-table-column><el-table-column label="操作" width="145" fixed="right"><template #default="{ row }"><el-button text @click="showDetail(section, row)">详情</el-button><el-button v-if="!row.deleted_at && row.role !== 'superadmin'" text type="primary" @click="edit(section, row)">{{ section === 'users' ? '管理' : '编辑' }}</el-button></template></el-table-column>
            </template>
            <template v-if="section === 'uploads'">
              <el-table-column prop="filename" label="文件" min-width="180" /><el-table-column prop="username" label="用户名" min-width="135" /><el-table-column prop="phone" label="手机号" min-width="130" /><el-table-column prop="tenant_name" label="租户" min-width="150" /><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'success' ? 'success' : 'warning'" size="small">{{ uploadLabel[row.status] }}</el-tag></template></el-table-column><el-table-column label="提交时间" width="170"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="95" fixed="right"><template #default="{ row }"><el-button text @click="showDetail('uploads', row)">详情</el-button></template></el-table-column>
            </template>
            <template v-if="section === 'audit'">
              <el-table-column label="操作者" min-width="160"><template #default="{ row }">{{ row.full_name || row.username }}<small class="table-secondary">{{ row.username }}</small></template></el-table-column><el-table-column prop="phone" label="手机号" min-width="130" /><el-table-column prop="tenant_name" label="租户" min-width="150" /><el-table-column prop="action" label="动作" min-width="170" /><el-table-column label="时间" width="175"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="95" fixed="right"><template #default="{ row }"><el-button text @click="showDetail('audit', row)">详情</el-button></template></el-table-column>
            </template>
          </el-table>
          <el-pagination class="list-pagination" background layout="total, prev, pager, next" :total="total" :page-size="20" :current-page="filter.page" @current-change="paginate" />
          </section>
        </template>
      </div>
    </main>
  </section>
  <el-drawer v-model="mobileNav" title="平台管理导航" size="260px" direction="ltr"><nav class="console-drawer-nav"><button v-for="item in navigation" :key="item.id" @click="selectSection(item.id)">{{ item.name }}</button></nav></el-drawer>
  <el-drawer :model-value="!!detail" :title="detailKind === 'users' ? '账号详情' : detailKind === 'tenants' ? '租户详情' : detailKind === 'uploads' ? '上传详情' : '操作详情'" size="min(620px, 96vw)" @close="detail = null; ++detailRequest">
    <template v-if="detail"><h2>{{ detail.username || detail.name || detail.filename || detail.action }}</h2>
      <el-descriptions :column="1" border><el-descriptions-item label="ID">{{ detail.id }}</el-descriptions-item><el-descriptions-item v-if="detailKind === 'users'" label="姓名">{{ detail.full_name || '未填写姓名' }}</el-descriptions-item><el-descriptions-item v-if="detail.tenant_name" label="租户">{{ detail.tenant_name }}</el-descriptions-item><el-descriptions-item v-if="detail.phone" label="手机号">{{ detail.phone }}</el-descriptions-item><el-descriptions-item label="创建时间">{{ date(detail.created_at) }}</el-descriptions-item><template v-if="detailKind === 'users' || detailKind === 'tenants'"><el-descriptions-item v-if="detailKind === 'users'" label="活跃状态">{{ detail.active ? detail.online_sessions ? '在线（' + detail.online_sessions + ' 个会话）' : detail.last_activity_at ? '离线' : '从未活跃' : '账号已停用' }}</el-descriptions-item><el-descriptions-item v-if="detailKind === 'users'" label="最近活动">{{ date(detail.last_activity_at) }}</el-descriptions-item><el-descriptions-item label="当前页面数">{{ detail.pages }}</el-descriptions-item><el-descriptions-item v-if="detailKind === 'users'" label="累计上传 / 失败">{{ detail.uploads }} / {{ detail.failed }}</el-descriptions-item><el-descriptions-item v-if="detailKind === 'users'" label="历史版本">{{ detail.versions }}</el-descriptions-item><el-descriptions-item label="已用 / 配额">{{ bytes(detail.storage_bytes) }} / {{ bytes(detail.quota_bytes) }}</el-descriptions-item><el-descriptions-item v-if="detailKind === 'users'" label="最近上传">{{ date(detail.last_upload_at) }}</el-descriptions-item></template></el-descriptions>
      <template v-if="detailKind === 'users' || detailKind === 'tenants'"><p class="muted">{{ detailKind === 'users' ? '账号按上传者累计所有保留版本；调整租户后不会清零。' : '租户按当前文件归属统计；历史版本与保留文件占用空间。' }}</p><h3>近期上传</h3><el-alert v-if="detailError" :title="detailError" type="error" :closable="false" /><div v-for="job in recent" :key="job.id" class="metric-row"><span>{{ job.filename }}<small class="table-secondary">{{ date(job.created_at) }}</small></span><el-tag size="small">{{ uploadLabel[job.status] }}</el-tag></div><p v-if="!recent.length && !detailError" class="muted">暂无上传记录</p><el-button v-if="!detail.deleted_at && detail.role !== 'superadmin'" type="primary" @click="edit(detailKind, detail)">编辑设置</el-button><el-button v-if="detailKind === 'tenants' && !detail.deleted_at" type="danger" plain @click="removeTenant(detail)">删除租户</el-button></template>
      <template v-else><p v-if="detail.error" class="error-message">{{ detail.error }}</p><pre class="wrap-code">{{ JSON.stringify(detail.detail || detail.result || { status: detail.status }, null, 2) }}</pre></template>
    </template>
  </el-drawer>
  <el-drawer v-model="failedDrawer" title="上传失败原因" size="min(920px, 96vw)" @close="++failedRequest">
    <el-alert v-if="failedError" :title="failedError" type="error" :closable="false" />
    <el-table v-loading="failedLoading" :data="failedUploads" row-key="id" empty-text="没有失败记录"><el-table-column prop="filename" label="文件" min-width="160" /><el-table-column label="上传用户" min-width="145"><template #default="{ row }">{{ row.full_name || row.username }}<small class="table-secondary">{{ row.username }} · {{ row.phone || '无手机号' }}</small></template></el-table-column><el-table-column label="失败原因" min-width="280"><template #default="{ row }"><span class="error-message failure-reason">{{ row.error || '未记录失败原因' }}</span></template></el-table-column><el-table-column label="提交时间" width="170"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column></el-table>
    <el-pagination v-if="failedTotal > 20" class="list-pagination" background layout="total, prev, pager, next" :total="failedTotal" :page-size="20" :current-page="failedPage" @current-change="showFailedUploads" />
  </el-drawer>
  <el-drawer :model-value="!!editKind" :title="editKind === 'tenants' ? (editing ? '管理租户' : '创建租户') : (editing ? '管理用户' : '创建普通用户')" size="min(520px, 96vw)" :close-on-click-modal="!saving" :show-close="!saving" @close="editKind = ''; provisioned = ''">
    <template v-if="provisioned"><el-alert title="临时密码仅展示一次，首次登录需修改，请及时保存。" type="warning" :closable="false" /><pre class="wrap-code">{{ provisioned }}</pre></template>
    <el-form v-else label-position="top" @submit.prevent="save"><template v-if="editKind === 'tenants'"><el-form-item label="租户名称" required><el-input v-model="form.name" maxlength="100" /></el-form-item></template><template v-else><el-form-item label="姓名" required><el-input v-model="form.full_name" maxlength="60" autocomplete="name" /></el-form-item><template v-if="!editing"><el-form-item label="用户名" required><el-input v-model="form.username" maxlength="40" /></el-form-item><el-form-item label="手机号" required><el-input v-model="form.phone" maxlength="20" /></el-form-item></template><el-form-item label="所属租户" required><el-select v-model="form.tenant_id" filterable remote :remote-method="searchTenants" placeholder="输入租户名称搜索"><el-option v-for="tenant in tenantOptions" :key="tenant.id" :label="tenant.name" :value="tenant.id" /></el-select></el-form-item></template><el-form-item :label="editKind === 'users' ? '账号存储配额（MB）' : '租户存储配额（MB）'" required><el-input-number v-model="form.quota_mb" :min="1" :max="1048576" :precision="0" /></el-form-item><p class="muted">默认 1024 MB（1 GB）。降低配额保留已有文件，空间不足时阻止新增上传。</p><el-form-item v-if="editing" label="启用"><el-switch v-model="form.active" /></el-form-item><el-button type="primary" native-type="submit" :loading="saving">保存</el-button></el-form>
  </el-drawer>
</template>
