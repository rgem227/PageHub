<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search, FolderOpened, Refresh, Delete, Document, Download, View } from '@element-plus/icons-vue'
import { api, bytes, date, json } from './api'

const props = withDefaults(defineProps<{ platform?: boolean; pageId?: string; refreshKey?: object | number }>(), { platform: false, pageId: '' })
const emit = defineEmits<{ changed: []; focus: [pageId: string] }>()
const filters = reactive({ q: '', state: 'all', sort: 'newest', tenant: '', owner: '', menu: '', page: 1 })
const focusedPage = ref(props.pageId)
const focusedTitle = ref('')
const tab = ref('files')
const rows = ref<any[]>([])
const folders = ref<any[]>([])
const totals = ref<any>({})
const jobs = ref<any[]>([])
const jobPage = ref(1)
const jobTotal = ref(0)
const total = ref(0)
const selected = ref<any[]>([])
const ownerOptions = ref<any[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const detail = ref<any>(null)
const preview = ref('')
const previewRow = ref<any>(null)
const previewLoading = ref(false)
const manifest = ref<any>({ items: [], folders: [], ready: false, total: 0, page: 1 })
const entryFilter = reactive({ path: '', q: '', page: 1 })
const entryLoading = ref(false)
const entryError = ref('')
let serial = 0
let jobSerial = 0
let entrySerial = 0
let ownerSerial = 0
let disposed = false
let searchTimer: ReturnType<typeof setTimeout> | undefined
let entryTimer: ReturnType<typeof setTimeout> | undefined
let poll: ReturnType<typeof setInterval> | undefined
const fileStates: Record<string, string> = { active: '文件保留', queued: '等待删除', processing: '删除中', failed: '删除失败', deleted: '文件已删除', temporary: '上传处理中', unpublished: '未生成页面版本' }
const jobStates: Record<string, string> = { queued: '等待删除', processing: '删除中', failed: '删除失败', success: '已完成' }
const bindingLabels: Record<string, string> = { attached: '已挂接', withdrawn: '页面已下架', unbound: '未挂接' }
const scopeParams = () => new URLSearchParams({ ...(props.platform ? { platform: 'true' } : {}), ...(filters.tenant ? { tenant: filters.tenant } : {}) })
const endpoint = (path: string) => path + '?' + scopeParams()
const canDeleteVersion = (row: any) => !!row.version_id && !row.is_current && !row.purge_id && row.file_state === 'active'
const canDownload = (row: any) => row.upload_status === 'success' && row.file_state === 'active'
const canPreview = (row: any) => props.platform && !!row.version_id && row.file_state === 'active' && !row.purge_id
const selectedHistory = computed(() => selected.value.filter(canDeleteVersion))
const tenantOptions = computed(() => [...new Map(folders.value.map(row => [row.tenant_id, { id: row.tenant_id, name: row.tenant_name }])).values()])
const folderTree = computed(() => {
  const nodes = new Map<string, any>(folders.value.filter(row => !filters.tenant || row.tenant_id === filters.tenant).map(row => [row.id, { ...row, children: [], total_bytes: Number(row.direct_bytes) }]))
  const roots: any[] = []
  for (const node of nodes.values()) {
    if (nodes.has(node.parent_id)) nodes.get(node.parent_id).children.push(node)
    else roots.push(node)
  }
  function sum(node: any): number { node.total_bytes = Number(node.direct_bytes) + node.children.reduce((amount: number, child: any) => amount + sum(child), 0); return node.total_bytes }
  roots.forEach(sum)
  if (!props.platform || filters.tenant) return roots
  return tenantOptions.value.map(tenant => {
    const children = roots.filter(root => root.tenant_id === tenant.id)
    return { id: 'tenant-' + tenant.id, name: tenant.name, tenant_id: tenant.id, children, total_bytes: children.reduce((n, child) => n + child.total_bytes, 0), isTenant: true }
  })
})

async function loadSecondary(current: number) {
  const summaryParams = scopeParams()
  if (filters.owner) summaryParams.set('owner', filters.owner)
  const [summaryResult, foldersResult] = await Promise.allSettled([
    api('/files/summary?' + summaryParams), api('/files/folders?' + summaryParams),
  ])
  if (current !== serial || disposed) return
  if (summaryResult.status === 'fulfilled') totals.value = summaryResult.value
  else error.value = summaryResult.reason?.message || '空间统计加载失败'
  if (foldersResult.status === 'fulfilled') folders.value = foldersResult.value
  else error.value = foldersResult.reason?.message || '目录加载失败'
}
async function loadDeletions() {
  const current = ++jobSerial
  const params = scopeParams()
  if (filters.owner) params.set('owner', filters.owner)
  params.set('page', String(jobPage.value))
  try {
    const result = await api('/files/deletions?' + params)
    if (current !== jobSerial || disposed) return
    const pendingBefore = jobs.value.filter(job => ['queued', 'processing'].includes(job.status)).map(job => job.id)
    jobs.value = result.items; jobTotal.value = result.total; jobPage.value = result.page
    if (pendingBefore.some(id => jobs.value.some(job => job.id === id && job.status === 'success'))) {
      emit('changed')
      void loadSecondary(serial)
    }
  } catch (e: any) { if (current === jobSerial && !disposed) error.value = e.message }
}
async function load(options: { secondary?: boolean } = {}) {
  const current = ++serial
  loading.value = true; error.value = ''
  const params = scopeParams()
  for (const [key, value] of Object.entries(filters)) if (value && !['tenant'].includes(key)) params.set(key, String(value))
  if (focusedPage.value) params.set('page_id', focusedPage.value)
  try {
    const result = await api('/files?' + params)
    if (current !== serial || disposed) return
    const hadPendingFiles = rows.value.some(row => ['temporary', 'queued', 'processing'].includes(row.file_state))
    rows.value = result.items; total.value = result.total; filters.page = result.page
    selected.value = []
    const hasPendingFiles = rows.value.some(row => ['temporary', 'queued', 'processing'].includes(row.file_state))
    if (options.secondary !== false || hadPendingFiles && !hasPendingFiles) void loadSecondary(current)
    if (tab.value === 'jobs') void loadDeletions()
  } catch (e: any) { if (current === serial && !disposed) error.value = e.message }
  finally { if (current === serial && !disposed) loading.value = false }
}
function search() { filters.page = 1; ++serial; if (searchTimer) clearTimeout(searchTimer); searchTimer = setTimeout(load, 250) }
function chooseFolder(node: any) {
  if (node.isTenant) { filters.tenant = node.tenant_id; filters.menu = '' }
  else filters.menu = node.id
  search()
}
function chooseState(state: string) { filters.menu = ''; filters.state = state; search() }
function focusPage(row?: any) { focusedPage.value = row?.page_id || ''; focusedTitle.value = row?.title || ''; filters.menu = ''; filters.owner = ''; filters.state = 'all'; emit('focus', focusedPage.value); search() }
async function owners(value = '') {
  const current = ++ownerSerial
  try { const params = scopeParams(); params.set('q', value); const result = await api('/files/owners?' + params); if (current === ownerSerial && !disposed) ownerOptions.value = result }
  catch (e: any) { if (!disposed) ElMessage.error(e.message) }
}
async function action(fn: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  try { await fn(); await load(); emit('changed') }
  catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message) }
  finally { busy.value = false }
}
function downloadUrl(row: any) { return '/api/files/uploads/' + row.id + '/download?' + scopeParams() }
async function showPreview(row: any) {
  if (!canPreview(row) || previewLoading.value) return
  previewLoading.value = true
  try {
    const result = await api(endpoint('/files/versions/' + row.version_id + '/preview'))
    previewRow.value = row
    preview.value = result.url
  } catch (e: any) { ElMessage.error(e.message || '页面预览失败') }
  finally { previewLoading.value = false }
}
async function download(row: any) {
  const params = scopeParams(); params.append('ids', row.id)
  try { await api('/files/download/check?' + params); window.location.assign(downloadUrl(row)) }
  catch (e: any) { ElMessage.error(e.message || '下载失败') }
}
async function downloadSelected() {
  const params = scopeParams()
  selected.value.forEach(row => params.append('ids', row.id))
  try { await api('/files/download/check?' + params); window.location.assign('/api/files/download?' + params) }
  catch (e: any) { ElMessage.error(e.message || '批量下载失败') }
}
async function removeVersions(items: any[]) {
  if (!items.length) return
  await action(async () => {
    const amount = items.reduce((n, row) => n + Number(row.size_bytes), 0)
    const count = items.reduce((n, row) => n + Number(row.file_count), 0)
    await ElMessageBox.confirm(`将永久删除 ${items.length} 个历史版本、${count} 个文件，预计释放 ${bytes(amount)}。删除后无法撤销，当前版本不受影响。`, '彻底删除历史版本', { type: 'warning', confirmButtonText: '彻底删除', cancelButtonText: '取消' })
    await api(endpoint('/files/versions/delete'), json('POST', { ids: items.map(row => row.version_id) }))
    ElMessage.success('清理任务已提交，完成后释放空间')
  })
}
async function purgePage(row: any) {
  await action(async () => {
    const statsParams = new URLSearchParams({ tenant: row.tenant_id, ...(props.platform ? { platform: 'true' } : {}) })
    const impact = await api('/files/pages/' + row.page_id + '/impact?' + statsParams)
    await ElMessageBox.confirm(`将彻底删除“${row.title}”的全部 ${impact.versions} 个保留版本、${impact.file_count} 个文件，预计释放 ${bytes(impact.size_bytes)}。页面访问链接立即失效，菜单保留。此操作无法撤销。`, '彻底删除页面及文件', { type: 'warning', confirmButtonText: '彻底删除', cancelButtonText: '取消' })
    await api('/files/pages/' + row.page_id + '/delete?' + statsParams, json('POST', { expected_version_ids: impact.version_ids }))
    ElMessage.success('整页清理已提交，可在清理任务中查看进度')
  })
}
async function activate(row: any) {
  await action(async () => {
    await ElMessageBox.confirm(`将“${row.title}”切换至 V${row.version_no}？原当前版本会保留为历史版本。`, '切换页面版本', { confirmButtonText: '确认切换', cancelButtonText: '取消' })
    await api(endpoint('/files/versions/' + row.version_id + '/activate'), json('POST'))
    ElMessage.success('当前版本已切换')
  })
}
async function retry(row: any) { await action(async () => { await api(endpoint('/files/deletions/' + row.id + '/retry'), json('POST')); ElMessage.success('已重新加入清理队列') }) }
async function showEntries(row: any) {
  detail.value = row; entryFilter.path = ''; entryFilter.q = ''; entryFilter.page = 1
  manifest.value = { ready: false, items: [], folders: [], total: 0, page: 1 }
  await loadEntries()
}
async function loadEntries() {
  if (!detail.value) return
  const current = ++entrySerial
  entryLoading.value = true; entryError.value = ''
  try {
    const params = scopeParams(); params.set('path', entryFilter.path); params.set('q', entryFilter.q); params.set('page', String(entryFilter.page))
    const result = await api('/files/versions/' + detail.value.version_id + '/entries?' + params)
    if (current === entrySerial && !disposed) { manifest.value = result; entryFilter.page = result.page }
  } catch (e: any) { if (current === entrySerial && !disposed) entryError.value = e.message }
  finally { if (current === entrySerial && !disposed) entryLoading.value = false }
}
function searchEntries() { entryFilter.page = 1; ++entrySerial; if (entryTimer) clearTimeout(entryTimer); entryTimer = setTimeout(loadEntries, 250) }
async function rescan() {
  try { await api(endpoint('/files/versions/' + detail.value.version_id + '/rescan'), json('POST')); await loadEntries() }
  catch (e: any) { ElMessage.error(e.message) }
}
watch(() => props.pageId, value => { focusedPage.value = value; focusedTitle.value = ''; filters.page = 1; void load() })
watch(() => props.refreshKey, () => { void load() })
watch(tab, value => { if (value === 'jobs') void loadDeletions() })
onMounted(() => {
  void load()
  poll = setInterval(() => {
    if (!busy.value && document.visibilityState === 'visible' && rows.value.some(row => ['temporary', 'queued', 'processing'].includes(row.file_state))) void load({ secondary: false })
    if (!busy.value && document.visibilityState === 'visible' && tab.value === 'jobs' && jobs.value.some(job => ['queued', 'processing'].includes(job.status))) void loadDeletions()
    if (detail.value && !manifest.value.ready && !manifest.value.error && !entryError.value) void loadEntries()
  }, 2500)
})
onBeforeUnmount(() => { disposed = true; ++serial; ++jobSerial; ++entrySerial; ++ownerSerial; if (poll) clearInterval(poll); if (searchTimer) clearTimeout(searchTimer); if (entryTimer) clearTimeout(entryTimer) })
</script>

