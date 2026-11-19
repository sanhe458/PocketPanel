<template>
  <Teleport to="body">
    <div v-if="show" class="overlay" @click.self="closeOnBg">
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
import { onMounted, onUnmounted, watch } from 'vue'
import Icon from './Icon.vue'
import { pushOverlay, removeOverlay, isTopOverlay } from '../overlay'

const props = defineProps({
  // 全站习惯是 <Modal v-if="open">，此时不传 show，默认 true 即可。
  // 但写 :show="open" 也认——早先 Mefrp 页就漏了 v-if 只传 show，组件又不认这个
  // prop，结果四个弹窗常驻 DOM，最上面那个「客户端日志」点关闭毫无反应。
  show: { type: Boolean, default: true },
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
// 栈只在真正显示时占位：常驻挂载 + show=false 的写法不该挡住下面那层的 Esc。
let token = null
function attach() { if (!token) token = pushOverlay(Symbol('modal')) }
function detach() {
  if (token) { removeOverlay(token); token = null }
}
watch(() => props.show, (v) => (v ? attach() : detach()), { immediate: true })

function onKey(e) {
  if (!token) return
  if (e.key === 'Escape' && isTopOverlay(token)) emit('close')
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => {
  detach()
  window.removeEventListener('keydown', onKey)
})
</script>
