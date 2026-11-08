<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="server" />服务管理</h2>
      <p>runit 托管的服务目录：{{ list.length }} 个服务 · $PREFIX/var/service</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />刷新</button>
    </div>
  </div>

  <DataTable :cols="cols" :rows="list" title="服务列表" icon-name="server" :row-key="s => s.name"
             empty-text="未发现服务目录">
    <template #toolbar>
      <span class="tag" :class="runCount === list.length && list.length ? 'g' : 'y'">{{ runCount }} / {{ list.length }} 运行中</span>
    </template>
    <template #c-name="{ row }">
      <div class="tname">{{ row.name }}</div>
      <div class="tsub mono">{{ row.name === 'pocketpanel' || row.name === 'pocketpanel-py' ? '面板自身' : 'runit' }}</div>
    </template>
    <template #c-status="{ row }">
      <span class="state" :class="row.status === 'run' ? 'run' : row.status === 'fail' ? 'fail' : 'stop'">
        <i class="dot" :class="{ off: row.status !== 'run', fail: row.status === 'fail' }"></i>
        {{ row.status === 'run' ? '运行中' : row.status === 'fail' ? '失败' : '已停止' }}
      </span>
    </template>
    <template #c-pid="{ row }">
      <span class="mono dim">{{ row.pid || '—' }}</span>
    </template>
    <template #c-uptime="{ row }">
      <span class="dim nowrap">{{ row.uptime || '—' }}</span>
    </template>
    <template #c-ops="{ row }">
      <div class="tbl-ops">
        <span v-if="row.name === 'pocketpanel' || row.name === 'pocketpanel-py'" class="tag b">面板自身</span>
        <template v-else>
          <button class="btn xs" @click.stop="act(row, 'restart')"><Icon name="restart" />重启</button>
          <button class="btn xs" :class="row.status === 'run' ? 'danger' : 'success'"
                  @click.stop="act(row, row.status === 'run' ? 'stop' : 'start')">
            <Icon :name="row.status === 'run' ? 'stop' : 'play'" />{{ row.status === 'run' ? '停止' : '启动' }}
          </button>
        </template>
      </div>
    </template>
  </DataTable>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import DataTable from '../components/DataTable.vue'

const list = ref([])
const runCount = computed(() => list.value.filter(s => s.status === 'run').length)

const cols = [
  { key: 'name', title: '服务' },
  { key: 'status', title: '状态' },
  { key: 'pid', title: 'PID', width: '90px' },
  { key: 'uptime', title: '运行时长', width: '130px' },
  { key: 'ops', title: '操作', align: 'r', width: '190px' }
]

async function load() {
  try { list.value = await api('/api/services') } catch (e) { toast(e.message, 'err') }
}
async function act(s, a) {
  try {
    await api(`/api/services/${encodeURIComponent(s.name)}/${a}`, { method: 'POST' })
    toast(`${s.name} ${{ start: '已启动', stop: '已停止', restart: '已重启' }[a]}`, 'ok')
    setTimeout(load, 500)
  } catch (e) { toast(e.message, 'err') }
}
onMounted(load)
</script>
