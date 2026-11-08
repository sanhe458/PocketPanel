<template>
  <Teleport to="body">
    <div class="overlay" @click.self="closeOnBg">
      <div class="overlay-bg" @click="closeOnBg"></div>
      <div class="modal" :class="sizeClass" role="dialog" aria-modal="true">
        <div class="modal-h">
          <slot name="icon">
            <Icon v-if="iconName" :name="iconName" class="ni s20" style="color:var(--brand)" />
          </slot>
          <div class="grow" style="min-width:0">
            <b class="ell">{{ title }}</b>
            <div v-if="sub" class="mh-sub ell">{{ sub }}</div>
          </div>
          <button class="ico-btn" @click="$emit('close')" title="关闭"><Icon name="close" /></button>
        </div>
        <div class="modal-b"><slot /></div>
        <div class="modal-f" v-if="$slots.footer"><slot name="footer" /></div>
      </div>
    </div>
  </Teleport>
</template>
<script setup>
import { onMounted, onUnmounted } from 'vue'
import Icon from './Icon.vue'
import { pushOverlay, removeOverlay, isTopOverlay } from '../overlay'

const props = defineProps({
  title: { type: String, default: '' },
  sub: { type: String, default: '' },
  iconName: { type: String, default: '' },
  size: { type: String, default: '' },     // '', 'wide', 'narrow'
  bgClose: { type: Boolean, default: true }
})
const emit = defineEmits(['close'])
function closeOnBg() { if (props.bgClose) emit('close') }

// Esc 关闭要挂在 window 上：浮层内的 div 拿不到焦点，@keydown 收不到按键。
// 与 Sheet 共用浮层栈，保证一次按键只关最上面那层。
const token = pushOverlay(Symbol('modal'))
function onKey(e) {
  if (e.key === 'Escape' && isTopOverlay(token)) emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => {
  removeOverlay(token)
  window.removeEventListener('keydown', onKey)
})
</script>