<template>
  <section class="file-manager" aria-label="文件管理">
    <div class="file-summary">
      <div><span>{{ platform ? '当前筛选文件占用' : '租户文件占用' }}</span><strong>{{ bytes(totals.used_bytes) }} <small>/ {{ bytes(totals.quota_bytes) }}</small></strong></div>
      <button @click="chooseState('current')"><span>当前版本</span><strong>{{ bytes(totals.current_bytes) }}</strong></button>
      <button @click="chooseState('history')"><span>历史版本</span><strong>{{ bytes(totals.history_bytes) }}</strong></button>
      <button @click="chooseState('withdrawn')"><span>已下架页面</span><strong>{{ bytes(totals.withdrawn_bytes) }}</strong></button>
    </div>
    <div class="file-layout">
      <aside class="file-folders panel" aria-label="文件目录">
        <div class="file-folder-heading"><b>菜单目录</b><el-icon><FolderOpened /></el-icon></div>
        <button class="file-all" @click="filters.menu = ''; filters.tenant = ''; chooseState('all')">全部文件 <span>{{ bytes(totals.used_bytes) }}</span></button>
        <el-tree :data="folderTree" node-key="id" :props="{ label: 'name' }" :highlight-current="true" :expand-on-click-node="false" :default-expand-all="!platform" @node-click="chooseFolder">
          <template #default="{ data }"><span class="file-folder-node"><span :title="data.name">{{ data.name }}</span><small>{{ bytes(data.total_bytes) }}</small></span></template>
        </el-tree>
        <p class="muted file-folder-note">目录大小包含下级和全部保留版本。下架页面的文件仍占空间。</p>
      </aside>
      <div class="panel file-main">
        <div class="file-toolbar">
          <el-input v-model="filters.q" clearable :prefix-icon="Search" placeholder="搜索文件名或页面名称" @input="search" />
          <el-select v-if="platform" v-model="filters.tenant" clearable filterable placeholder="全部租户" aria-label="文件租户" @change="filters.menu = ''; filters.owner = ''; search()"><el-option v-for="tenant in tenantOptions" :key="tenant.id" :value="tenant.id" :label="tenant.name" /></el-select>
          <el-select v-model="filters.owner" clearable filterable remote :remote-method="owners" placeholder="全部上传者" aria-label="文件上传者" @visible-change="(open: boolean) => open && owners()" @change="search"><el-option v-for="owner in ownerOptions" :key="owner.id" :value="owner.id" :label="owner.username" /></el-select>
          <el-select v-model="filters.state" aria-label="文件状态" @change="search"><el-option label="全部状态" value="all" /><el-option label="当前版本" value="current" /><el-option label="历史版本" value="history" /><el-option label="页面已下架" value="withdrawn" /><el-option label="未挂接" value="unbound" /><el-option label="文件已删除" value="deleted" /><el-option label="等待/正在删除" value="pending" /><el-option label="上传或删除失败" value="failed" /></el-select>
          <el-select v-model="filters.sort" aria-label="文件排序" @change="search"><el-option label="最新上传" value="newest" /><el-option label="最早上传" value="oldest" /><el-option label="占用空间从大到小" value="size" /></el-select>
        </div>
        <div class="file-actions"><el-radio-group v-model="tab" size="small"><el-radio-button value="files">上传文件</el-radio-button><el-radio-button value="jobs">清理任务</el-radio-button></el-radio-group><el-tag v-if="focusedPage" closable @close="focusPage()">{{ focusedTitle || '已筛选此页面的全部版本' }}</el-tag><el-button v-if="tab === 'files'" size="small" :icon="Download" :disabled="!selected.length || selected.length > 100 || busy" @click="downloadSelected">批量下载{{ selected.length ? ` (${selected.length})` : '' }}</el-button><el-button v-if="tab === 'files'" size="small" type="danger" plain :icon="Delete" :disabled="!selectedHistory.length || busy" @click="removeVersions(selectedHistory)">删除所选历史版本{{ selectedHistory.length ? ` (${selectedHistory.length})` : '' }}</el-button></div>
        <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button text @click="load">重试</el-button></el-alert>
        <template v-if="tab === 'files'">
          <el-table v-loading="loading" :data="rows" row-key="id" empty-text="暂无匹配的上传文件" @selection-change="selected = $event">
            <el-table-column type="selection" width="40" :selectable="canDownload" />
            <el-table-column label="文件 / 页面 / 版本" min-width="220"><template #default="{ row }"><button class="table-name" :disabled="row.file_state !== 'active'" @click="showEntries(row)">{{ row.filename }}</button><small class="table-secondary">{{ row.title || '未生成页面' }}</small><small class="table-secondary">{{ row.version_no ? `V${row.version_no} · ${row.is_current ? '当前版本' : '历史版本'}` : '未发布' }}{{ platform ? ` · ${row.tenant_name}` : '' }}</small></template></el-table-column>
            <el-table-column label="菜单挂接" min-width="155"><template #default="{ row }"><el-tag size="small" :type="row.binding === 'attached' ? 'success' : 'info'">{{ bindingLabels[row.binding] }}</el-tag><small class="table-secondary">{{ row.menu_path || '—' }}</small></template></el-table-column>
            <el-table-column label="占用 / 状态" width="130"><template #default="{ row }"><b>{{ bytes(row.size_bytes) }}</b><small class="table-secondary">{{ row.version_id ? `${row.file_count} 个文件` : `原包 ${bytes(row.source_bytes)}` }}</small><el-tag size="small" :type="row.file_state === 'failed' ? 'danger' : row.file_state === 'active' ? 'success' : 'info'">{{ fileStates[row.file_state] }}</el-tag></template></el-table-column>
            <el-table-column label="上传时间 / 上传者" width="160"><template #default="{ row }">{{ date(row.created_at) }}<small class="table-secondary">{{ row.username }}</small></template></el-table-column>
            <el-table-column label="操作" width="220" fixed="right"><template #default="{ row }"><div class="file-row-actions"><el-button text size="small" :disabled="!canDownload(row)" @click="download(row)">下载原件</el-button><el-button text size="small" :disabled="row.file_state !== 'active'" @click="showEntries(row)">文件清单</el-button><el-dropdown trigger="click" :disabled="busy"><el-button text size="small" aria-label="文件操作">更多</el-button><template #dropdown><el-dropdown-menu><el-dropdown-item v-if="platform" :disabled="!canPreview(row) || previewLoading" :icon="View" @click="showPreview(row)">预览页面</el-dropdown-item><el-dropdown-item :disabled="!row.page_id" @click="focusPage(row)">查看全部版本</el-dropdown-item><el-dropdown-item v-if="!row.is_current && row.binding === 'attached' && row.file_state === 'active'" @click="activate(row)">设为当前版本</el-dropdown-item><el-dropdown-item :disabled="!canDeleteVersion(row)" @click="removeVersions([row])">删除此版本</el-dropdown-item><el-dropdown-item v-if="row.page_id && !row.purge_id && row.file_state !== 'deleted'" divided @click="purgePage(row)">彻底删除页面及文件</el-dropdown-item></el-dropdown-menu></template></el-dropdown></div></template></el-table-column>
          </el-table>
          <el-pagination v-if="total > 50" class="list-pagination" background layout="total, prev, pager, next" :total="total" :page-size="50" :current-page="filters.page" @current-change="(page: number) => { filters.page = page; load() }" />
          <p class="muted file-table-note">{{ total }} 次上传记录 · 原始 ZIP 大小与解压后占用不同；文件按完整版本删除，实际删除完成后归还空间。</p>
        </template>
        <template v-else>
          <el-table v-loading="loading" :data="jobs" row-key="id" empty-text="暂无文件清理任务"><el-table-column label="清理对象" min-width="190"><template #default="{ row }">{{ row.title }}<small class="table-secondary">{{ row.kind === 'page' ? '整个页面及全部版本' : '历史版本' }} · {{ row.version_ids.length }} 个版本</small></template></el-table-column><el-table-column label="预计释放" width="135"><template #default="{ row }">{{ bytes(row.size_bytes) }}<small class="table-secondary">{{ row.file_count }} 个文件</small></template></el-table-column><el-table-column label="状态" width="105"><template #default="{ row }"><el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'success' ? 'success' : 'info'">{{ jobStates[row.status] }}</el-tag></template></el-table-column><el-table-column label="申请时间 / 操作者" width="180"><template #default="{ row }">{{ date(row.created_at) }}<small class="table-secondary">{{ row.username }}{{ platform ? ` · ${row.tenant_name}` : '' }}</small></template></el-table-column><el-table-column label="结果" min-width="220"><template #default="{ row }"><span v-if="row.status === 'success'">已释放 {{ bytes(row.size_bytes) }} · {{ date(row.completed_at) }}</span><span v-else-if="row.error" class="error-message">{{ row.error }}</span><span v-else>完成前继续占用配额；异常退出后自动接续。</span><el-button v-if="row.status === 'failed'" text type="primary" :disabled="busy" @click="retry(row)">重试删除</el-button></template></el-table-column></el-table>
          <el-pagination v-if="jobTotal > 20" class="list-pagination" background layout="total, prev, pager, next" :total="jobTotal" :page-size="20" :current-page="jobPage" @current-change="(page: number) => { jobPage = page; loadDeletions() }" />
        </template>
      </div>
    </div>
    <el-drawer :model-value="!!detail" :title="detail ? `${detail.filename} · V${detail.version_no} 文件清单` : '文件清单'" size="min(860px, 96vw)" @close="detail = null; ++entrySerial">
      <template v-if="detail"><p class="muted">{{ detail.title }} · {{ bytes(detail.size_bytes) }} · {{ detail.file_count }} 个文件 · {{ date(detail.created_at) }}</p><div class="file-entry-toolbar"><el-input v-model="entryFilter.q" clearable :prefix-icon="Search" placeholder="搜索版本内部文件" @input="searchEntries" /><el-select v-model="entryFilter.path" clearable placeholder="全部内部目录" aria-label="内部文件夹" @change="searchEntries"><el-option v-for="folder in manifest.folders" :key="folder.path" :value="folder.path" :label="`${folder.path} (${bytes(folder.size_bytes)})`" /></el-select><el-button :icon="Refresh" :loading="entryLoading" @click="loadEntries">刷新</el-button></div><el-alert v-if="entryError || manifest.error" :title="entryError || manifest.error" type="error" :closable="false"><el-button text @click="rescan">重新扫描</el-button></el-alert><el-alert v-else-if="!manifest.ready" title="后台正在补齐文件清单，完成后自动显示。" type="info" :closable="false" /><el-table v-loading="entryLoading" :data="manifest.items" empty-text="暂无匹配文件"><el-table-column label="文件路径" min-width="240"><template #default="{ row }"><span class="file-path"><el-icon><Document /></el-icon>{{ row.path }}</span></template></el-table-column><el-table-column label="大小" width="120"><template #default="{ row }">{{ bytes(row.size_bytes) }}</template></el-table-column></el-table><el-pagination v-if="manifest.total > 100" class="list-pagination" background layout="total, prev, pager, next" :total="manifest.total" :page-size="100" :current-page="entryFilter.page" @current-change="(page: number) => { entryFilter.page = page; loadEntries() }" /><p class="muted file-table-note">资源按完整版本一起删除，避免遗漏图片、样式或脚本导致页面损坏。</p></template>
    </el-drawer>
    <el-drawer :model-value="!!preview" :title="previewRow ? `${previewRow.title || previewRow.filename} · V${previewRow.version_no} 预览` : '页面预览'" size="min(1200px, 96vw)" @close="preview = ''; previewRow = null">
      <template v-if="previewRow"><p class="muted">{{ previewRow.tenant_name }} · {{ previewRow.username }} · 此版本的临时预览授权有效 5 分钟</p><iframe class="file-preview-frame" :src="preview" sandbox="allow-scripts" referrerpolicy="no-referrer" :title="previewRow.title || previewRow.filename" /></template>
    </el-drawer>
  </section>
