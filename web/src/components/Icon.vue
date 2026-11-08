<template>
  <svg viewBox="0 0 24 24" class="ni" :class="sizeClass" v-html="path" aria-hidden="true"></svg>
</template>
<script setup>
import { computed } from 'vue'
import { icon } from '../icons'

const props = defineProps({
  name: { type: String, required: true },
  size: { type: String, default: '' }   // '', 's12', 's14', 's18', 's20', 's24'
})
const path = computed(() => icon(props.name))
const sizeClass = computed(() => props.size ? ' ' + props.size : '')
</script>
