<script setup lang="ts">
// 日志页（M7）：历史日志视图，数据源 = /v1/logs（SQLite）。
// 近实时 = 2.5s 轮询当前页（暂停停轮询）；筛选（类型/模型/时间区间）可组合；分页 + 清空。
// 交互控件统一 Naive UI（dark 主题），类型标签用全局 cp-tag 配色。
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { NButton, NDatePicker, NEmpty, NPagination, NPopconfirm, NSelect, NSpin, useMessage } from 'naive-ui'
import { fetchLogs, fetchLogKinds, fetchLogModels, deleteLogs, getModels, type LogRow, type LogsQuery } from '../api'
import { STANDARD_EFFORT_LABELS } from '../store'

const message = useMessage()
const POLL_MS = 2500

// ── 类型元信息（M6 落库 kind；配色沿用全局 cp-tag）──
const kindMeta: Record<string, { label: string; cls: string }> = {
  chat_request: { label: '请求', cls: 'cyan' },
  chat_done: { label: '完成', cls: 'green' },
  chat_error: { label: '错误', cls: 'red' },
  auth_401_refresh: { label: '401刷新', cls: 'yellow' }
}

const rows = ref<LogRow[]>([])
const total = ref(0)
const kinds = ref<string[]>([])
const models = ref<string[]>([])

const kind = ref<string | null>(null) // null = 全部类型
const model = ref<string | null>(null) // null = 全部模型
const timeRange = ref<[number, number] | null>(null) // naive 时间戳（ms）
const page = ref(1)
const pageSize = ref(50)

const paused = ref(false)
const loading = ref(false)
const loadError = ref('')
const clearing = ref(false)

let timer: number | undefined

const kindOptions = computed(() => Object.entries(kindMeta).map(([key, m]) => ({ label: m.label, value: key })))
const modelOptions = computed(() => models.value.map(m => ({ label: m, value: m })))

const hasAnyFilter = computed(() => !!(kind.value || model.value || timeRange.value))

/** naive 时间戳（ms）→ UTC ISO，后缀与后端存储一致的 `+00:00`（毫秒）。 */
function toUtcIso(ts: number): string {
  return new Date(ts).toISOString().replace('Z', '+00:00')
}

function filterQuery(): LogsQuery {
  const r = timeRange.value
  return {
    kind: kind.value ?? undefined,
    model: model.value ?? undefined,
    timeFrom: r ? toUtcIso(r[0]) : undefined,
    timeTo: r ? toUtcIso(r[1]) : undefined
  }
}

async function fetchKindsModels() {
  try {
    const [k, m] = await Promise.all([fetchLogKinds(), fetchLogModels()])
    kinds.value = k.kinds
    models.value = m.models
  } catch {
    /* 下拉失败不阻断列表；列表返回同源亦可兜底 */
  }
}

/** 模型 → { 思考档位 level → 上游中文标签 }，日志行展示「低（low）」双写用；拉取失败回退原始值。 */
const effortLabels = ref<Record<string, Record<string, string>>>({})

async function fetchEffortLabels() {
  try {
    const res = await getModels(true)
    const map: Record<string, Record<string, string>> = {}
    for (const m of res.data) {
      if (m.reasoning_labels && Object.keys(m.reasoning_labels).length > 0) map[m.id] = m.reasoning_labels
    }
    effortLabels.value = map
  } catch {
    /* 标签缺失不阻断日志展示，回退显示 level 原值 */
  }
}

async function fetchList(silent = false): Promise<void> {
  if (!silent) loading.value = true
  loadError.value = ''
  try {
    const res = await fetchLogs({
      ...filterQuery(),
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value
    })
    rows.value = res.rows
    total.value = res.total
    if (res.kinds.length) kinds.value = res.kinds
    if (res.models.length) models.value = res.models
    const maxPage = Math.max(1, Math.ceil(res.total / pageSize.value))
    if (page.value > maxPage) {
      page.value = maxPage
      await fetchList(true)
      return
    }
  } catch (e) {
    loadError.value = String(e)
  } finally {
    if (!silent) loading.value = false
  }
}

function onFilterChange() {
  page.value = 1
  void fetchList()
}

function resetFilter() {
  kind.value = null
  model.value = null
  timeRange.value = null
  onFilterChange()
}

