<script setup lang="ts">
// 顶栏（服务状态/启停/登录态）+ 左侧导航 + 四页内容区。深色主题，全局轮询服务与登录态。
import { onMounted, onUnmounted, ref } from "vue";
import { NButton, NModal, useMessage } from "naive-ui";
import { listen } from "@tauri-apps/api/event";
import { relayStatus, relayStop, relayRestart, authStatus, authLoginStart, authLogout, authSync, openAuthorizeUrl, windowHide, appExit } from "./api";
import { store } from "./store";
import Overview from "./views/Overview.vue";
import Config from "./views/Config.vue";
import Models from "./views/Models.vue";
import Logs from "./views/Logs.vue";

const message = useMessage();

const views: Record<string, { name: string; comp: any }> = {
  overview: { name: "总览", comp: Overview },
  config: { name: "配置", comp: Config },
  models: { name: "模型", comp: Models },
  logs: { name: "日志", comp: Logs },
};

const busy = ref(false);
let timer: number | undefined;
let lastAuthStatus = "not_logged_in";
let syncing = false;
let closeUnlisten: Promise<() => void> | undefined;

// 关窗三选对话框：用户点右上角 X 时，由 Rust 发 `close-requested` 事件触发。
const showCloseDlg = ref(false);

async function chooseHide() {
  try {
    await windowHide();
  } catch (e) {
    message.error(String(e));
  } finally {
    showCloseDlg.value = false;
  }
}

async function chooseExit() {
  showCloseDlg.value = false;
  try {
    await appExit();
  } catch (e) {
    message.error(String(e));
  }
}

async function syncModels() {
  if (syncing) return;
  syncing = true;
  try {
    await authSync();
  } catch {
    /* 同步失败不打扰：模型页可手动重试 */
  } finally {
    syncing = false;
  }
}

async function pollStatus() {
  try {
    store.relay = await relayStatus();
  } catch {
    /* 壳未就绪 */
  }
  try {
    store.auth = await authStatus();
  } catch {
    /* 忽略 */
  }
  // 登录态由「未登录 → 已登录」转变（含浏览器授权完成后轮询到）：自动同步一次模型目录
  if (store.auth.status === "logged_in" && lastAuthStatus !== "logged_in") {
    syncModels();
  }
  lastAuthStatus = store.auth.status;
}

async function onStop() {
  busy.value = true;
  try {
    await relayStop();
    message.success("服务已停止");
  } catch (e) {
    message.error(String(e));
  } finally {
    busy.value = false;
    await pollStatus();
  }
}

async function onRestart() {
  busy.value = true;
  try {
    await relayRestart();
    message.info("正在重启服务...");
  } catch (e) {
    message.error(String(e));
  } finally {
    busy.value = false;
  }
}

async function onLogin() {
  busy.value = true;
  try {
    const res = await authLoginStart();
    if (res.status === "pending" && res.authorize_url) {
      openAuthorizeUrl(res.authorize_url);
      message.info("已在浏览器打开授权页，登录完成后自动同步");
    } else if (res.status === "logged_in") {
      message.success("已登录");
    } else {
      message.warning(res.error || "登录未完成");
    }
  } catch (e) {
    message.error(String(e));
  } finally {
    busy.value = false;
    await pollStatus();
  }
}

async function onLogout() {
  busy.value = true;
  try {
    await authLogout();
    message.info("已登出");
  } catch (e) {
    message.error(String(e));
  } finally {
    busy.value = false;
    await pollStatus();
  }
}

