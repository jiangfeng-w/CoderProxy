<script setup lang="ts">
// 配置页：本地 API Key / 工具伪装模式 / 服务端口。读写全部走 useConfigStore——
// 后端「校验→落盘→改运行值」成功后返回完整 config，由 store 整体回写，页面即展示
// 当前真值（顶栏模式/其它页面随同一 store 自动收敛，不再各自维护副本）。
import { onMounted, ref, watch } from 'vue'
import { useMessage, NPopconfirm } from 'naive-ui'
import { relayRestart, relayStatus } from '../api'
import { configStore, toolModeLabel } from '../store'

const message = useMessage()

const config = configStore
const showKey = ref(false)
const busy = ref(false)
// 端口草稿（本地，不写 store）：输入即生效会让「配置端口 vs 实际监听」提示行在
// **未保存**时就误报「被占用」，且丢掉「存了什么」的唯一真值口径。
const portDraft = ref(String(config.port))
const portBusy = ref(false)

// store 真值变化（装载 / 保存回执 / 重启后重读）→ 同步草稿
watch(
  () => config.port,
  p => {
    portDraft.value = String(p)
  }
)

const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

/**
 * 等壳把 relay 重启完成并重读真值。
 *
 * `relay_restart` 是**后台重启**（壳侧线程 stop+spawn 后立即返回），前端只能轮询
 * `relay_status` 感知：先看到侧车下线（running=false）再看到新进程就绪（running=true
 * 且端口变化）才算重启完成。随后重读配置，并用「顺延只向上」的不变量校验——
 * 起点是配置端口、占用只往高顺延，故实际监听必然 ≥ 配置端口；读到更小的值说明
 * 联的还是重启前的旧进程，继续等（这就是上一版误报「13601 被占用，顺延到 3601」的根因）。
 */
async function reloadAfterRestart(configured: number, oldPort: number | undefined, timeoutMs = 30000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs
  let sawDown = false
  while (Date.now() < deadline) {
    const st = await relayStatus().catch(() => null)
    if (st && !st.running) sawDown = true
    if (st && st.running && (sawDown || st.port !== oldPort)) {
      try {
        await config.load()
        // 一致性双检：① 壳报的就绪端口 == relay 报的实际监听（读到旧进程时二者
        // 必不同）；② 实际监听 ≥ 配置端口（顺延只向上）。两条都过才算读到真值。
        if (config.listen_port === st.port && config.listen_port >= configured) return true
      } catch {
        /* 未就绪，下轮重试 */
      }
    }
    await sleep(400)
  }
  return false
}

async function load() {
  try {
    await config.load()
  } catch (e) {
    message.error(String(e))
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success('已复制到剪贴板')
  } catch {
    message.error('复制失败')
  }
}

async function onResetKey() {
  busy.value = true
  try {
    const cfg = await config.update({ regenerate_api_key: true })
    void copyText(cfg.api_key)
  } catch (e) {
    message.error(String(e))
  } finally {
    busy.value = false
  }
}

async function onToolMode(mode: string) {
  try {
    await config.update({ tool_mode: mode })
    message.success(`已切换为「${toolModeLabel(mode)}」`)
  } catch (e) {
    message.error(String(e))
  }
}