</template>

<style scoped>
.file-summary{display:grid;grid-template-columns:1.4fr repeat(3,1fr);gap:12px;margin-bottom:14px}.file-summary>div,.file-summary>button{min-width:0;text-align:left;border:1px solid var(--line);border-radius:12px;background:var(--surface);padding:14px 18px;color:var(--text)}.file-summary>button:hover{border-color:var(--el-color-primary);background:var(--accent-soft)}.file-summary span{font-size:12px;color:var(--muted)}.file-summary strong{display:block;font-size:21px;font-weight:600;margin-top:7px}.file-summary small{font-size:11px;color:var(--muted);font-weight:400}.file-layout{display:grid;grid-template-columns:230px minmax(0,1fr);gap:14px}.file-manager .file-folders{padding:16px 10px;align-self:start;min-width:0}.file-folder-heading{display:flex;align-items:center;justify-content:space-between;margin:0 8px 12px;color:var(--muted);font-size:12px}.file-all{display:flex;justify-content:space-between;gap:8px;align-items:center;width:100%;padding:10px 8px;border:0;background:var(--accent-soft);color:var(--el-color-primary);border-radius:7px;font-size:12px;margin-bottom:8px}.file-all span{font-size:11px}.file-folders :deep(.el-tree){background:transparent;max-height:calc(100dvh - 290px);overflow:auto}.file-folder-node{display:flex;justify-content:space-between;align-items:center;gap:8px;flex:1;min-width:0;padding-right:8px;font-size:12px}.file-folder-node>span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.file-folder-node small{flex-shrink:0;font-size:10px;color:var(--muted)}.file-folders :deep(.el-tree-node__content){height:34px;border-radius:6px}.file-folder-note{margin:16px 8px 0;font-size:11px}.file-manager .file-main{min-width:0;padding:16px}.file-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.file-toolbar>.el-input{flex:1;min-width:180px}.file-toolbar>.el-select{width:145px}.file-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:14px}.file-actions>.el-button{margin-left:auto}.file-row-actions{display:flex;align-items:center;gap:2px}.file-row-actions .el-button{margin:0;padding:6px}.file-table-note{font-size:11px;margin:12px 0 0}.file-manager :deep(.el-alert){margin-bottom:12px}.file-entry-toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}.file-entry-toolbar>.el-input{flex:1;min-width:150px}.file-entry-toolbar>.el-select{width:240px;max-width:100%}.file-path{display:flex;align-items:center;gap:8px;overflow-wrap:anywhere;font-size:12px}.file-path .el-icon{flex-shrink:0;color:var(--muted)}
.file-preview-frame{display:block;width:100%;height:calc(100dvh - 190px);min-height:320px;border:1px solid var(--line);border-radius:6px;background:#fff}
.file-summary{--metric-blue:#3978d4;--metric-green:#21836e;--metric-amber:#a96a19;--metric-rose:#c6535e}
.file-summary>div,.file-summary>button{--metric-accent:var(--metric-blue);--metric-tint:#edf4ff;position:relative;isolation:isolate;overflow:hidden;border-color:color-mix(in srgb,var(--metric-accent) 20%,var(--line));background:linear-gradient(145deg,color-mix(in srgb,var(--metric-tint) 72%,var(--surface)),var(--surface) 72%);box-shadow:0 3px 12px color-mix(in srgb,var(--metric-accent) 7%,transparent);transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.file-summary>div::before,.file-summary>button::before{content:"";position:absolute;z-index:-1;inset:0 auto 0 0;width:3px;background:var(--metric-accent)}
.file-summary>:nth-child(2){--metric-accent:var(--metric-green);--metric-tint:#eaf6f1}.file-summary>:nth-child(3){--metric-accent:var(--metric-amber);--metric-tint:#fff4e3}.file-summary>:nth-child(4){--metric-accent:var(--metric-rose);--metric-tint:#fff0f1}
.file-summary span{display:flex;align-items:center;gap:8px;font-weight:550}.file-summary span::before{content:"";width:7px;height:7px;flex:0 0 7px;border-radius:50%;background:var(--metric-accent);box-shadow:0 0 0 3px color-mix(in srgb,var(--metric-accent) 12%,transparent)}
.file-summary strong{font-size:20px;color:var(--text);font-variant-numeric:tabular-nums}.file-summary small{color:var(--muted)}
.file-summary>button:hover{transform:translateY(-2px);border-color:var(--metric-accent);box-shadow:0 8px 20px color-mix(in srgb,var(--metric-accent) 13%,transparent)}
:global(.dark) .file-summary>div,:global(.dark) .file-summary>button{--metric-blue:#83b0ff;--metric-green:#72c9ae;--metric-amber:#efbd72;--metric-rose:#f09198;--metric-tint:#1c2d49;border-color:color-mix(in srgb,var(--metric-accent) 24%,var(--line));background:linear-gradient(145deg,color-mix(in srgb,var(--metric-tint) 78%,var(--surface)),var(--surface) 76%);box-shadow:0 4px 16px #0002}
:global(.dark) .file-summary>:nth-child(2){--metric-tint:#19362f}:global(.dark) .file-summary>:nth-child(3){--metric-tint:#3a2d1c}:global(.dark) .file-summary>:nth-child(4){--metric-tint:#3d2429}
:global(.dark) .file-summary>button:hover{box-shadow:0 8px 22px #0004}
@media(prefers-reduced-motion:reduce){.file-summary>button{transition:none}.file-summary>button:hover{transform:none}}
@media(max-width:1100px){.file-layout{grid-template-columns:190px minmax(0,1fr)}.file-toolbar>.el-input{min-width:100%}.file-toolbar>.el-select{flex:1;min-width:130px}.file-summary strong{font-size:18px}}
@media(max-width:700px){.file-summary{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.file-summary>div,.file-summary>button{padding:12px}.file-layout{grid-template-columns:minmax(0,1fr)}.file-manager .file-folders{padding:12px}.file-folders :deep(.el-tree){max-height:180px}.file-folder-note{display:none}.file-summary strong{font-size:17px}.file-actions>.el-button{margin-left:0}.file-manager .file-main{padding:12px}}
</style>
