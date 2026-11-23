<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { Directive } from 'vue'
import { ArrowRight, Search, Grid, List, Share } from '@element-plus/icons-vue'
import { api, date, json } from './api'

const props = defineProps<{ menus: any[]; state: { q: string; category: string; sort: string; page: number; scroll: number }; view: string; external: boolean }>()
const emit = defineEmits<{ choose: [id: string]; open: [page: any]; share: [menu: any]; view: [value: string]; change: [] }>()
const items = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const categories = computed(() => props.menus.filter(m => !m.parent_id && m.visible).map(m => ({ value: m.id, label: m.name })))
let serial = 0
let debounce: ReturnType<typeof setTimeout> | undefined
let disposed = false
const draggedMenuId = ref('')
const dropTargetId = ref('')
const motion = window.matchMedia('(prefers-reduced-motion: reduce)')
const hoverPointer = window.matchMedia('(hover: hover) and (pointer: fine)')
const arrivalObserver = new IntersectionObserver(entries => {
  for (const entry of entries) if (entry.isIntersecting) {
    entry.target.classList.add('is-visible')
    arrivalObserver.unobserve(entry.target)
  }
}, { threshold: 0.1 })
const vArrive: Directive<HTMLElement> = {
  mounted(el) { arrivalObserver.observe(el) },
  beforeUnmount(el) { arrivalObserver.unobserve(el) },
}
function menuTone(id: string) {
  let hash = 0
  for (const char of id) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash % 4
}
let pointerFrame = 0
function resetCard(el: HTMLElement) {
  cancelAnimationFrame(pointerFrame)
  for (const name of ['--glow-x', '--glow-y', '--tilt-x', '--tilt-y']) el.style.removeProperty(name)
}
function settleCard(event: PointerEvent) { (event.currentTarget as HTMLElement).parentElement?.classList.add('is-settled') }
function leaveCard(event: PointerEvent) { resetCard(event.currentTarget as HTMLElement) }
function enterDrop(id: string) { if (draggedMenuId.value) dropTargetId.value = id }
function moveGlow(event: PointerEvent) {
  if (props.view === 'list' || motion.matches || !hoverPointer.matches || draggedMenuId.value || event.pointerType !== 'mouse') return
  const el = event.currentTarget as HTMLElement
  const bounds = el.getBoundingClientRect()
  const x = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width))
  const y = Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height))
  cancelAnimationFrame(pointerFrame)
  pointerFrame = requestAnimationFrame(() => {
    el.style.setProperty('--glow-x', `${x * 100}%`)
    el.style.setProperty('--glow-y', `${y * 100}%`)
    el.style.setProperty('--tilt-x', `${(0.5 - y) * 2}deg`)
    el.style.setProperty('--tilt-y', `${(x - 0.5) * 2}deg`)
  })
}
function startDrag(event: DragEvent, id: string) {
  draggedMenuId.value = id
  const card = (event.currentTarget as HTMLElement).querySelector<HTMLElement>('.directory-card')
  if (card) resetCard(card)
  if (event.dataTransfer) { event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', id) }
}
function endDrag() { draggedMenuId.value = ''; dropTargetId.value = '' }
async function load() {
  const request = ++serial
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ q: props.state.q, sort: props.state.sort, page: String(props.state.page), size: props.view === 'list' ? '50' : '48' })
    if (props.state.category) params.set('category', props.state.category)
    const result = await api('/catalogue?' + params)
    if (request !== serial || disposed) return
    items.value = result.items; total.value = result.total; props.state.page = result.page
  } catch (e: any) { if (request === serial && !disposed) error.value = e.message }
  finally { if (request === serial && !disposed) loading.value = false }
}
async function moveMenuCard(targetId: string) {
  const sourceId = draggedMenuId.value
  endDrag()
  if (!sourceId || sourceId === targetId) return
  try {
    await api('/menus/' + sourceId + '/flat-move', json('POST', { target_id: targetId, before: true }))
    await load()
  } catch (e: any) { error.value = e.message || '排序失败' }
}
watch(() => [props.state.q, props.state.category, props.state.sort], () => {
  props.state.page = 1; props.state.scroll = 0; ++serial
  loading.value = true; emit('change')
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => { void load() }, 250)
})
watch(() => props.view, () => { props.state.page = 1; emit('change'); void load() })
watch(() => props.menus, () => { void load() })
function changePage(value: number) { props.state.page = value; props.state.scroll = 0; emit('change'); void load(); window.scrollTo(0, 0) }
onMounted(async () => { await load(); await nextTick(); if (!disposed) window.scrollTo(0, props.state.scroll) })
onBeforeUnmount(() => { disposed = true; ++serial; arrivalObserver.disconnect(); cancelAnimationFrame(pointerFrame); if (debounce) clearTimeout(debounce); props.state.scroll = window.scrollY })
</script>