async function onApplyPort() {
  const p = Number(portDraft.value)
  if (!Number.isInteger(p) || p < 1 || p > 65535) {
    message.error('端口需在 1-65535 之间')
    return
  }
  portBusy.value = true
  try {
    // 1) 保存「配置端口」到 relay（落盘持久化）；回执里的 listen_port 仍是当前
    //    实际监听（重启前），故此时 store 里 port ≠ listen_port 属正常过渡态
    try {
      await config.update({ port: p })
    } catch (e) {
      message.error(`端口保存失败：${String(e)}`)
      return
    }
    message.info(`已保存端口 ${p}`)
    const oldPort = config.listen_port
    // 2) 触发重启；实际重启在壳侧后台线程执行，靠轮询状态等待完成
    try {
      await relayRestart()
    } catch (e) {
      message.error(`重启服务失败：${String(e)}`)
      return
    }
    // 3) 等新进程就绪后重读真值（被占用时实际端口向上顺延，listen_port 才是真值）
    if (!(await reloadAfterRestart(p, oldPort))) {
      message.warning('已触发重启，但未读到新的监听端口；请稍后刷新页面确认')
      return
    }
    if (config.listen_port === p) {
      message.success(`已重启服务，监听端口 ${p}`)
    } else {
      message.warning(`端口 ${p} 被占用，已自动顺延到 ${config.listen_port}，请按实际端口连接`)
    }
  } finally {
    portBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <!-- 本地 API Key -->
    <div class="card">
      <div class="card-title">本地 API Key</div>
      <div class="field">
        <label class="lbl">本地 API Key（在 Agent 里添加自定义模型时，填在 API Key 一栏）</label>
        <div class="row">
          <div class="key-input-wrap">
            <input
              class="mono key-input"
              :type="showKey ? 'text' : 'password'"
              :value="config.api_key"
              readonly
            />
            <button
              class="key-eye"
              @click="showKey = !showKey"
              :title="showKey ? '隐藏' : '显示'"
            >
              <svg
                v-if="showKey"
                viewBox="0 0 24 24"
                width="18"
                height="18"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" />
                <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" />
                <line
                  x1="1"
                  y1="1"
                  x2="23"
                  y2="23"
                />
              </svg>
              <svg
                v-else
                viewBox="0 0 24 24"
                width="18"
                height="18"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                stroke-linecap="round"
                stroke-linejoin="round"
              >
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                <circle
                  cx="12"
                  cy="12"
                  r="3"
                />
              </svg>
            </button>
          </div>
          <button
            class="btn primary"
            @click="copyText(config.api_key)"
            >复制</button
          >
          <n-popconfirm
            positive-text="确认重置"
            negative-text="取消"
            @positive-click="onResetKey"
          >
            <template #trigger>
              <button
                class="btn danger"
                :disabled="busy"
                >重置</button
              >
            </template>
            重置将生成新密钥并作废旧值，已连接的 Agent 需改用新密钥。
          </n-popconfirm>
        </div>
        <div class="hint warn">密钥仅保存在本机，牛码账号的真实登录信息不会发给任何 Agent。</div>
      </div>
    </div>

    <!-- 工具伪装模式 -->
    <div class="card">
      <div class="card-title">工具伪装模式</div>
      <div class="field">
        <label class="lbl">Agent 工具名 ↔ 牛码原生工具名的映射策略</label>
        <div class="mode-options">
          <label
            v-for="m in [
              { key: 'hybrid', name: '智能适配（推荐）', desc: '认识的工具自动转换成牛码能理解的名称，不认识的按原名转发' },
              { key: 'strict', name: '严格模式', desc: '只转换认识的工具，不认识的直接丢弃' },
              { key: 'passthrough', name: '原样转发', desc: '所有工具按原名转发，不做任何转换' }
            ]"
            :key="m.key"
            class="mode-opt"
            :class="{ active: config.tool_mode === m.key }"
            @click="onToolMode(m.key)"
          >
            <span
              class="radio"
              :class="{ on: config.tool_mode === m.key }"
            />
            <div>
              <div class="mode-name mono">{{ m.name }}</div>
              <div class="mode-desc">{{ m.desc }}</div>
            </div>
          </label>
        </div>
      </div>
    </div>

    <!-- 服务端口 -->
    <div class="card">
      <div class="card-title">服务端口</div>
      <div class="field">
        <label class="lbl">本地服务端口（保存后自动重启；被占用时自动向上顺延到空位）</label>
        <div class="row">
          <input
            class="mono port-input"
            type="number"
            min="1"
            max="65535"
            :value="portDraft"
            @input="portDraft = ($event.target as HTMLInputElement).value"
          />
          <button
            class="btn primary"
            :disabled="portBusy"
            @click="onApplyPort"
            >保存</button
          >
        </div>
        <div
          v-if="!portBusy && config.listen_port > config.port"
          class="hint warn"
        >
          配置端口 {{ config.port }} 已被占用，实际监听 {{ config.listen_port }}；Agent 连接请用下方实际地址（每次启动都从 {{ config.port }} 起扫空位）。
        </div>
      </div>
      <div class="field">
        <label class="lbl">Agent 连接地址（在其它 Agent 中填写自定义模型时使用）</label>
        <div class="row">
          <input
            class="mono key-input"
            :value="config.base_url"
            readonly
          />
          <button
            class="btn primary"
            @click="copyText(config.base_url)"
            >复制</button
          >
        </div>
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
  margin-bottom: 14px;
}
.card-title {
  font-size: 14px;
  font-weight: 600;
  margin-bottom: 12px;
}
.field {
  margin-bottom: 6px;
}
.lbl {
  display: block;
  color: var(--cp-dim);
  font-size: 13px;
  margin-bottom: 8px;
}
.row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.key-input-wrap {
  position: relative;
  display: inline-flex;
  align-items: center;
}
.key-input {
  width: 360px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-text);
  padding: 7px 36px 7px 10px;
  font-size: 13px;
}
.key-eye {
  position: absolute;
  right: 8px;
  background: none;
  border: none;
  color: var(--cp-dim);
  cursor: pointer;
  padding: 2px;
  display: flex;
  align-items: center;
  transition: color 0.15s;
}
.key-eye:hover {
  color: var(--cp-cyan);
}
.port-input {
  width: 140px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 6px;
  color: var(--cp-text);
  padding: 7px 10px;
}
.port-static {
  color: var(--cp-text);
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
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
.btn.ghost:hover {
  border-color: var(--cp-dim);
  color: var(--cp-text);
}
.hint.warn {
  margin-top: 8px;
  color: var(--cp-yellow);
  font-size: 12px;
}
.mode-options {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.mode-opt {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  cursor: pointer;
  transition:
    border-color 0.15s,
    background 0.15s;
}
.mode-opt.active {
  border-color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.08);
}
.radio {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  border: 2px solid var(--cp-dim);
  margin-top: 2px;
  flex: none;
}
.radio.on {
  border-color: var(--cp-cyan);
  background: radial-gradient(circle, var(--cp-cyan) 0 4px, transparent 5px);
}
.mode-name {
  color: var(--cp-text);
  font-size: 14px;
  font-weight: 500;
}
.mode-desc {
  color: var(--cp-dim);
  font-size: 12px;
  margin-top: 2px;
}
.switch-row {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--cp-dim);
  font-size: 13px;
  cursor: pointer;
}
</style>
