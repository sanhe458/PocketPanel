<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="cpu" />进程管理</h2>
      <p>以当前 Termux 用户可见的进程，按常驻内存排序</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />刷新</button>
    </div>
  </div>

  <div class="grid g4" style="margin-bottom:12px">
    <StatCard label="进程总数" :value="data.count" icon-name="layers" />
    <StatCard label="常驻内存合计" :value="fmtB(data.rss_total)" sub="RSS 之和，含共享页" icon-name="memory" tone="purple" />
    <StatCard label="受保护进程" :value="protectedCount" sub="面板自身与关键进程不可结束" icon-name="shield" tone="ok" />
    <StatCard label="平均 RSS" :value="avgRss" sub="按当前进程数摊平" icon-name="gauge" tone="warn" />
  </div>

  <DataTable :cols="cols" :rows="data.list" title="进程列表" icon-name="cpu" :row-key="p => p.pid"
             empty-text="没有可见进程">
    <template #c-command="{ row }">
      <div class="tname mono ell" style="max-width:340px">{{ base(row.command) }}</div>
      <div class="tsub mono ell" style="max-width:340px">{{ rest(row.command) }}</div>
    </template>
    <template #c-pid="{ row }">
      <span class="mono dim">{{ row.pid }}</span>
    </template>
    <template #c-rss="{ row }">
      <span class="nowrap">{{ fmtB(row.rss) }}</span>
    </template>
    <template #c-stat="{ row }">
      <span class="tag">{{ row.stat }}</span>
    </template>
    <template #c-ops="{ row }">
      <div class="tbl-ops">
        <span v-if="row.protected" class="tag">{{ row.reason }}</span>
        <button v-else class="btn xs danger" @click.stop="kill(row)"><Icon name="close" />结束</button>
      </div>
    </template>
    <template #empty>
      <EmptyState icon-name="cpu" title="没有可见进程" />
    </template>
  </DataTable>

  <div class="note mt12">
    <b>提示</b>：结束进程发送 SIGKILL。runit 托管的服务被结束后会自动重启，要停服务请到
    <a href="#" @click.prevent="$emit('go', 'services')">服务管理</a>。
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, fmtB } from '../api'
import { toast, ui } from '../store'
import Icon from '../components/Icon.vue'
import StatCard from '../components/StatCard.vue'
import DataTable from '../components/DataTable.vue'
import EmptyState from '../components/EmptyState.vue'

const data = ref({ list: [], count: 0, rss_total: 0 })
const base = (c) => (c || '').split(' ')[0].split('/').pop()
const rest = (c) => (c || '').split(' ').slice(1).join(' ').slice(0, 90) || '—'
const protectedCount = computed(() => data.value.list.filter(p => p.protected).length)
const avgRss = computed(() => data.value.list.length ? fmtB(Math.round(data.value.rss_total / data.value.list.length)) : '0 B')

const cols = [
  { key: 'command', title: '命令' },
  { key: 'pid', title: 'PID', width: '80px' },
  { key: 'rss', title: '内存', width: '100px', align: 'r' },
  { key: 'stat', title: '状态', width: '90px' },
  { key: 'ops', title: '操作', align: 'r', width: '120px' }
]

async function load() {
  try { data.value = await api('/api/processes') } catch (e) { toast(e.message, 'err') }
}
async function kill(p) {
  if (!confirm(`结束进程 ${p.pid}？该操作不可撤销。`)) return
  try {
    await api(`/api/processes/${p.pid}/kill9`, { method: 'POST' })
    toast(`进程 ${p.pid} 已结束`, 'ok')
    load()
  } catch (e) { toast(e.message, 'err') }
}
onMounted(load)
</script>
