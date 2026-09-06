<script setup lang="ts">
// 模型页：列表 + 搜索 + 白名单启用开关 + 全部启用/禁用 + 可用状态检测。
import { computed, h, onMounted, onUnmounted, ref, type VNodeChild } from 'vue'
import { NSelect, NTooltip, useMessage } from 'naive-ui'
import { getModels, authSync, type OaiModel } from '../api'
import { store, useConfigStore, STANDARD_EFFORTS, STANDARD_EFFORT_LABELS } from '../store'
import { testModel } from '../modelCheck'

const message = useMessage()
const config = useConfigStore()
const DISABLE_ALL = '__none__'

const models = ref<OaiModel[]>([])
const search = ref('')
const syncing = ref(false)
const testing = ref<string | null>(null)
let timer: number | undefined

/** 从全局 store 读取模型状态 */
function getStatus(id: string): string {
  return store.modelStatus[id] ?? ''
}

/** 状态胶囊（即「测试」触发器）：按连通状态定配色。 */
function capClass(id: string): string {
  const s = getStatus(id)
  if (s === 'testing') return 'busy'
  return s === 'success' ? 'green' : s === 'failure' ? 'red' : 'gray'
}

/** 状态胶囊文案：展示最近连通结论（测试中态显示进行中）。 */
function capText(id: string): string {
  const s = getStatus(id)
  if (s === 'testing') return '测试中…'
  if (s === 'success') return '成功'
  if (s === 'failure') return '失败'
  return '未测试'
}

/** 状态胶囊 title：明示「点击=测试」，并告知只更新状态、不改白名单。 */
function capTitle(id: string): string {
  const s = getStatus(id)
  if (s === 'testing') return '正在测试…'
  if (s === 'success' || s === 'failure') return '点击重新测试（只更新状态，不改白名单）'
  return '点击测试'
}

const wl = computed(() => config.model_whitelist)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  const list = q ? models.value.filter(m => m.id.toLowerCase().includes(q) || (m.name ?? '').toLowerCase().includes(q)) : models.value
  return list
})

function isEnabled(id: string): boolean {
  const w = wl.value
  if (w.length === 0) return true
  if (w.length === 1 && w[0] === DISABLE_ALL) return false
  return w.includes(id)
}

async function saveWl(next: string[]) {
  try {
    await config.update({ model_whitelist: next })
  } catch (e) {
    message.error(String(e))
  }
}

/** 启用开关：纯白名单成员管理（即时生效）。不再内嵌连通性预检——
 * 「先测后启用」在 probe 落地后与「测试」按钮重复；诊断职责收敛到
 * 「测试」按钮与登录/同步后的自动批量测试（见需求连通性测试-白名单豁免）。 */
async function onToggle(id: string, on: boolean) {
  const w = wl.value
  let next: string[]
  if (on) {
    if (w.length === 0) return
    if (w.length === 1 && w[0] === DISABLE_ALL) next = [id]
    else next = w.includes(id) ? w : [...w, id]
  } else {
    if (w.length === 0) {
      next = models.value.map(m => m.id).filter(x => x !== id)
    } else if (w.length === 1 && w[0] === DISABLE_ALL) {
      return
    } else {
      next = w.filter(x => x !== id)
      if (next.length === 0) next = [DISABLE_ALL]
    }
  }
  await saveWl(next)
}

async function onAllEnable() {
  await saveWl([])
  message.success('已全部启用')
}
async function onAllDisable() {
  await saveWl([DISABLE_ALL])
  message.success('已全部禁用')
}

/** 同步模型目录并刷新列表（只拉最新目录，不自动做连通性测试——
 * 测试为显式动作；登录后每会话一次的自动摸底仍由概览页负责）。 */
async function onSync() {
  if (syncing.value) return
  syncing.value = true
  try {
    const res = await authSync()
    message.success(`同步完成：${res.models.length} 个模型`)
    models.value = (await getModels(true)).data
  } catch (e) {
    message.error(String(e))
  } finally {
    syncing.value = false
  }
}

