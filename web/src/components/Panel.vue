<template>
  <Modal :title="title" :sub="sub" :icon-name="iconName" :size="size" @close="$emit('close')">
    <slot />
    <template v-if="$slots.footer" #footer><slot name="footer" /></template>
  </Modal>
</template>
<script setup>
import Modal from './Modal.vue'
defineProps({
  title: { type: String, default: '' },
  sub: { type: String, default: '' },
  iconName: { type: String, default: '' },
  size: { type: String, default: '' }
})
defineEmits(['close'])
</script>
