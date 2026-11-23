<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Search, Plus, Grid, List, TopRight, Link, MoreFilled, Download } from '@element-plus/icons-vue'
import { api, date, json } from './api'

const props = defineProps<{ state: { q: string; category: string; sort: string; page: number; view: string } }>()
const emit = defineEmits<{ open: [entry: any]; changed: []; change: [] }>()
const rows = ref<any[]>([])
const total = ref(0)
const categories = ref<any[]>([])
const allCategories = ref<any[]>([])
const loading = ref(false)
const error = ref('')
const selected = ref<any[]>([])
const dialog = ref(false)
const editing = ref('')
const saving = ref(false)
const editorRef = ref<FormInstance>()
const bookmarkForm = ref({ title: '', url: '', description: '', category_id: '', open_mode: 'external', position: 0 })
const categoryDialog = ref(false)
const categoryEditOpen = ref(false)
const categoryEditing = ref('')
const categoryFormRef = ref<FormInstance>()
const categoryForm = ref({ name: '', position: 0 })
const moveDialog = ref(false)
const moveIds = ref<string[]>([])
const targetCategory = ref('')
let serial = 0
let draggedBookmarkId = ''
let debounce: ReturnType<typeof setTimeout> | undefined
const categoryOptions = computed(() => allCategories.value.map(c => ({ value: c.id, label: c.name })))
const groups = computed(() => {
  const grouped = new Map<string, { name: string; items: any[] }>()
  for (const row of rows.value) {
    const id = props.state.sort === 'position' ? row.category_id : 'sorted'
    if (!grouped.has(id)) grouped.set(id, { name: props.state.sort === 'position' ? row.category_name : '搜索与排序结果', items: [] })
    grouped.get(id)!.items.push(row)
  }
  return Array.from(grouped, ([id, group]) => ({ id, ...group }))
})
function categoryTone(id: string) {
  let hash = 0
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash % 4
}
const rules: FormRules = {
  title: [{ required: true, whitespace: true, message: '请输入网站名称', trigger: 'blur' }],
  url: [{ required: true, message: '请输入网站地址', trigger: 'blur' }, { validator: (_r, value, callback) => {
    try { const url = new URL(value); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error(); callback() }
    catch { callback(new Error('请输入完整的 HTTP 或 HTTPS 网址，勿包含账号密码')) }
  }, trigger: 'blur' }],
}
const categoryRules: FormRules = { name: [{ required: true, whitespace: true, message: '请输入分类名称', trigger: 'blur' }] }
async function loadCategories() {
  const [visible, managed] = await Promise.all([api('/bookmark-categories'), api('/bookmark-categories?include_empty=true')])
  categories.value = visible; allCategories.value = managed
}
async function load() {
  const request = ++serial; loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ q: props.state.q, page: String(props.state.page), size: '40', sort: props.state.sort })
    if (props.state.category) params.set('category_id', props.state.category)
    const result = await api('/bookmarks?' + params)
    if (request !== serial) return
    rows.value = result.items; total.value = result.total; props.state.page = result.page; selected.value = []
  } catch (e: any) { if (request === serial) error.value = e.message }
  finally { if (request === serial) loading.value = false }
}
function downloadList() {
  const params = new URLSearchParams({ q: props.state.q, sort: props.state.sort })
  if (props.state.category) params.set('category_id', props.state.category)
  window.location.assign('/api/bookmarks/export?' + params)
}
async function moveBookmarkCard(targetId: string) {
  const sourceId = draggedBookmarkId
  draggedBookmarkId = ''
  if (!sourceId || sourceId === targetId) return
  saving.value = true
  try {
    await api('/bookmarks/' + sourceId + '/move', json('POST', { target_id: targetId, before: true }))
    await load()
  } catch (e: any) { ElMessage.error(e.message || '排序失败') }
  finally { saving.value = false }
}
watch(() => [props.state.q, props.state.category, props.state.sort], () => {
  props.state.page = 1; ++serial; loading.value = true; emit('change')
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(load, 250)
})
watch(() => props.state.view, () => emit('change'))
onMounted(async () => { await loadCategories(); await load() })
onBeforeUnmount(() => { ++serial; if (debounce) clearTimeout(debounce) })
function paginate(value: number) { props.state.page = value; emit('change'); void load(); window.scrollTo(0, 0) }
function host(url: string) { try { return new URL(url).host } catch { return '' } }
function edit(row?: any) {
  editing.value = row?.id || ''
  Object.assign(bookmarkForm.value, { title: row?.title || '', url: row?.url || '', description: row?.description || '', category_id: row?.category_id || props.state.category || allCategories.value.find(c => c.name === '未分类')?.id || '', open_mode: row?.open_mode || 'external', position: row?.position || 0 })
  dialog.value = true
}
async function save() {
  if (!await editorRef.value?.validate().catch(() => false)) return
  saving.value = true
  try {
    const data = { ...bookmarkForm.value, title: bookmarkForm.value.title.trim(), url: bookmarkForm.value.url.trim() }
    const endpoint = '/bookmarks' + (editing.value ? '/' + editing.value : '')
    const method = editing.value ? 'PATCH' : 'POST'
    try { await api(endpoint, json(method, data)) }
    catch (e: any) {
      if (!e.message.includes('已收藏相同网址')) throw e
      await ElMessageBox.confirm('此分类已收藏相同网址，是否仍保存？', '重复网址', { confirmButtonText: '仍然保存', cancelButtonText: '取消' })
      await api(endpoint, json(method, { ...data, allow_duplicate: true }))
    }
    dialog.value = false; emit('changed'); await loadCategories(); await load(); ElMessage.success('收藏已保存')
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '保存失败') }
  finally { saving.value = false }
}
function editCategory(category?: any) {
  categoryEditing.value = category?.id || ''
  Object.assign(categoryForm.value, { name: category?.name || '', position: category?.position || 0 })
  categoryEditOpen.value = true
}
async function saveCategory() {
  if (!await categoryFormRef.value?.validate().catch(() => false)) return
  saving.value = true
  try {
    await api('/bookmark-categories' + (categoryEditing.value ? '/' + categoryEditing.value : ''),
      json(categoryEditing.value ? 'PATCH' : 'POST', categoryForm.value))
    categoryEditOpen.value = false; await loadCategories(); await load(); emit('change'); ElMessage.success('分类已保存')
  } catch (e: any) { ElMessage.error(e.message || '保存失败') }
  finally { saving.value = false }
}
async function removeCategory(category: any) {
  try {
    await ElMessageBox.confirm(`删除分类“${category.name}”？此分类需为空。`, '删除收藏分类', { type: 'warning' })
    await api('/bookmark-categories/' + category.id, json('DELETE'))
    if (props.state.category === category.id) props.state.category = ''
    await loadCategories(); await load(); emit('change'); ElMessage.success('分类已删除')
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '删除失败') }
}
async function remove(ids: string[]) {
  try {
    await ElMessageBox.confirm(`删除 ${ids.length} 条收藏链接？目标网站不受影响。`, '删除收藏', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
    await api('/bookmarks/batch-delete', json('POST', { ids })); emit('changed'); await loadCategories(); await load(); ElMessage.success('收藏已删除')
  } catch (e: any) { if (e !== 'cancel' && e !== 'close') ElMessage.error(e.message || '删除失败') }
}
function startMove(ids: string[]) { moveIds.value = ids; targetCategory.value = ''; moveDialog.value = true }
async function move() {
  if (!targetCategory.value) { ElMessage.warning('请选择目标分类'); return }
  saving.value = true
  try {
    await api('/bookmarks/batch-move', json('POST', { ids: moveIds.value, category_id: targetCategory.value }))
    moveDialog.value = false; emit('changed'); await loadCategories(); await load(); ElMessage.success('分类已调整')
  } catch (e: any) { ElMessage.error(e.message) }
  finally { saving.value = false }
}
</script>

<template>
  <section class="bookmark-library" aria-label="网站收藏">
    <div class="bookmark-toolbar">
      <el-input v-model="state.q" :prefix-icon="Search" clearable maxlength="200" placeholder="搜索名称、网址或备注" aria-label="搜索收藏" />
      <el-select-v2 v-model="state.category" :options="categoryOptions" filterable clearable placeholder="全部分类" aria-label="收藏分类筛选" />
      <el-select v-model="state.sort" aria-label="收藏排序"><el-option label="分类与顺序" value="position" /><el-option label="名称排序" value="name" /><el-option label="最近更新" value="updated" /></el-select>
      <div class="view-switch" role="group" aria-label="收藏展示方式"><button :class="{ active: state.view === 'cards' }" :aria-pressed="state.view === 'cards'" aria-label="收藏卡片视图" @click="state.view = 'cards'"><el-icon><Grid /></el-icon></button><button :class="{ active: state.view === 'list' }" :aria-pressed="state.view === 'list'" aria-label="收藏管理列表" @click="state.view = 'list'"><el-icon><List /></el-icon></button></div>
      <div class="bookmark-actions"><el-button text size="small" :icon="Download" @click="downloadList">下载清单</el-button><el-button text size="small" :icon="Plus" @click="editCategory()">新增分类</el-button><el-button text size="small" @click="categoryDialog = true">管理分类</el-button><el-button type="primary" :icon="Plus" @click="edit()">新增收藏</el-button></div>
    </div>
    <div class="bookmark-category-bar"><nav class="bookmark-categories" aria-label="收藏分类"><button :class="{ active: !state.category }" @click="state.category = ''">全部</button><button v-for="category in categories" :key="category.id" :class="{ active: state.category === category.id }" @click="state.category = category.id">{{ category.name }} <small>{{ category.bookmark_count }}</small></button></nav></div>
    <div class="bookmark-summary"><span>外链收藏 · 共 {{ total }} 项</span><span>默认新标签页打开</span></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button text @click="load">重试</el-button></el-alert>
    <div v-loading="loading" :aria-busy="loading">
      <template v-if="state.view === 'cards'">
        <section v-for="group in groups" :key="group.id" class="bookmark-group" :data-tone="categoryTone(group.id)">
          <div class="bookmark-group-heading"><h2>{{ group.name }}</h2><span>本页 {{ group.items.length }} 项</span></div>
          <TransitionGroup tag="div" name="card-swap" class="bookmark-grid"><article v-for="row in group.items" :key="row.id" class="bookmark-card" :draggable="state.sort === 'position'" :title="state.sort === 'position' ? '拖动卡片调整顺序' : undefined" @dragstart="draggedBookmarkId = row.id" @dragend="draggedBookmarkId = ''" @dragover.prevent="state.sort === 'position'" @drop.prevent="state.sort === 'position' && moveBookmarkCard(row.id)">
            <button class="bookmark-open" :aria-label="'打开收藏：' + row.title" @click="emit('open', row)"><span class="bookmark-mark" aria-hidden="true">{{ Array.from(row.title)[0] }}</span><span class="bookmark-copy"><strong>{{ row.title }}</strong><span v-if="row.description" class="bookmark-description" :title="row.description">{{ row.description }}</span><small :title="row.url">{{ host(row.url) }}</small></span><el-icon class="bookmark-open-indicator" :title="row.open_mode === 'external' ? '新标签页打开' : '尝试框架内显示'"><TopRight v-if="row.open_mode === 'external'" /><Link v-else /></el-icon></button>
            <el-dropdown trigger="click" class="bookmark-more"><el-button text circle :icon="MoreFilled" :aria-label="'管理收藏：' + row.title" /><template #dropdown><el-dropdown-menu><el-dropdown-item @click="edit(row)">编辑收藏</el-dropdown-item><el-dropdown-item @click="startMove([row.id])">移动分类</el-dropdown-item><el-dropdown-item divided @click="remove([row.id])">删除收藏</el-dropdown-item></el-dropdown-menu></template></el-dropdown>
          </article></TransitionGroup>
        </section>
      </template>
      <section v-else class="panel bookmark-table">
        <div class="bookmark-batch"><span class="muted">已选 {{ selected.length }} 项</span><el-button size="small" :disabled="!selected.length || loading" @click="startMove(selected.map(r => r.id))">批量移动</el-button><el-button size="small" type="danger" plain :disabled="!selected.length || loading" @click="remove(selected.map(r => r.id))">批量删除</el-button><small class="muted">单页最多选择 40 项</small></div>
        <el-table :data="rows" row-key="id" @selection-change="selected = $event" empty-text="暂无匹配的收藏"><el-table-column type="selection" width="42" /><el-table-column label="网站" min-width="220"><template #default="{ row }"><button class="bookmark-table-title" @click="emit('open', row)">{{ row.title }}</button><small class="table-secondary" :title="row.url">{{ row.url }}</small><small v-if="row.description" class="table-secondary">{{ row.description }}</small></template></el-table-column><el-table-column prop="category_name" label="分类" min-width="120" /><el-table-column label="打开方式" width="140"><template #default="{ row }">{{ row.open_mode === 'external' ? '新标签页' : '尝试框架内' }}</template></el-table-column><el-table-column prop="position" label="排序" width="70" /><el-table-column label="收藏时间" min-width="165"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="150" fixed="right"><template #default="{ row }"><el-button text @click="edit(row)">编辑</el-button><el-button text type="danger" @click="remove([row.id])">删除</el-button></template></el-table-column></el-table>
      </section>
      <div v-if="!rows.length && !loading && !error && state.view === 'cards'" class="directory-empty"><el-icon :size="30"><Link /></el-icon><h2>{{ state.q || state.category ? '没有找到匹配收藏' : '把常用网站收在一起' }}</h2><p class="muted">按平级分类整理外链收藏；不会显示上传页面或租户导航分类。</p><el-button @click="edit()" type="primary" :icon="Plus">新增收藏</el-button></div>
    </div>
    <el-pagination v-if="total > 40" class="list-pagination" background layout="total, prev, pager, next" :total="total" :page-size="40" :current-page="state.page" @current-change="paginate" />
    <el-dialog v-model="dialog" :title="editing ? '编辑收藏' : '新增收藏'" width="min(560px, 94vw)" @closed="editorRef?.clearValidate()"><el-form ref="editorRef" :model="bookmarkForm" :rules="rules" label-position="top" @submit.prevent="save"><el-form-item label="网站名称" prop="title"><el-input v-model="bookmarkForm.title" maxlength="200" placeholder="例如：团队知识库" /></el-form-item><el-form-item label="网站地址" prop="url"><el-input v-model="bookmarkForm.url" maxlength="2048" placeholder="https://example.com" /></el-form-item><el-form-item label="收藏分类"><el-select-v2 v-model="bookmarkForm.category_id" :options="categoryOptions" filterable clearable placeholder="未分类" aria-label="收藏所属分类" class="wide" /><el-button text type="primary" :icon="Plus" @click="editCategory()">新增分类</el-button></el-form-item><el-form-item label="备注"><el-input v-model="bookmarkForm.description" type="textarea" :rows="2" maxlength="1000" show-word-limit /></el-form-item><div class="bookmark-form-bottom"><el-form-item label="打开方式"><el-radio-group v-model="bookmarkForm.open_mode"><el-radio-button value="external">新标签页</el-radio-button><el-radio-button value="embedded">尝试框架内</el-radio-button></el-radio-group></el-form-item><el-form-item label="排序值（越小越靠前）"><el-input-number v-model="bookmarkForm.position" :precision="0" :min="-2147483648" :max="2147483647" /></el-form-item></div><p class="muted bookmark-form-note">收藏仅保存外链。部分网站限制嵌入或登录，框架内查看时仍可在新标签页打开。</p></el-form><template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="save">保存收藏</el-button></template></el-dialog>
    <el-dialog v-model="moveDialog" title="移动收藏分类" width="min(440px, 94vw)"><p class="muted">移动 {{ moveIds.length }} 条外链收藏。</p><el-select-v2 v-model="targetCategory" :options="categoryOptions" filterable placeholder="选择目标分类" aria-label="目标收藏分类" class="wide" /><template #footer><el-button @click="moveDialog = false">取消</el-button><el-button type="primary" :loading="saving" @click="move">确认移动</el-button></template></el-dialog>
    <el-dialog v-model="categoryDialog" title="管理收藏分类" width="min(620px, 94vw)"><div class="bookmark-batch"><el-button type="primary" :icon="Plus" @click="editCategory()">新增分类</el-button><span class="muted">独立于租户菜单，平级分类；仅有收藏的分类出现在页面筛选中。</span></div><el-table :data="allCategories" row-key="id" empty-text="暂无收藏分类"><el-table-column prop="name" label="分类名称" min-width="180" /><el-table-column prop="bookmark_count" label="收藏数" width="90" /><el-table-column prop="position" label="顺序" width="80" /><el-table-column label="操作" width="145"><template #default="{ row }"><el-button text @click="editCategory(row)">编辑</el-button><el-button text type="danger" @click="removeCategory(row)">删除</el-button></template></el-table-column></el-table></el-dialog>
    <el-dialog v-model="categoryEditOpen" :title="categoryEditing ? '编辑收藏分类' : '新增收藏分类'" width="min(420px, 94vw)" @closed="categoryFormRef?.clearValidate()"><el-form ref="categoryFormRef" :model="categoryForm" :rules="categoryRules" label-position="top"><el-form-item label="平级分类名称" prop="name"><el-input v-model="categoryForm.name" maxlength="60" placeholder="例如：开发工具" /></el-form-item><el-form-item label="排序值（越小越靠前）"><el-input-number v-model="categoryForm.position" :precision="0" /></el-form-item></el-form><template #footer><el-button @click="categoryEditOpen = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveCategory">保存分类</el-button></template></el-dialog>
  </section>
</template>