/** 点击模型名即复制到剪贴板。 */
async function onCopy(id: string) {
  try {
    await navigator.clipboard.writeText(id)
    message.success(`已复制模型名：${id}`)
  } catch (e) {
    message.error(`复制失败：${String(e)}`)
  }
}

/** 上下文窗口格式化：200000 → 200K，1048576 → 1M。 */
function fmtContext(w?: number | null): string {
  if (!w || w <= 0) return '—'
  if (w >= 1_000_000) {
    const m = w / 1_000_000
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`
  }
  return `${Math.round(w / 1000)}K`
}

/** 档位强度语义（hover tooltip 用；厂商映射差异见需求文档「思考强度控制与日志」）。 */
const EFFORT_DESC: Record<string, string> = {
  minimal: '最低限度思考',
  low: '轻度思考',
  medium: '中度思考',
  high: '深度思考',
  xhigh: '超深度思考',
  max: '最大强度思考'
}

/** 每个档位选项的 hover 说明：head=一句话概括（首行高亮），body=详细描述（次行起）。
 * 目录档位保证生效；目录外标准档标注不保证生效。
 * 「关」的 GLM-5.3 风险提示仅对 glm-5.3 系列显示（官方文档称该系列不支持关思考）。 */
function effortTip(m: OaiModel, value: string): { head: string; body: string } {
  if (value === 'none') {
    const body = m.id.toLowerCase().startsWith('glm-5.3') ? '注意：GLM-5.3 系列官方不支持关闭思考（直连会报错），牛码网关可能映射为低档位，以实测为准' : 'agent 未传思考参数时下发 thinking: disabled'
    return { head: '显式关闭思考', body }
  }
  const head = EFFORT_DESC[value] ?? value
  const body = (m.reasoning_efforts ?? []).includes(value) ? '上游档位（官方客户端同款选项），保证生效' : '目录外标准档位：上游未列出，牛码网关可能映射到其他档位或报错，不保证生效'
  return { head, body }
}

/** 下拉选项渲染：默认 Naive 样式，仅包一层 tooltip（hover 显示档位说明：首行概括高亮，次行详细描述）。
 * 柯里化以携带当前模型上下文；tooltip 宽度定内容 div 上（NTooltip 的 style 不作用于浮层面板），
 * 用 min-width 强制撑开（仅 max-width 会被父容器 shrink-to-fit 压成窄条）。 */
function effortRenderer(m: OaiModel) {
  return (info: { node: VNodeChild; option: { value?: string | number } }): VNodeChild => {
    const tip = effortTip(m, String(info.option.value ?? ''))
    return h(
      NTooltip,
      { trigger: 'hover', placement: 'left' },
      {
        trigger: () => h('div', null, [info.node]),
        default: () => h('div', { style: 'min-width: 300px; max-width: 440px; line-height: 1.6' }, [h('div', { style: 'color: var(--cp-cyan); font-weight: 600; margin-bottom: 2px' }, tip.head), h('div', null, tip.body)])
      }
    )
  }
}

/** 默认思考强度下拉选项：关（none，显式关思考）置顶；目录档位 + 目录外标准档合并后
 * 统一按强度排序（微/低/中/高/超高/最大），目录外标签标「目录外」，说明在 hover tooltip。 */
function effortOptions(m: OaiModel): { label: string; value: string }[] {
  const labels = m.reasoning_labels ?? {}
  const catalogLevels = m.reasoning_efforts ?? []
  const order = (lv: string) => {
    const i = (STANDARD_EFFORTS as readonly string[]).indexOf(lv)
    return i === -1 ? 99 : i
  }
  const all = [...new Set([...catalogLevels, ...STANDARD_EFFORTS])].sort((a, b) => order(a) - order(b))
  const opts = all.map(lv => (catalogLevels.includes(lv) ? { label: labels[lv] ? `${labels[lv]}（${lv}）` : lv, value: lv } : { label: `${STANDARD_EFFORT_LABELS[lv] ?? lv}（${lv}）·目录外`, value: lv }))
  return [{ label: '关（不思考）', value: 'none' }, ...opts]
}

/** 当前生效的默认档位（缺省 = none 关）。 */
function effortValue(m: OaiModel): string {
  return config.thinking_defaults[m.id] ?? 'none'
}

/** 写入某模型的默认思考强度（整体覆盖写回，收敛走 config store 响应）。 */
async function onEffortChange(m: OaiModel, v: string) {
  try {
    await config.update({ thinking_defaults: { ...config.thinking_defaults, [m.id]: v } })
  } catch (e) {
    message.error(String(e))
  }
}

/** 手动测试单个模型连通性：只更新连通状态，不修改白名单（与启用开关完全解耦）。 */
async function onTest(id: string) {
  if (testing.value) return
  testing.value = id
  store.modelStatus[id] = 'testing'
  try {
    const ok = await testModel(id)
    store.modelStatus[id] = ok ? 'success' : 'failure'
    if (ok) {
      message.success(`模型 ${id} 连通`)
    } else {
      message.error(`模型 ${id} 连接失败`)
    }
  } finally {
    testing.value = null
  }
}

async function load() {
  try {
    // GUI 用完整目录（all=1），失败模型仍需可见，不能按白名单过滤隐藏
    models.value = (await getModels(true)).data
  } catch (e) {
    message.error(String(e))
  }
  if (!config.loaded) {
    // 配置真值走 useConfigStore（Shell 就绪轮询会装；此处兜底，保证白名单开关可用）
    try {
      await config.load()
    } catch (e) {
      message.error(String(e))
    }
  }
}

onMounted(() => {
  load()
  // 空列表时轮询，感知 Shell 登录后自动同步的结果
  timer = window.setInterval(() => {
    if (models.value.length === 0) load()
  }, 3000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div>
    <div class="card">
      <div class="toolbar">
        <input
          v-model="search"
          class="search"
          placeholder="搜索模型名"
        />
        <button
          class="btn primary"
          :disabled="syncing"
          @click="onSync"
        >
          同步模型
        </button>
        <button
          class="btn primary"
          @click="onAllEnable"
        >
          全部启用
        </button>
        <button
          class="btn danger"
          @click="onAllDisable"
        >
          全部禁用
        </button>
        <span class="count mono">已启用 {{ models.filter(m => isEnabled(m.id)).length }}/{{ models.length }}</span>
      </div>

      <div
        v-if="filtered.length === 0"
        class="empty"
      >
        暂无模型，请先登录并同步模型目录
      </div>
      <table
        v-else
        class="tbl"
      >
        <thead>
          <tr>
            <th>模型名</th>
            <th class="col-ctx">上下文</th>
            <th class="col-effort">默认思考强度</th>
            <th class="col-status">状态（点击测试）</th>
            <th class="col-toggle">启用</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="m in filtered"
            :key="m.id"
          >
            <td
              class="mono copyable"
              title="点击复制模型名"
              @click="onCopy(m.id)"
            >
              {{ m.id }}
            </td>
            <td class="mono dim col-ctx">
              {{ fmtContext(m.context_window) }}
            </td>
            <td class="col-effort">
              <NSelect
                v-if="(m.reasoning_efforts ?? []).length > 0"
                size="small"
                :value="effortValue(m)"
                :options="effortOptions(m)"
                :render-option="effortRenderer(m)"
                :consistent-menu-width="false"
                title="agent 未传思考参数时按此默认值下发；hover 各选项查看档位说明（仅上游目录档位保证生效）"
                @update:value="(v: string) => onEffortChange(m, v)"
              />
              <span
                v-else
                class="dim"
                >{{ m.supports_reasoning ? '支持（无档位）' : '—' }}</span
              >
            </td>
            <td class="col-status">
              <div class="st">
                <button
                  class="cap"
                  :class="capClass(m.id)"
                  :disabled="testing !== null"
                  :title="capTitle(m.id)"
                  @click="onTest(m.id)"
                >
                  {{ capText(m.id) }}
                </button>
                <button
                  v-if="getStatus(m.id) === 'success' && !isEnabled(m.id)"
                  class="hint"
                  title="连通性测试已通过但未在白名单，点击立即启用"
                  @click="onToggle(m.id, true)"
                >
                  点击启用
                </button>
                <button
                  v-else-if="getStatus(m.id) === 'failure' && isEnabled(m.id)"
                  class="hint danger"
                  title="测试失败但该模型仍在启用中，点击停用"
                  @click="onToggle(m.id, false)"
                >
                  点击停用
                </button>
              </div>
            </td>
            <td class="col-toggle">
              <label class="switch">
                <input
                  type="checkbox"
                  :checked="isEnabled(m.id)"
                  @change="onToggle(m.id, ($event.target as HTMLInputElement).checked)"
                />
                <span class="slider" />
              </label>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.card {
  background: var(--cp-panel);
  border: 1px solid var(--cp-border);
  border-radius: 10px;
  padding: 14px 16px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.search {
  width: 260px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-text);
  padding: 7px 10px;
}
.count {
  margin-left: auto;
  color: var(--cp-dim);
  font-size: 13px;
}
.btn {
  padding: 6px 14px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid var(--cp-border);
  background: transparent;
  color: var(--cp-text);
  cursor: pointer;
  transition:
    border-color 0.15s,
    color 0.15s,
    background 0.15s;
}
.btn:hover {
  border-color: var(--cp-cyan);
  color: var(--cp-cyan);
}
.btn.primary {
  color: var(--cp-cyan);
  border-color: rgba(34, 211, 238, 0.5);
}
.btn.primary:hover {
  border-color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.1);
}
.btn.danger {
  color: var(--cp-red);
  border-color: rgba(239, 68, 68, 0.5);
}
.btn.danger:hover {
  border-color: var(--cp-red);
  background: rgba(239, 68, 68, 0.1);
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
  padding: 8px;
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 8px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
}
.dim {
  color: var(--cp-dim);
}
.copyable {
  cursor: pointer;
  user-select: text;
  transition: color 0.15s;
}
.copyable:hover {
  color: var(--cp-cyan);
}
.cap {
  padding: 2px 10px;
  border-radius: 4px;
  border: 1px solid transparent;
  font-size: 12px;
  line-height: 16px;
  cursor: pointer;
  transition:
    filter 0.15s,
    border-color 0.15s,
    opacity 0.15s;
}
.cap.green {
  background: rgba(34, 197, 94, 0.2);
  color: var(--cp-green);
}
.cap.red {
  background: rgba(239, 68, 68, 0.2);
  color: var(--cp-red);
}
.cap.busy {
  background: rgba(34, 211, 238, 0.2);
  color: var(--cp-cyan);
}
.cap.gray {
  background: rgba(100, 116, 139, 0.2);
  color: var(--cp-dim);
}
.cap:hover:not(:disabled) {
  border-color: currentColor;
  filter: brightness(1.2);
}
.cap:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.st {
  display: inline-flex;
  align-items: center;
  justify-content: flex-start;
  gap: 4px;
  white-space: nowrap;
}
.hint {
  padding: 0;
  border: none;
  background: none;
  color: var(--cp-cyan);
  opacity: 0.85;
  font-size: 12px;
  line-height: 16px;
  white-space: nowrap;
  cursor: pointer;
  transition: opacity 0.15s;
  text-decoration: underline;
  text-decoration-color: transparent;
  text-underline-offset: 3px;
}
.hint:hover {
  opacity: 1;
  text-decoration-color: currentColor;
}
.hint.danger {
  color: var(--cp-red);
}
.empty {
  color: var(--cp-dim);
  padding: 24px 0;
  text-align: center;
}
.switch {
  position: relative;
  display: inline-block;
  width: 36px;
  height: 20px;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}
.slider {
  position: absolute;
  inset: 0;
  background: #334155;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s;
}
.slider::before {
  content: '';
  position: absolute;
  width: 14px;
  height: 14px;
  left: 3px;
  top: 3px;
  border-radius: 50%;
  background: #cbd5e1;
  transition: transform 0.15s;
}
.switch input:checked + .slider {
  background: var(--cp-cyan);
}
.switch input:checked + .slider::before {
  transform: translateX(16px);
  background: #04121a;
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
.col-status {
  width: 124px;
}
.col-toggle {
  width: 60px;
}
.col-ctx {
  width: 72px;
  white-space: nowrap;
}
.col-effort {
  width: 150px;
}
</style>
