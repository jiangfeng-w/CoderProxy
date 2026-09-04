<script setup lang="ts">
  // 模型页：列表 + 搜索 + 白名单启用开关 + 全部启用/禁用 + 可用状态检测。
  import { computed, onMounted, onUnmounted, ref } from 'vue'
  import { useMessage } from 'naive-ui'
  import { getModels, getConfig, updateConfig, authSync, type OaiModel, type Config } from '../api'
  import { store } from '../store'
  import { testModel } from '../modelCheck'

  const message = useMessage()
  const DISABLE_ALL = '__none__'

  const models = ref<OaiModel[]>([])
  const config = ref<Config | null>(null)
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

  const wl = computed(() => config.value?.model_whitelist ?? [])

  const filtered = computed(() => {
    const q = search.value.trim().toLowerCase()
    const list = q
      ? models.value.filter(m => m.id.toLowerCase().includes(q) || (m.name ?? '').toLowerCase().includes(q))
      : models.value
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
      config.value = await updateConfig({ model_whitelist: next })
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
    try {
      config.value = await getConfig()
    } catch (e) {
      message.error(String(e))
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
</style>
