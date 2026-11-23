<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
defineProps<{ bookmark: any }>()
defineEmits<{ back: [] }>()
const revision = ref(0)
const hint = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
function remind() { if (timer) clearTimeout(timer); hint.value = false; timer = setTimeout(() => { hint.value = true }, 8000) }
function refresh() { ++revision.value; remind() }
onMounted(remind)
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
</script>
<template>
  <div class="preview-toolbar"><h1 :title="bookmark.title">{{ bookmark.title }}</h1><div class="inline preview-actions"><el-button text size="small" :icon="Refresh" @click="refresh">刷新</el-button><a class="bookmark-external" :href="bookmark.url" target="_blank" rel="noopener noreferrer">新标签页打开 ↗</a><el-button text size="small" @click="$emit('back')">返回</el-button></div></div>
  <el-alert v-if="hint" class="bookmark-embed-hint" title="如果页面空白、显示拒绝连接或无法登录，网站可能限制嵌入；请使用上方“新标签页打开”。" type="info" show-icon @close="hint = false" />
  <iframe :key="bookmark.id + '-' + revision" class="page-viewer" :src="bookmark.url" sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox" referrerpolicy="no-referrer" :title="bookmark.title" />
</template>