function onPageChange() {
  void fetchList()
}

function onSizeChange() {
  page.value = 1
  void fetchList()
}

// ── 行渲染辅助 ──
function fmtTs(iso: string): string {
  const d = new Date(iso)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtDur(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`
}

function kindOf(row: LogRow): { label: string; cls: string } {
  return kindMeta[row.kind] ?? { label: row.kind, cls: 'gray' }
}

function detailOf(row: LogRow): string {
  switch (row.kind) {
    case 'chat_done': {
      const t = row.total_tokens ?? 0
      const d = row.duration_ms ?? 0
      return `共 ${t.toLocaleString()} tokens · 耗时 ${fmtDur(d)}`
    }
    case 'chat_request': {
      const d = row.detail
      const rec = typeof d === 'object' && d ? (d as Record<string, unknown>) : {}
      const tools = Number(rec.tools ?? 0)
      // 思考下发态（relay 兜底/agent 显式传参后的最终值）；无字段=旧日志，不展示。
      // 标签优先用上游自带中文名，目录外档位（透传/标准档补全）回退标准中文名，都没有则显示原值
      const effort = typeof rec.thinking_effort === 'string' ? rec.thinking_effort : ''
      const thinking = rec.thinking === true
      const label = (effort && row.model && effortLabels.value[row.model]?.[effort]) || STANDARD_EFFORT_LABELS[effort] || ''
      const thinkText = effort ? (effort === 'none' ? ' · 思考关' : ` · 思考 ${label ? `${label}（${effort}）` : effort}`) : thinking ? ' · 思考开' : ''
      return `工具 ${tools} 个${row.stream ? ' · 流式' : ''}${thinkText}`
    }
    case 'chat_error': {
      const d = row.detail
      if (typeof d === 'string') return d
      if (d && typeof d === 'object') return String((d as Record<string, unknown>).error ?? '')
      return ''
    }
    case 'auth_401_refresh':
      return '上游返回 401，已自动刷新重试'
    default:
      return ''
  }
}

/** 悬浮显示完整 usage（chat_done 行）。 */
function titleOf(row: LogRow): string | undefined {
  if (row.kind !== 'chat_done') return undefined
  const p = (n?: number | null) => n?.toLocaleString() ?? '—'
  return `prompt ${p(row.prompt_tokens)} · completion ${p(row.completion_tokens)}\n缓存 ${p(row.cached_tokens)} · 推理 ${p(row.reasoning_tokens)} · duration ${row.duration_ms ?? '—'}ms`
}

// ── 清空（按当前筛选条件 = 条件清空，无条件 = 全清）──
const clearScopeText = computed(() => (hasAnyFilter.value ? '当前筛选条件内的全部日志' : '全部日志（不可恢复）'))

async function confirmClear() {
  clearing.value = true
  try {
    const res = await deleteLogs(filterQuery())
    message.success(`已清空 ${res.deleted} 条日志`)
    page.value = 1
    await Promise.all([fetchList(), fetchKindsModels()])
  } catch (e) {
    message.error(String(e))
  } finally {
    clearing.value = false
  }
}

// ── 生命周期：首次加载 + 近实时轮询 ──
onMounted(() => {
  void fetchKindsModels()
  void fetchEffortLabels()
  void fetchList()
  timer = window.setInterval(() => {
    if (!paused.value && !loading.value) void fetchList(true)
  }, POLL_MS)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="card">
      <div class="head">
        <span class="card-title">请求日志</span>
        <span class="spacer" />
        <span
          class="live mono"
          :class="{ off: paused }"
        >
          <span class="live-dot" />
          {{ paused ? '已暂停' : '自动刷新' }}
        </span>
      </div>

      <div class="filters">
        <n-select
          v-model:value="kind"
          class="ctl"
          :options="kindOptions"
          placeholder="全部类型"
          clearable
          size="small"
          style="width: 150px"
          @update:value="onFilterChange"
        />
        <n-select
          v-model:value="model"
          class="ctl"
          :options="modelOptions"
          placeholder="全部模型"
          clearable
          filterable
          size="small"
          style="width: 220px"
          @update:value="onFilterChange"
        />
        <n-date-picker
          v-model:value="timeRange"
          class="ctl"
          type="datetimerange"
          clearable
          size="small"
          style="width: 396px"
          :update-value-on-close="true"
          @update:value="onFilterChange"
        />
        <n-button
          size="small"
          quaternary
          @click="resetFilter"
          >重置筛选</n-button
        >

        <div class="spacer" />
        <n-button
          size="small"
          :type="paused ? 'primary' : 'default'"
          @click="paused = !paused"
        >
          {{ paused ? '继续' : '暂停' }}
        </n-button>
        <n-popconfirm
          :positive-button-props="{ size: 'small', type: 'error' }"
          :negative-button-props="{ size: 'small' }"
          positive-text="确认清空"
          negative-text="取消"
          @positive-click="confirmClear"
        >
          <template #trigger>
            <n-button
              size="small"
              type="error"
              secondary
              :loading="clearing"
              >清空日志</n-button
            >
          </template>
          将删除{{ clearScopeText }}，该操作不可恢复。
        </n-popconfirm>
      </div>

      <div
        v-if="loadError"
        class="err-banner"
        >加载失败：{{ loadError }}</div
      >

      <div class="log-wrap">
        <n-spin :show="loading">
          <table class="tbl">
            <thead>
              <tr>
                <th class="w-time">时间</th>
                <th class="w-type">类型</th>
                <th class="w-model">模型</th>
                <th>详情</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in rows"
                :key="row.id"
              >
                <td class="mono dim time-cell">{{ fmtTs(row.ts) }}</td>
                <td>
                  <span
                    class="cp-tag"
                    :class="kindOf(row).cls"
                    >{{ kindOf(row).label }}</span
                  >
                </td>
                <td
                  class="mono dim model-cell"
                  :title="row.model || ''"
                  >{{ row.model || '—' }}</td
                >
                <td
                  class="mono dim detail"
                  :title="titleOf(row)"
                  >{{ detailOf(row) }}</td
                >
              </tr>
            </tbody>
          </table>
          <div
            v-if="!loading && !loadError && rows.length === 0"
            class="empty-wrap"
          >
            <n-empty
              size="small"
              description="暂无日志"
            />
          </div>
        </n-spin>
      </div>

      <div class="footer">
        <div class="summary mono">
          <span>本页 {{ rows.length }} 条</span>
          <span class="sep" />
          <span>共 {{ total.toLocaleString() }} 条</span>
        </div>
        <n-pagination
          v-model:page="page"
          v-model:page-size="pageSize"
          :item-count="total"
          :page-sizes="[20, 50, 100]"
          show-size-picker
          size="small"
          @update:page="onPageChange"
          @update:page-size="onSizeChange"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.card {
  background: var(--cp-panel);
  border: 1px solid var(--cp-border);
  border-radius: 10px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  height: 100%;
}
.head {
  display: flex;
  align-items: center;
  margin-bottom: 10px;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
}
.spacer {
  flex: 1;
}
.live {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--cp-green);
}
.live.off {
  color: var(--cp-yellow);
}
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: 0 0 6px currentColor;
}
.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.err-banner {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: var(--cp-red);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  margin-bottom: 8px;
}
.log-wrap {
  flex: 1;
  overflow: auto;
  max-height: 560px;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  min-height: 120px;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.tbl th {
  position: sticky;
  top: 0;
  z-index: 1;
  text-align: left;
  color: var(--cp-dim);
  font-weight: 500;
  padding: 9px 10px;
  background: var(--cp-panel-2);
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 8px 10px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
  vertical-align: top;
  line-height: 1.55;
}
.tbl tbody tr {
  transition: background 0.12s;
}
.tbl tbody tr:hover {
  background: rgba(51, 65, 85, 0.35);
}
.tbl tbody tr:last-child td {
  border-bottom: none;
}
.w-time {
  width: 130px;
}
.time-cell {
  white-space: nowrap;
}
.w-type {
  width: 96px;
}
.w-model {
  width: 210px;
}
.model-cell {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.detail {
  font-size: 12px;
  color: #94a3b8;
  word-break: break-all;
}
.empty-wrap {
  padding: 26px 0;
}
.footer {
  margin-top: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.summary {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--cp-dim);
  font-size: 12px;
  white-space: nowrap;
}
.summary .sep {
  width: 1px;
  height: 12px;
  background: var(--cp-border);
}
</style>
