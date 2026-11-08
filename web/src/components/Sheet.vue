<template>
  <Teleport to="body">
    <div class="sheet">
      <div class="sheet-bg" @click="close"></div>
      <div class="sheet-box">
        <div class="sheet-grip"></div>
        <div class="sheet-h">{{ title }}</div>
        <div v-for="(it, i) in items" :key="i" class="sheet-i" :class="{ danger: it.danger }"
             @click="$emit('pick', it)">
          <span class="i">
            <Icon v-if="isIcon(it.icon)" :name="it.icon" />
            <template v-else>{{ it.icon || '' }}</template>
          </span>
          <span class="grow ell">{{ it.label }}</span>
          <span v-if="it.sub" class="sc">{{ it.sub }}</span>
        </div>
        <div class="sheet-i" @click="close">
          <span class="i"></span><span>取消</span>
        </div>
      </div>
    </div>
  </Teleport>
</template>
<script setup>
import { onMounted, onUnmounted } from 'vue'
import Icon from './Icon.vue'
import { ICONS } from '../icons'
import { pushOverlay, removeOverlay, isTopOverlay } from '../overlay'

defineProps({ title: String, items: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'pick'])
function isIcon(n) { return !!n && Object.prototype.hasOwnProperty.call(ICONS, n) }
function close() { emit('close') }

// Esc 关闭：与 Modal 共用浮层栈，只有最上层响应
const token = pushOverlay(Symbol('sheet'))
function onKey(e) {
  if (e.key === 'Escape' && isTopOverlay(token)) close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => {
  removeOverlay(token)
  window.removeEventListener('keydown', onKey)
})
</script>