onMounted(() => {
  pollStatus();
  timer = window.setInterval(pollStatus, 2500);
  // 点右上角 X → Rust prevent_close + 发事件 → 弹三选对话框
  closeUnlisten = listen("close-requested", () => {
    showCloseDlg.value = true;
  });
});
onUnmounted(() => {
  clearInterval(timer);
  closeUnlisten?.then((fn) => fn());
});
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="logo">CP</span>
        <span class="name">CoderProxy</span>
      </div>

      <div class="status">
        <span class="dot" :class="store.relay.running ? 'ok' : 'off'" />
        <span>{{ store.relay.running ? "运行中" : "已停止" }}</span>
        <span v-if="store.relay.running && store.relay.port" class="port mono">
          端口：{{ store.relay.port }}
        </span>
        <span v-if="store.relay.running" class="mode cp-tag cyan">
          {{ store.relay.tool_mode }}
        </span>
      </div>

      <div class="ops">
        <button class="btn danger" :disabled="busy || !store.relay.running" @click="onStop">
          停止服务
        </button>
        <button class="btn primary" :disabled="busy" @click="onRestart">
          重启服务
        </button>
        <template v-if="store.auth.status === 'logged_in'">
          <span class="user">
            已登录
            <b>{{ store.auth.account?.label || store.auth.account?.id || "" }}</b>
          </span>
          <button class="btn ghost" :disabled="busy" @click="onLogout">登出</button>
        </template>
        <template v-else>
          <button class="btn ghost" :disabled="busy" @click="onLogin">登录</button>
        </template>
      </div>
    </header>

    <div class="body">
      <aside class="sidebar">
        <nav>
          <a
            v-for="(v, key) in views"
            :key="key"
            class="nav-item"
            :class="{ active: store.view === key }"
            @click="store.view = key"
          >
            <span class="nav-ico">{{ { overview: "◉", config: "⚙", models: "◈", logs: "☰" }[key] }}</span>
            {{ v.name }}
          </a>
        </nav>
        <div class="ver mono">v0.1.0</div>
      </aside>

      <main class="content">
        <component :is="views[store.view]?.comp" />
      </main>
    </div>

    <!-- 关窗三选对话框：取消(灰) / 退出应用(红) / 最小化到托盘(蓝) -->
    <n-modal
      v-model:show="showCloseDlg"
      preset="card"
      title="退出 CoderProxy？"
      :style="{ width: '420px' }"
      :closable="false"
      :mask-closable="false"
      :bordered="false"
    >
      <p class="dlg-tip">选择关闭后的处理方式：</p>
      <template #footer>
        <div class="dlg-actions">
          <n-button @click="showCloseDlg = false">取消</n-button>
          <n-button type="error" @click="chooseExit">退出应用</n-button>
          <n-button type="primary" @click="chooseHide">最小化到托盘</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.app {
  /* 用 100vh 而非 height:100%：Naive UI 的 provider 会渲染无高度的包裹 div，
     打断 height:100% 链，导致 .content 无法成为滚动容器，页面被 body 裁剪 */
  height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--cp-bg);
}

.topbar {
  height: 52px;
  flex: none;
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 0 16px;
  background: var(--cp-panel-2);
  border-bottom: 1px solid var(--cp-border);
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.logo {
  width: 24px;
  height: 24px;
  border-radius: 6px;
  background: linear-gradient(135deg, #22d3ee, #0ea5e9);
  color: #04121a;
  font-weight: 800;
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.name {
  font-weight: 600;
}

.status {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--cp-text);
  font-size: 13px;
}
.dot.ok {
  background: var(--cp-green);
  box-shadow: 0 0 6px var(--cp-green);
}
.dot.off {
  background: #475569;
}
.port {
  color: var(--cp-dim);
  font-size: 12px;
}

.ops {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.user {
  color: var(--cp-dim);
  font-size: 13px;
  margin-left: 8px;
}
.user b {
  color: var(--cp-text);
}

.btn {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid var(--cp-border);
  background: transparent;
  color: var(--cp-text);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.btn:hover {
  border-color: var(--cp-cyan);
  color: var(--cp-cyan);
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn.danger {
  color: var(--cp-red);
  border-color: rgba(239, 68, 68, 0.5);
}
.btn.danger:hover {
  border-color: var(--cp-red);
  background: rgba(239, 68, 68, 0.1);
}
.btn.primary {
  color: var(--cp-cyan);
  border-color: rgba(34, 211, 238, 0.5);
}
.btn.primary:hover {
  border-color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.1);
}
.btn.ghost:hover {
  border-color: var(--cp-dim);
  color: var(--cp-text);
}

.body {
  flex: 1;
  display: flex;
  min-height: 0;
}

.sidebar {
  width: 180px;
  flex: none;
  background: var(--cp-panel-2);
  border-right: 1px solid var(--cp-border);
  display: flex;
  flex-direction: column;
  padding: 12px 8px;
}
.sidebar nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  color: var(--cp-dim);
  font-size: 14px;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s, color 0.15s;
}
.nav-item:hover {
  color: var(--cp-text);
  background: rgba(51, 65, 85, 0.4);
}
.nav-item.active {
  color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.12);
  border-left: 2px solid var(--cp-cyan);
}
.nav-ico {
  width: 18px;
  text-align: center;
  font-size: 15px;
}
.ver {
  margin-top: auto;
  padding: 8px 12px;
  color: #475569;
  font-size: 12px;
}

.content {
  flex: 1;
  min-width: 0;
  overflow: auto;
  padding: 18px 20px;
}

/* 内容区滚动条（深色主题可见） */
.content::-webkit-scrollbar {
  width: 10px;
}
.content::-webkit-scrollbar-thumb {
  background: #334155;
  border-radius: 5px;
  border: 2px solid var(--cp-bg);
}
.content::-webkit-scrollbar-thumb:hover {
  background: #475569;
}
.content::-webkit-scrollbar-track {
  background: transparent;
}

.dlg-tip {
  margin: 0 0 4px;
  color: var(--cp-dim);
  font-size: 13px;
}
.dlg-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
