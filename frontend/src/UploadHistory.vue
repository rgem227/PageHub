<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { api, bytes, date } from './api'
defineEmits<{ files: [pageId: string] }>()
const q = ref('')
const status = ref('all')
const page = ref(1)
const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
let request = 0
let debounce: ReturnType<typeof setTimeout> | undefined
let poll: ReturnType<typeof setInterval> | undefined
const labels: Record<string, string> = { success: '已发布', failed: '失败', queued: '排队解析', processing: '解析中' }
async function load() {
  const serial = ++request; loading.value = true; error.value = ''
  try {
    const result = await api('/upload-list?' + new URLSearchParams({ q: q.value, status: status.value, page: String(page.value), size: '50' }))
    if (serial !== request) return
    items.value = result.items; total.value = result.total; page.value = result.page
  } catch (e: any) { if (serial === request) error.value = e.message }
  finally { if (serial === request) loading.value = false }
}
watch([q, status], () => { ++request; page.value = 1; if (debounce) clearTimeout(debounce); debounce = setTimeout(load, 250) })
function paginate(value: number) { page.value = value; void load() }
onMounted(() => { void load(); poll = setInterval(() => { if (items.value.some(j => ['queued','processing'].includes(j.status))) void load() }, 2500) })
onBeforeUnmount(() => { ++request; if (debounce) clearTimeout(debounce); if (poll) clearInterval(poll) })
</script>
<template>
  <section class="panel"><div class="console-filters"><el-input v-model="q" :prefix-icon="Search" placeholder="搜索上传文件" clearable /><el-select v-model="status" aria-label="上传状态"><el-option label="全部状态" value="all" /><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value" /></el-select></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button text @click="load">重试</el-button></el-alert>
    <el-table v-loading="loading" :data="items" empty-text="暂无匹配的上传记录">
      <el-table-column prop="filename" label="文件名" min-width="180" />
      <el-table-column label="上传状态" width="115"><template #default="{ row }"><el-tag :type="row.status === 'success' ? 'success' : row.status === 'failed' ? 'danger' : 'warning'">{{ labels[row.status] }}</el-tag></template></el-table-column>
      <el-table-column label="大小" width="135"><template #default="{ row }">{{ bytes(row.size_bytes) }}<small v-if="row.expanded_bytes !== null" class="table-secondary">展开 {{ bytes(row.expanded_bytes) }}</small></template></el-table-column>
      <el-table-column label="文件状态" width="120"><template #default="{ row }">{{ row.file_deleted_at ? '文件已删除' : row.deletion_status === 'failed' ? '删除失败' : row.deletion_status ? '等待/正在删除' : row.version_id ? '文件保留' : ['queued','processing'].includes(row.status) ? '处理中' : '未生成页面版本' }}</template></el-table-column>
      <el-table-column label="处理结果" min-width="200"><template #default="{ row }">{{ row.error || row.result?.title || '等待处理' }}</template></el-table-column>
      <el-table-column label="上传时间" width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column>
      <el-table-column label="操作" width="100" fixed="right"><template #default="{ row }"><el-button v-if="row.page_id" text @click="$emit('files', row.page_id)">管理文件</el-button><span v-else>—</span></template></el-table-column>
    </el-table><el-pagination v-if="total > 50" class="list-pagination" background layout="total, prev, pager, next" :total="total" :page-size="50" :current-page="page" @current-change="paginate" />
  </section>
</template>
