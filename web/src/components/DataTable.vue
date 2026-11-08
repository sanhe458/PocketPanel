<template>
  <div class="card flush">
    <div class="card-h">
      <Icon v-if="iconName" :name="iconName" />
      <h3>{{ title }}<slot name="titleExtra" /></h3>
      <div class="sub"><slot name="sub" /></div>
    </div>
    <div v-if="$slots.toolbar" class="row wrap" style="padding:10px 16px;border-bottom:1px solid var(--line);gap:8px">
      <slot name="toolbar" />
    </div>
    <div class="tbl-wrap">
      <table class="tbl">
        <thead>
          <tr>
            <th v-for="c in cols" :key="c.key" :class="c.align" :style="c.width ? { width: c.width } : null">
              {{ c.title }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="rowKey(r, i)" :class="{ clickable: !!onRowClick }"
              @click="onRowClick && onRowClick(r)">
            <td v-for="c in cols" :key="c.key" :class="[c.align, c.nowrap ? 'nowrap' : '']">
              <slot :name="'c-' + c.key" :row="r" :value="r[c.key]">{{ r[c.key] }}</slot>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="!rows.length" class="tbl-empty">
      <slot name="empty">
        <div class="empty" style="padding:0">
          <div class="ei"><Icon name="list" /></div>
          <b>{{ emptyText }}</b>
        </div>
      </slot>
    </div>
  </div>
</template>
<script setup>
import Icon from './Icon.vue'
const props = defineProps({
  cols: { type: Array, default: () => [] },   // [{key,title,align,width,nowrap}]
  rows: { type: Array, default: () => [] },
  title: { type: String, default: '' },
  iconName: { type: String, default: '' },
  rowKey: { type: Function, default: null },
  emptyText: { type: String, default: '暂无数据' },
  onRowClick: { type: Function, default: null }
})
function rowKey(r, i) { return props.rowKey ? props.rowKey(r, i) : (r.id != null ? r.id : i) }
</script>
