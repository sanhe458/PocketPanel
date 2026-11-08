<template>
  <div class="stat">
    <div class="stat-h">
      <Icon v-if="iconName" :name="iconName" />
      <span>{{ label }}</span>
    </div>
    <div class="stat-v">
      {{ value }}<small v-if="unit">{{ unit }}</small>
    </div>
    <div v-if="sub" class="stat-sub">{{ sub }}</div>
    <div v-if="pct != null" class="stat-bar">
      <i :style="{ width: Math.max(0, Math.min(100, pct)) + '%', background: barColor }"></i>
    </div>
    <div v-if="iconName" class="stat-icon" :style="{ background: iconBg, color: iconColor }">
      <Icon :name="iconName" />
    </div>
  </div>
</template>
<script setup>
import Icon from './Icon.vue'
const props = defineProps({
  label: String,
  value: [String, Number],
  unit: { type: String, default: '' },
  sub: { type: String, default: '' },
  pct: { type: Number, default: null },
  iconName: { type: String, default: '' },
  tone: { type: String, default: 'brand' }  // brand|ok|warn|danger|purple
})
const TONE = {
  brand: ['var(--brand-soft)', 'var(--brand)'],
  ok: ['var(--ok-soft)', 'var(--ok)'],
  warn: ['var(--warn-soft)', 'var(--warn)'],
  danger: ['var(--danger-soft)', 'var(--danger)'],
  purple: ['var(--purple-soft)', 'var(--purple)']
}
const iconBg = TONE[props.tone] ? TONE[props.tone][0] : TONE.brand[0]
const iconColor = TONE[props.tone] ? TONE[props.tone][1] : TONE.brand[1]
const barColor = iconColor
</script>
