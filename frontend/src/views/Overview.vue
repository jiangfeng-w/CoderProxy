<script setup lang="ts">
// 总览页：快速指标 + 工具映射 + 模型/事件速览。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import { getStats, getModels, getEvents, type MonitorStats, type OaiModel } from '../api'
import { store } from '../store'
import { batchTestAll } from '../modelCheck'

const message = useMessage()
const stats = ref<MonitorStats>({})
const models = ref<OaiModel[]>([])
const recentEvents = ref<{ id: number; ts: string; kind: string; data: Record<string, unknown> }[]>([])

const n = (k: string) => stats.value[k] ?? 0
const requests = computed(() => n('chat_request'))
const done = computed(() => n('chat_done'))
const error = computed(() => n('chat_error'))
const refresh = computed(() => n('auth_401_refresh'))
const hit = computed(() => n('tool_map_hits'))
const longtail = computed(() => n('tool_longtail_passthrough'))
const drop = computed(() => n('tool_dropped'))
const toolTotal = computed(() => hit.value + longtail.value + drop.value || 1)
const pct = (v: number) => Math.round((v / toolTotal.value) * 1000) / 10

let timer: number | undefined
let modelTimer: number | undefined
let eventTimer: number | undefined

async function pollStats() {
  try {
    stats.value = await getStats()
  } catch (e) {
    console.error('[overview] pollStats', e)
  }
}
async function loadModels() {
  try {
    // GUI 用完整目录（all=1），不受白名单过滤，失败模型仍需可见以便重新启用
    models.value = (await getModels(true)).data
    // 登录后首次：模型同步完成后顺序测试一轮（会话内只测一次，跨页面挂载保持）
    if (!store.batchTested && models.value.length > 0) {
      store.batchTested = true
      batchTestAll(models.value).then(failed => {
        if (failed.length > 0) {
          message.warning(`${failed.length} 个模型连接失败`)
        }
      })
    }
  } catch (e) {
    console.error('[overview] loadModels', e)
  }
}
async function loadRecent() {
  try {
    recentEvents.value = (await getEvents(0, 8)).events
  } catch (e) {
    console.error('[overview] loadRecent', e)
  }
}

/** 点击模型名复制到剪贴板。 */
async function onCopy(id: string) {
  try {
    await navigator.clipboard.writeText(id)
    message.success(`已复制：${id}`)
  } catch (e) {
    message.error(`复制失败：${String(e)}`)
  }
}

const kindMeta: Record<string, [string, string]> = {
  chat_request: ['请求', 'cyan'],
  chat_done: ['完成', 'green'],
  chat_error: ['错误', 'red'],
  auth_401_refresh: ['401刷新', 'yellow']
}

function fmtTs(iso: string) {
  return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false })
}
function detailOf(e: { kind: string; data: Record<string, unknown> }): string {
  const d = e.data
  const stream = d.stream ? ' · 流式' : ''
  switch (e.kind) {
    case 'chat_request':
      return `工具 ${d.tools ?? 0} 个${stream}`
    case 'chat_done':
      return `转换 ${d.map_hits ?? 0} · 转发 ${d.longtail ?? 0} · 忽略 ${d.dropped ?? 0}${stream}`
    case 'chat_error':
      return String(d.error ?? '')
    case 'auth_401_refresh':
      return `模型 ${d.model ?? ''} 上游返回 401，已自动刷新重试`
    default:
      return ''
  }
}

onMounted(() => {
  pollStats()
  loadModels()
  loadRecent()
  timer = window.setInterval(pollStats, 2000)
  modelTimer = window.setInterval(loadModels, 5000)
  eventTimer = window.setInterval(loadRecent, 3000)
})

onUnmounted(() => {
  clearInterval(timer)
  clearInterval(modelTimer)
  clearInterval(eventTimer)
})
</script>

