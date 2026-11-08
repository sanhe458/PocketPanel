<template>
  <div class="empty">
    <div class="ei"><Icon :name="iconName" /></div>
    <b>{{ title }}</b>
    <span v-if="desc">{{ desc }}</span>
    <slot />
  </div>
</template>
<script setup>
import Icon from './Icon.vue'
defineProps({
  iconName: { type: String, default: 'info' },
  title: { type: String, default: '暂无数据' },
  desc: { type: String, default: '' }
})
</script>
