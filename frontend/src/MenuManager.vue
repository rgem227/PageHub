<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Document, FolderOpened, Plus, Rank, Search } from '@element-plus/icons-vue'
import { api, json } from './api'

const props = defineProps<{ menus: any[] }>()
const emit = defineEmits<{ edit: [menu?: any]; remove: [menu: any]; changed: [] }>()
const q = ref('')
const menus = ref<any[]>(props.menus)
const moving = ref(false)
const error = ref('')
const draggingId = ref('')
const dropTargetId = ref('')
const dropMode = ref('')
watch(() => props.menus, value => { menus.value = value })

function byId(id: string) { return menus.value.find(menu => menu.id === id) }
function menuDepth(id: string) {
  let depth = 0
  let current = byId(id)
  while (current) { depth++; current = current.parent_id ? byId(current.parent_id) : null }
  return depth
}
function descendants(id: string): string[] {
  return [id, ...menus.value.filter(menu => menu.parent_id === id).flatMap(menu => descendants(menu.id))]
}
function subtreeHeight(id: string): number {
  return 1 + Math.max(0, ...menus.value.filter(menu => menu.parent_id === id).map(menu => subtreeHeight(menu.id)))
}
function menuIsVisible(id: string) {
  let current = byId(id)
  while (current) {
    if (!current.visible) return false
    current = current.parent_id ? byId(current.parent_id) : null
  }
  return true
}
const treeRows = computed(() => {
  const children = new Map<string | null, any[]>()
  for (const menu of menus.value) {
    if (!children.has(menu.parent_id)) children.set(menu.parent_id, [])
    children.get(menu.parent_id)!.push(menu)
  }
  const query = q.value.trim().toLocaleLowerCase()
  function branch(parentId: string | null, parentPath = '', depth = 1): any[] {
    return (children.get(parentId) || [])
      .sort((a, b) => Number(a.position) - Number(b.position) || a.name.localeCompare(b.name))
      .flatMap(menu => {
        const path = parentPath ? `${parentPath} / ${menu.name}` : menu.name
        const files = menu.attached_files || []
        const nested = branch(menu.id, path, depth + 1)
        const text = [menu.name, path, ...files.flatMap((file: any) => [file.title, file.path])].join(' ').toLocaleLowerCase()
        if (query && !text.includes(query) && !nested.length) return []
        return [{ ...menu, path, depth, children: nested.length ? nested : undefined }]
      })
  }
  return branch(null)
})

function rowClass({ row }: { row: any }) { return `menu-tree-row menu-depth-${row.depth}` }
function canMoveInto(sourceId: string, targetId: string) {
  const source = byId(sourceId)
  const target = byId(targetId)
  if (!source || !target || sourceId === targetId || source.parent_id === targetId) return false
  if (Number(target.page_count) > 0 || descendants(sourceId).includes(targetId)) return false
  return menuDepth(targetId) + subtreeHeight(sourceId) <= 3
}
function modeAt(event: DragEvent, target: any) {
  const source = byId(draggingId.value)
  if (!source || source.id === target.id || q.value.trim() || moving.value) return ''
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const offset = (event.clientY - rect.top) / rect.height
  if (offset < 0.24 || offset > 0.76)
    return source.parent_id === target.parent_id ? (offset < 0.5 ? 'swap-before' : 'swap-after') : ''
  return canMoveInto(source.id, target.id) ? 'into' : ''
}
function dragStart(event: DragEvent, menu: any) {
  if (q.value.trim() || moving.value) { event.preventDefault(); return }
  draggingId.value = menu.id
  event.dataTransfer?.setData('text/plain', menu.id)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}