<template>
  <section class="application-directory">
    <div class="directory-heading"><h1>工作空间<span class="count-badge">{{ total }}</span></h1></div>
    <div class="directory-toolbar">
      <el-input v-model="state.q" :prefix-icon="Search" clearable placeholder="搜索应用或页面名称" aria-label="搜索应用或页面名称" maxlength="200" />
      <el-select-v2 v-model="state.category" :options="categories" clearable filterable placeholder="全部分类" aria-label="应用分类" />
      <el-select v-model="state.sort" aria-label="应用排序"><el-option label="菜单顺序" value="position" /><el-option label="名称排序" value="name" /><el-option label="最近更新" value="updated" /></el-select>
      <div class="view-switch" role="group" aria-label="目录展示方式"><button :class="{ active: view === 'compact' }" :aria-pressed="view === 'compact'" aria-label="紧凑目录" @click="emit('view', 'compact')"><el-icon><Grid /></el-icon></button><button :class="{ active: view === 'list' }" :aria-pressed="view === 'list'" aria-label="列表视图" @click="emit('view', 'list')"><el-icon><List /></el-icon></button></div>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false"><el-button text @click="load">重试</el-button></el-alert>
    <div v-loading="loading" class="directory-results" :aria-busy="loading">
      <TransitionGroup tag="nav" name="card-swap" class="flat-menus" :class="{ 'directory-list': view === 'list', 'is-sorting': draggedMenuId }" aria-label="平铺菜单">
        <div v-for="(menu, index) in items" :key="menu.id" class="directory-entry" :data-tone="menuTone(menu.id)" :class="{ 'is-dragging': draggedMenuId === menu.id, 'is-drop-target': dropTargetId === menu.id && draggedMenuId !== menu.id }" :draggable="state.sort === 'position'" :title="state.sort === 'position' ? '拖动卡片调整顺序' : undefined" @dragstart="startDrag($event, menu.id)" @dragend="endDrag" @dragover.prevent="state.sort === 'position'" @dragenter.prevent="enterDrop(menu.id)" @drop.prevent="state.sort === 'position' && moveMenuCard(menu.id)">
          <div v-arrive class="directory-arrival" :style="{ '--arrival-delay': Math.min(index, 6) * 35 + 'ms' }">
          <div class="directory-card" @pointerenter="settleCard" @pointermove="moveGlow" @pointerleave="leaveCard">
          <div class="directory-entry-main"><button class="directory-entry-link" :title="menu.name + '\n' + menu.path" :aria-label="menu.name" @click="emit('choose', menu.id)">
            <span class="flat-link-mark" aria-hidden="true">{{ Array.from(menu.name)[0] }}</span>
            <span class="flat-link-copy"><span class="flat-link-name">{{ menu.name }}</span><span class="flat-link-path" :title="menu.path">{{ menu.path === menu.name ? '独立应用' : menu.path }}</span></span>
            <el-icon class="flat-link-arrow" aria-hidden="true"><ArrowRight /></el-icon>
          </button><div class="directory-entry-footer"><div class="directory-entry-details"><span class="directory-entry-meta" :class="{ 'is-pending': !Number(menu.page_count) }">{{ Number(menu.page_count) ? menu.page_count + ' 个页面' + (external ? ' · 新窗口' : '') : '待发布' }}</span><time v-if="menu.published_at" class="directory-entry-published" :datetime="menu.published_at">最近发布 {{ date(menu.published_at).split(' ')[0] }}</time></div><el-button v-if="Number(menu.page_count) > 0" class="directory-entry-share" text size="small" :icon="Share" @click="emit('share', menu)">分享</el-button></div></div>
          <div v-if="menu.matches.length" class="matched-pages"><button v-for="match in menu.matches" :key="match.id" :aria-label="'打开匹配页面：' + match.title" @click="emit('open', match)">{{ match.title }} <el-icon aria-hidden="true"><ArrowRight /></el-icon></button></div>
          </div></div>
        </div>
      </TransitionGroup>
      <div v-if="!items.length && !loading && !error" class="directory-empty"><el-icon :size="30"><Search /></el-icon><h2>{{ state.q || state.category ? '没有找到匹配的应用' : '你的应用空间已就绪' }}</h2><p class="muted">{{ state.q || state.category ? '试试其他关键词，或清除分类筛选。' : '从顶部工作台上传 HTML，也可以让智能体通过 MCP 自动发布。' }}</p><el-button v-if="state.q || state.category" text @click="state.q = ''; state.category = ''">清除筛选</el-button></div>
    </div>
    <el-pagination v-if="total > (view === 'list' ? 50 : 48)" class="list-pagination" background layout="total, prev, pager, next" :total="total" :page-size="view === 'list' ? 50 : 48" :current-page="state.page" @current-change="changePage" />
  </section>
</template>
