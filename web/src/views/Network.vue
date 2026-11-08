<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="network" />网络</h2>
      <p>网卡地址与本地端口连通性探测</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />重新探测</button>
    </div>
  </div>

  <DataTable :cols="cols" :rows="data.ifaces" title="网络接口" icon-name="network" :row-key="f => f.name"
             empty-text="未读到网络接口">
    <template #c-name="{ row }">
      <div class="tname mono">{{ row.name }}</div>
      <div class="tsub">{{ (row.flags || '').includes('UP') ? '已启用' : '未启用' }}</div>
    </template>
    <template #c-addr="{ row }">
      <span class="mono nowrap">{{ row.addr || '—' }}</span>
    </template>
    <template #c-mac="{ row }">
      <span class="mono dim nowrap">{{ row.mac || '系统限制不可读' }}</span>
    </template>
    <template #c-mtu="{ row }">
      <span class="mono dim">{{ row.mtu || '—' }}</span>
    </template>
    <template #c-state="{ row }">
      <span class="state" :class="(row.flags || '').includes('UP') ? 'run' : 'stop'">
        <i class="dot" :class="{ off: !(row.flags || '').includes('UP') }"></i>
        {{ (row.flags || '').includes('UP') ? '已启用' : '未启用' }}
      </span>
    </template>
  </DataTable>

  <div class="card flush mt12">
    <div class="card-h">
      <Icon name="globe" />
      <h3>本地端口探测</h3>
      <div class="sub">
        <span class="tag" :class="open ? 'g' : ''">{{ open }} / {{ (data.ports || []).length }} 个开放</span>
      </div>
    </div>
    <div class="card-b">
      <div class="pgrid">
        <div v-for="p in data.ports" :key="p.port" class="pcell" :class="{ open: p.open }">
          <b>{{ p.port }}</b><span>{{ p.hint }}{{ p.open ? ' · 开放' : ' · 关闭' }}</span>
        </div>
      </div>
      <div class="note mt12">
        <b>说明</b>：Android 禁止普通应用读取 /proc/net，无法列出监听端口。这里对 127.0.0.1 的常用端口
        发起 TCP 连接探测，作为「端口是否有人在用」的近似判断。
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import DataTable from '../components/DataTable.vue'

const data = ref({ ifaces: [], ports: [] })
const open = computed(() => (data.value.ports || []).filter(p => p.open).length)

const cols = [
  { key: 'name', title: '接口' },
  { key: 'addr', title: 'IPv4' },
  { key: 'mac', title: 'MAC' },
  { key: 'mtu', title: 'MTU', width: '90px' },
  { key: 'state', title: '状态', width: '110px' }
]

async function load() {
  try { data.value = await api('/api/network') } catch (e) { toast(e.message, 'err') }
}
onMounted(load)
</script>