function dragOver(event: DragEvent, menu: any) {
  const mode = modeAt(event, menu)
  if (!mode) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dropTargetId.value = menu.id
  dropMode.value = mode
}
function dragLeave(event: DragEvent, menu: any) {
  if ((event.currentTarget as HTMLElement).contains(event.relatedTarget as Node)) return
  if (dropTargetId.value === menu.id) { dropTargetId.value = ''; dropMode.value = '' }
}
function dragEnd() { draggingId.value = ''; dropTargetId.value = ''; dropMode.value = '' }
async function drop(event: DragEvent, target: any) {
  event.preventDefault()
  const sourceId = draggingId.value || event.dataTransfer?.getData('text/plain') || ''
  const mode = modeAt(event, target)
  dragEnd()
  if (!sourceId || !mode) return
  const operation = mode === 'into' ? 'into' : 'swap'
  let confirmHidden = false
  try {
    if (operation === 'into' && menuIsVisible(sourceId) && !menuIsVisible(target.id)) {
      await ElMessageBox.confirm('目标菜单或其上级菜单已隐藏。移动后，此菜单及其下属页面会从工作空间导航中隐藏；页面和文件不会删除。仍要移动吗？', '确认移动到隐藏菜单', { type: 'warning', confirmButtonText: '仍要移动', cancelButtonText: '取消' })
      confirmHidden = true
    }
    moving.value = true; error.value = ''
    await api(`/menus/${sourceId}/move`, json('POST', { target_id: target.id, operation, confirm_hidden: confirmHidden }))
    menus.value = await api('/menus')
    emit('changed')
    ElMessage.success(operation === 'swap' ? '菜单顺序已调整' : '菜单层级已调整')
  } catch (e: any) {
    if (e !== 'cancel' && e !== 'close') error.value = e.message || '菜单移动失败'
  } finally { moving.value = false }
}
</script>

<template>
  <section class="panel menu-manager-panel">
    <div class="console-filters menu-filters">
      <el-input v-model="q" :prefix-icon="Search" placeholder="搜索菜单或路径" clearable />
      <el-button type="primary" :icon="Plus" @click="emit('edit')">新增菜单</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <el-table v-loading="moving" :data="treeRows" row-key="id" default-expand-all :indent="36" :tree-props="{ children: 'children' }" :row-class-name="rowClass" empty-text="没有匹配的菜单">
      <el-table-column label="菜单名称" min-width="300">
        <template #default="{ row }">
          <div class="menu-drop-target" :class="{
            'is-dragging': draggingId === row.id,
            'is-swap-before': dropTargetId === row.id && dropMode === 'swap-before',
            'is-swap-after': dropTargetId === row.id && dropMode === 'swap-after',
            'is-move-into': dropTargetId === row.id && dropMode === 'into',
          }" :data-menu-id="row.id" :data-depth="row.depth" :style="{ '--menu-depth-indent': ((row.depth - 1) * 36) + 'px' }" :title="row.path" @dragover="dragOver($event, row)" @dragleave="dragLeave($event, row)" @drop="drop($event, row)">
            <button class="menu-drag-handle" type="button" :draggable="!q.trim() && !moving" :disabled="!!q.trim() || moving" :title="q.trim() ? '清除搜索后可拖动' : '拖动菜单调整顺序或层级'" :aria-label="'拖动菜单：' + row.name" @dragstart="dragStart($event, row)" @dragend="dragEnd"><el-icon><Rank /></el-icon></button>
            <el-icon class="menu-kind-icon"><component :is="row.depth === 3 ? Document : FolderOpened" /></el-icon>
            <span class="menu-tree-name">{{ row.name }}</span>
            <el-tag size="small" effect="plain" :class="'menu-depth-tag depth-' + row.depth">{{ ['','一级','二级','三级'][row.depth] }}</el-tag>
            <el-tag v-if="row.has_children" size="small" type="info" effect="plain">含子菜单</el-tag>
            <el-tag v-if="Number(row.page_count)" size="small" type="warning" effect="plain">已挂 {{ row.page_count }} 页</el-tag>
          </div>
          <small class="table-secondary menu-full-path" :style="{ '--menu-depth-indent': ((row.depth - 1) * 36) + 'px' }">{{ row.path }}</small>
        </template>
      </el-table-column>
      <el-table-column label="挂接文件相对路径" min-width="260">
        <template #default="{ row }">
          <div v-for="file in row.attached_files || []" :key="file.path" class="menu-file-path" :title="file.title"><span>{{ file.title }}</span><code>{{ file.path }}</code></div>
          <span v-if="!row.attached_files?.length" class="muted">{{ Number(row.page_count) ? '页面路径暂不可用' : '未直接挂接页面' }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="position" label="排序" width="85" />
      <el-table-column label="状态" width="90"><template #default="{ row }"><el-tag size="small" :type="row.visible ? 'success' : 'info'">{{ row.visible ? '显示' : '隐藏' }}</el-tag></template></el-table-column>
      <el-table-column label="操作" width="145" fixed="right"><template #default="{ row }"><el-button text @click="emit('edit', row)">编辑</el-button><el-button text type="danger" @click="emit('remove', row)">删除</el-button></template></el-table-column>
    </el-table>
  </section>
</template>
