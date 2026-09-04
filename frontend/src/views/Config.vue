<script setup lang="ts">
// 配置页：API Key / 工具伪装模式 / 服务端口。
import { onMounted, ref } from "vue";
import { useMessage, NPopconfirm } from "naive-ui";
import { getConfig, updateConfig, relayRestart, type Config } from "../api";

const message = useMessage();

const config = ref<Config | null>(null);
const showKey = ref(false);
const busy = ref(false);

async function load() {
  try {
    config.value = await getConfig();
  } catch (e) {
    message.error(String(e));
  }
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    message.success("已复制到剪贴板");
  } catch {
    message.error("复制失败");
  }
}

async function onResetKey() {
  busy.value = true;
  try {
    config.value = await updateConfig({ regenerate_api_key: true });
    void copyText(config.value.api_key);
  } catch (e) {
    message.error(String(e));
  } finally {
    busy.value = false;
  }
}

async function onToolMode(mode: string) {
  try {
    config.value = await updateConfig({ tool_mode: mode });
    message.success(`工具模式已切换为 ${mode}`);
  } catch (e) {
    message.error(String(e));
  }
}

async function onApplyPort() {
  const p = config.value?.port;
  if (!p || p < 1 || p > 65535) {
    message.error("端口需在 1-65535 之间");
    return;
  }
  // 1) 保存端口到 relay（持久化，跨重启一致）
  try {
    config.value = await updateConfig({ port: p });
  } catch (e) {
    message.error(`端口保存失败：${String(e)}`);
    return;
  }
  message.info(`已保存端口 ${p}，重启生效`);
  // 2) 重启 relay，按持久化端口立即生效
  await relayRestart();
  message.info(`已按固定端口 ${p} 重启`);
}

onMounted(load);
</script>

<template>
  <div>
    <!-- 本地 API Key -->
    <div class="card">
      <div class="card-title">本地 API Key</div>
      <div class="field">
        <label class="lbl">静态鉴权密钥（Agent 连接 relay 用，Bearer）</label>
        <div class="row">
          <div class="key-input-wrap">
            <input
              class="mono key-input"
              :type="showKey ? 'text' : 'password'"
              :value="config?.api_key ?? ''"
              readonly
            />
            <button class="key-eye" @click="showKey = !showKey" :title="showKey ? '隐藏' : '显示'">
              <svg v-if="showKey" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                <line x1="1" y1="1" x2="23" y2="23"/>
              </svg>
              <svg v-else viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                <circle cx="12" cy="12" r="3"/>
              </svg>
            </button>
          </div>
          <button class="btn primary" @click="copyText(config?.api_key ?? '')">复制</button>
          <n-popconfirm
            positive-text="确认重置"
            negative-text="取消"
            @positive-click="onResetKey"
          >
            <template #trigger>
              <button class="btn danger" :disabled="busy">重置</button>
            </template>
            重置将生成新密钥并作废旧值，已连接的 Agent 需改用新密钥。
          </n-popconfirm>
        </div>
        <div class="hint warn">密钥仅存于本机，真实牛码 token 不会下发到任何 Agent。</div>
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
              { key: 'hybrid', name: 'hybrid（推荐）', desc: '已知工具伪装映射，长尾工具默认透传' },
              { key: 'strict', name: 'strict（严格）', desc: '只放行映射表内工具，其余丢弃' },
              { key: 'passthrough', name: 'passthrough（透传）', desc: '全部工具名原样透传，不做伪装' },
            ]"
            :key="m.key"
            class="mode-opt"
            :class="{ active: config?.tool_mode === m.key }"
            @click="onToolMode(m.key)"
          >
            <span class="radio" :class="{ on: config?.tool_mode === m.key }" />
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
        <label class="lbl">relay 监听端口（保存后重启生效，每次启动固定该端口）</label>
        <div class="row">
          <input
            class="mono port-input"
            type="number"
            min="1"
            max="65535"
            :value="config?.port ?? 3601"
            @input="config && (config.port = Number(($event.target as HTMLInputElement).value))"
          />
          <button class="btn primary" @click="onApplyPort">保存并重启</button>
        </div>
        <div class="hint warn">端口修改后保存到本地，重启即生效；占用时应用会提示更换端口。</div>
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
  transition: border-color 0.15s, color 0.15s, background 0.15s;
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
  transition: border-color 0.15s, background 0.15s;
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