<template>
  <div>
    <!-- 合规警示（M5：开发计划 §10 风险 1） -->
    <div class="warn-banner">
      <span class="warn-icon">⚠</span>
      <span> 本工具通过非官方方式接入牛码，存在<strong>封号风险</strong>。请务必使用<strong>小号</strong>登录验证，谨慎对待生产账号；使用本工具导致的账号风控/封禁由使用者自行承担。 </span>
    </div>

    <div class="grid2-fixed">
      <!-- 快速指标 -->
      <div class="card">
        <div class="card-title">快速指标</div>
        <div class="kpi-grid">
          <div class="kpi">
            <div
              class="kpi-num"
              style="color: var(--cp-cyan)"
              >{{ requests.toLocaleString() }}</div
            >
            <div class="kpi-label">请求数</div>
          </div>
          <div class="kpi">
            <div
              class="kpi-num"
              style="color: var(--cp-green)"
              >{{ done.toLocaleString() }}</div
            >
            <div class="kpi-label">完成数</div>
          </div>
          <div class="kpi">
            <div
              class="kpi-num"
              style="color: var(--cp-red)"
              >{{ error.toLocaleString() }}</div
            >
            <div class="kpi-label">错误数</div>
          </div>
          <div class="kpi">
            <div
              class="kpi-num"
              style="color: var(--cp-yellow)"
              >{{ refresh.toLocaleString() }}</div
            >
            <div class="kpi-label">401刷新次数</div>
          </div>
        </div>
      </div>

      <!-- 工具映射 -->
      <div class="card">
        <div class="card-title">工具映射（累计）</div>
        <div class="tool-bar">
          <div class="tool-row">
            <span class="t-label">已转换</span>
            <div class="t-track"
              ><div
                class="t-fill cyan"
                :style="{ width: pct(hit) + '%' }"
            /></div>
            <span class="t-num mono">{{ hit.toLocaleString() }} · {{ pct(hit) }}%</span>
          </div>
          <div class="tool-row">
            <span class="t-label">原样转发</span>
            <div class="t-track"
              ><div
                class="t-fill orange"
                :style="{ width: pct(longtail) + '%' }"
            /></div>
            <span class="t-num mono">{{ longtail.toLocaleString() }} · {{ pct(longtail) }}%</span>
          </div>
          <div class="tool-row">
            <span class="t-label">已忽略</span>
            <div class="t-track"
              ><div
                class="t-fill red"
                :style="{ width: pct(drop) + '%' }"
            /></div>
            <span class="t-num mono">{{ drop.toLocaleString() }} · {{ pct(drop) }}%</span>
          </div>
        </div>
      </div>
    </div>

    <div class="grid2">
      <!-- 模型速览 -->
      <div class="card">
        <div class="card-title">
          已同步模型
          <span class="card-extra"><a @click="store.view = 'models'">前往模型页管理 →</a></span>
        </div>
        <div
          v-if="models.length === 0"
          class="empty"
          >暂无模型，请先登录并同步</div
        >
        <table
          v-else
          class="tbl"
        >
          <thead>
            <tr><th>模型名</th><th class="r">状态</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="m in models.slice(0, 6)"
              :key="m.id"
            >
              <td
                class="mono copyable"
                title="点击复制模型名"
                @click="onCopy(m.id)"
                >{{ m.id }}</td
              >
              <td class="r">
                <span
                  v-if="store.modelStatus[m.id] === 'success'"
                  class="cp-tag green"
                  >成功</span
                >
                <span
                  v-else-if="store.modelStatus[m.id] === 'testing'"
                  class="cp-tag gray"
                  >测试中</span
                >
                <span
                  v-else-if="store.modelStatus[m.id] === 'failure'"
                  class="cp-tag red"
                  >失败</span
                >
                <span
                  v-else
                  class="cp-tag gray"
                  >未测试</span
                >
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 事件速览 -->
      <div class="card">
        <div class="card-title">
          事件速览（最近 8 条）
          <span class="card-extra"><a @click="store.view = 'logs'">前往日志页查看全部 →</a></span>
        </div>
        <div
          v-if="recentEvents.length === 0"
          class="empty"
          >暂无事件</div
        >
        <table
          v-else
          class="tbl"
        >
          <thead>
            <tr><th>时间</th><th>类型</th><th>详情</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="e in recentEvents"
              :key="e.id"
            >
              <td class="mono dim">{{ fmtTs(e.ts) }}</td>
              <td>
                <span
                  v-if="kindMeta[e.kind]"
                  class="cp-tag"
                  :class="kindMeta[e.kind][1]"
                  >{{ kindMeta[e.kind][0] }}</span
                >
                <span
                  v-else
                  class="cp-tag gray"
                  >{{ e.kind }}</span
                >
              </td>
              <td class="mono dim detail">{{ detailOf(e) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.warn-banner {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  background: rgba(234, 179, 8, 0.1);
  border: 1px solid rgba(234, 179, 8, 0.4);
  color: #fde68a;
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 14px;
  font-size: 13px;
  line-height: 1.6;
}
.warn-icon {
  color: var(--cp-yellow);
  flex-shrink: 0;
}
.warn-banner strong {
  color: var(--cp-yellow);
}
.card {
  background: var(--cp-panel);
  border: 1px solid var(--cp-border);
  border-radius: 10px;
  padding: 14px 16px;
  margin-bottom: 14px;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
  display: flex;
  align-items: center;
}
.card-extra {
  margin-left: auto;
  font-weight: 400;
  font-size: 13px;
}
.card-extra a {
  color: var(--cp-cyan);
  cursor: pointer;
  text-decoration: none;
}
.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 1200px) {
  .grid2 {
    grid-template-columns: 1fr;
  }
}
.grid2-fixed {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
.kv-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
}
.kv .k {
  color: var(--cp-dim);
  font-size: 13px;
  margin-right: 8px;
}
.kv .v {
  font-size: 14px;
}
.dot.ok {
  background: var(--cp-green);
  box-shadow: 0 0 6px var(--cp-green);
}
.dot.off {
  background: #475569;
}
.kpi-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.kpi {
  background: var(--cp-panel-2);
  border-radius: 8px;
  padding: 12px 14px;
}
.kpi-label {
  color: var(--cp-dim);
  font-size: 13px;
  margin-top: 2px;
}
.tool-bar {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-top: 4px;
}
.tool-row {
  display: grid;
  grid-template-columns: 140px 1fr 120px;
  align-items: center;
  gap: 10px;
}
.t-label {
  color: var(--cp-text);
  font-size: 13px;
}
.t-track {
  height: 8px;
  background: var(--cp-panel-2);
  border-radius: 4px;
  overflow: hidden;
}
.t-fill {
  height: 100%;
  border-radius: 4px;
}
.t-fill.cyan {
  background: var(--cp-cyan);
}
.t-fill.orange {
  background: var(--cp-orange);
}
.t-fill.red {
  background: var(--cp-red);
}
.t-num {
  text-align: right;
  color: var(--cp-dim);
  font-size: 12px;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.tbl th {
  text-align: left;
  color: var(--cp-dim);
  font-weight: 500;
  padding: 6px 8px;
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 6px 8px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
}
.tbl .r {
  text-align: right;
}
.dim {
  color: var(--cp-dim);
}
.detail {
  font-size: 12px;
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.empty {
  color: var(--cp-dim);
  padding: 20px 0;
  text-align: center;
}
.copyable {
  cursor: pointer;
  user-select: text;
  transition: color 0.15s;
}
.copyable:hover {
  color: var(--cp-cyan);
}
.cp-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
}
.cp-tag.green {
  background: rgba(34, 197, 94, 0.2);
  color: var(--cp-green);
}
.cp-tag.red {
  background: rgba(239, 68, 68, 0.2);
  color: var(--cp-red);
}
.cp-tag.gray {
  background: rgba(100, 116, 139, 0.2);
  color: var(--cp-dim);
}
</style>
