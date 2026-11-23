<script setup lang="ts">
import { Grid, DataBoard, TrendCharts, Calendar, Collection, Reading, Monitor, Briefcase, OfficeBuilding, Connection, Location, Shop, Box, Cpu, Compass, Picture, Camera, Headset, Bell, Star, Flag, Trophy, Tools, Setting, Menu, Document } from '@element-plus/icons-vue'

type MenuNode = { id: string; name: string; children: MenuNode[] }
const props = withDefaults(defineProps<{ node: MenuNode; showIcons: boolean; depth?: number; position?: number }>(), { depth: 1, position: 0 })
defineEmits<{ choose: [id: string] }>()
const rootIcons = [Grid, DataBoard, TrendCharts, Calendar, Collection, Reading, Monitor, Briefcase, OfficeBuilding, Connection, Location, Shop, Box, Cpu, Compass, Picture, Camera, Headset, Bell, Star, Flag, Trophy, Tools, Setting]
function menuIcon(index: number) {
  // shortcut: More than 24 first-level menus reuse icons; add custom selection when needed.
  if (props.depth === 1) return rootIcons[index % rootIcons.length]
  return props.depth === 2 ? Menu : Document
}
</script>
<template>
    <el-sub-menu v-if="node.children.length" :index="node.id" :teleported="showIcons ? undefined : true">
      <template #title><el-icon v-if="showIcons" class="tenant-menu-icon" aria-hidden="true"><component :is="menuIcon(position)" /></el-icon><span class="tenant-menu-name" :title="node.name">{{ node.name }}</span></template>
      <MenuBranch v-for="(child, childIndex) in node.children" :key="child.id" :node="child" :show-icons="showIcons" :depth="depth + 1" :position="childIndex" @choose="$emit('choose', $event)" />
    </el-sub-menu>
    <el-menu-item v-else :index="node.id" @click="$emit('choose', node.id)"><el-icon v-if="showIcons" class="tenant-menu-icon" aria-hidden="true"><component :is="menuIcon(position)" /></el-icon><span class="tenant-menu-name" :title="node.name">{{ node.name }}</span></el-menu-item>
</template>
