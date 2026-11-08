<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="log" />日志</h2>
      <p>面板与 runit 服务的运行输出，尾部截断读取</p>
    </div>
    <div class="ph-ops">
      <div class="seg">
        <button v-for="n in [100, 200, 500, 1000]" :key="n" :class="{ on: n === lines }"
                @click="lines = n; tail()">{{ n }} 行</button>
      </div>
      <button class="btn" @click="tail"><Icon name="refresh" />刷新</button>
    </div>
  </div>

  <div class="card flush">
    <div class="card-h">
      <Icon name="list" />
      <h3>日志文件</h3>
      <div class="sub"><span class="fs11">{{ meta }}</span></div>
    </div>
    <div class="card-b tight">
      <div class="row wrap" style="gap:7px">
        <button v-for="l in list" :key="l.name" class="btn sm" :class="{ primary: l.name === cur }"
                @click="pick(l.name)">{{ l.name }}</button>
        <span v-if="!list.length" class="dim fs12">没有可读日志</span>
      </div>
    </div>
    <div class="card-b" style="padding-top:0">
      <pre class="code" style="max-height:62vh;min-height:280px">{{ body }}</pre>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api, fmtB } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'

const list = ref([])
const cur = ref('')
const lines = ref(200)
const body = ref('加载中…')
const meta = ref('')

async function loadList() {
  try {
    list.value = await api('/api/logs')
    if (!cur.value && list.value.length) { cur.value = list.value[0].name; tail() }
  } catch (e) { toast(e.message, 'err') }
}
async function pick(n) { cur.value = n; tail() }
async function tail() {
  if (!cur.value) { body.value = '没有可读的日志文件'; return }
  try {
    const d = await api(`/api/logs/${encodeURIComponent(cur.value)}?lines=${lines.value}`)
    body.value = (d.lines || []).join('\n') || '(空)'
    meta.value = `${cur.value} · ${(d.lines || []).length} 行 · ${fmtB(d.size)}${d.truncated ? ' · 已截断' : ''}`
  } catch (e) { body.value = '读取失败：' + e.message }
}
onMounted(loadList)
</script>
