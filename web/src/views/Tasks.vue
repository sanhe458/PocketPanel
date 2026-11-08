<template>
  <div class="page-head">
    <div class="ph-t">
      <h2><Icon name="list" />任务队列</h2>
      <p>压缩、解压、远程下载等后台操作的执行状态与输出</p>
    </div>
    <div class="ph-ops">
      <button class="btn" @click="load"><Icon name="refresh" />刷新</button>
    </div>
  </div>

  <div class="card flush">
    <div class="card-h">
      <Icon name="activity" />
      <h3>执行记录</h3>
      <div class="sub">
        <span class="tag" :class="running ? 'y' : ''">{{ running ? '运行中' : '空闲' }}</span>
        <span class="fs11 dim">{{ list.length }} 条</span>
      </div>
    </div>
    <div class="list">
      <div v-for="t in list" :key="t.id" class="li" style="align-items:flex-start">
        <i class="dot" :class="{ busy: t.status === 'running', fail: t.status === 'failed', off: t.status === 'canceled' }"
           style="margin-top:7px"></i>
        <div class="li-main">
          <div class="li-t">
            {{ t.title || t.cmd }}
            <span class="tag">{{ t.kind }}</span>
          </div>
          <div class="li-s">{{ fmtFull(t.started) }} · 退出码 {{ t.exit }}</div>
          <div v-if="t.progress > 0 && t.progress < 100" class="bar" style="margin-top:8px;max-width:320px">
            <i :style="{ width: t.progress + '%' }"></i>
          </div>
          <pre v-if="t.output" class="code" style="margin-top:8px;max-height:150px;font-size:11px">{{ t.output }}</pre>
        </div>
        <span class="tag lg" :class="t.status === 'success' ? 'g' : t.status === 'failed' ? 'r' : t.status === 'running' ? 'y' : ''">
          {{ { running: '运行中', success: '完成', failed: '失败', canceled: '已取消' }[t.status] || t.status }}
        </span>
      </div>
      <div v-if="!list.length">
        <EmptyState icon-name="list" title="暂无任务"
                    desc="压缩、解压、远程下载等后台操作会记录在这里" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { api, fmtFull } from '../api'
import { toast } from '../store'
import Icon from '../components/Icon.vue'
import EmptyState from '../components/EmptyState.vue'

const list = ref([])
const running = computed(() => list.value.some(t => t.status === 'running'))

async function load() {
  try { list.value = await api('/api/tasks') } catch (e) { toast(e.message, 'err') }
}
onMounted(load)
</script>
