<script setup lang="ts">
// 日志页：实时事件流（增量拉取）+ 筛选 / 清空 / 暂停 + 汇总。
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useMessage } from "naive-ui";
import { getEvents, clearMonitor, type MonitorEvent, type MonitorStats } from "../api";

const message = useMessage();

const MAX_EVENTS = 500;
const events = ref<MonitorEvent[]>([]);
const stats = ref<MonitorStats>({});
const paused = ref(false);
const filter = ref("全部");

let lastId = 0;
let timer: number | undefined;

const FILTERS = ["全部", "请求", "完成", "错误", "401刷新"];

const kindMeta: Record<string, { label: string; cls: string }> = {
  chat_request: { label: "请求", cls: "cyan" },
  chat_done: { label: "完成", cls: "green" },
  chat_error: { label: "错误", cls: "red" },
  auth_401_refresh: { label: "401刷新", cls: "yellow" },
};

const DISPLAY_KINDS = new Set(["chat_request", "chat_done", "chat_error", "auth_401_refresh"]);

const filtered = computed(() => {
  let list = events.value.filter((e) => DISPLAY_KINDS.has(e.kind));
  if (filter.value !== "全部") {
    list = list.filter((e) => kindMeta[e.kind]?.label === filter.value);
  }
  return [...list].reverse();
});

const summary = computed(() => ({
  requests: stats.value["chat_request"] ?? 0,
  done: stats.value["chat_done"] ?? 0,
  error: stats.value["chat_error"] ?? 0,
  refresh: stats.value["auth_401_refresh"] ?? 0,
}));

function fmtTs(iso: string) {
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

function detailOf(e: MonitorEvent): string {
  const d = e.data;
  const stream = d.stream ? " · 流式" : "";
  switch (e.kind) {
    case "chat_request":
      return `工具 ${d.tools ?? 0} 个${stream}`;
    case "chat_done":
      return `转换 ${d.map_hits ?? 0} · 转发 ${d.longtail ?? 0} · 忽略 ${d.dropped ?? 0}${stream}`;
    case "chat_error":
      return String(d.error ?? "");
    case "auth_401_refresh":
      return `模型 ${d.model ?? ""} 上游返回 401，已自动刷新重试`;
    default:
      return "";
  }
}

async function poll() {
  if (paused.value) return;
  try {
    const res = await getEvents(lastId, 200);
    if (res.events.length > 0) {
      lastId = Math.max(lastId, ...res.events.map((e) => e.id));
      events.value = [...events.value, ...res.events];
      if (events.value.length > MAX_EVENTS) {
        events.value = events.value.slice(-MAX_EVENTS);
      }
    }
    stats.value = res.stats;
  } catch {
    /* relay 未就绪 */
  }
}

async function onClear() {
  try {
    await clearMonitor();
    events.value = [];
    stats.value = {};
    lastId = 0;
    message.success("已清空");
  } catch (e) {
    message.error(String(e));
  }
}

onMounted(() => {
  poll();
  timer = window.setInterval(poll, 2000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div>
    <div class="card">
      <div class="toolbar">
        <div class="filters">
          <button
            v-for="f in FILTERS"
            :key="f"
            class="chip"
            :class="{ active: filter === f }"
            @click="filter = f"
          >
            {{ f }}
          </button>
        </div>
        <div class="spacer" />
        <button class="btn" @click="paused = !paused">{{ paused ? "继续" : "暂停" }}</button>
        <button class="btn danger" @click="onClear">清空</button>
      </div>

      <div class="log-wrap">
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
            <tr v-for="e in filtered" :key="e.id">
              <td class="mono dim">{{ fmtTs(e.ts) }}</td>
              <td>
                <span class="cp-tag" :class="kindMeta[e.kind].cls">{{ kindMeta[e.kind].label }}</span>
              </td>
              <td class="mono dim">{{ String(e.data.model ?? "—") }}</td>
              <td class="mono dim detail">{{ detailOf(e) }}</td>
            </tr>
            <tr v-if="filtered.length === 0">
              <td colspan="4" class="empty">暂无事件</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="summary mono">
        <span>请求 <b style="color: var(--cp-cyan)">{{ summary.requests.toLocaleString() }}</b></span>
        <span>完成 <b style="color: var(--cp-green)">{{ summary.done.toLocaleString() }}</b></span>
        <span>错误 <b style="color: var(--cp-red)">{{ summary.error.toLocaleString() }}</b></span>
        <span>401刷新 <b style="color: var(--cp-yellow)">{{ summary.refresh.toLocaleString() }}</b></span>
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
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.spacer {
  flex: 1;
}
.filters {
  display: flex;
  gap: 6px;
}
.chip {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid var(--cp-border);
  background: transparent;
  color: var(--cp-dim);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.chip:hover {
  color: var(--cp-text);
}
.chip.active {
  color: var(--cp-cyan);
  border-color: rgba(34, 211, 238, 0.5);
  background: rgba(34, 211, 238, 0.12);
}
.btn {
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid var(--cp-border);
  background: transparent;
  color: var(--cp-text);
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;
}
.btn:hover {
  border-color: var(--cp-cyan);
  color: var(--cp-cyan);
}
.btn.danger {
  color: var(--cp-red);
  border-color: rgba(239, 68, 68, 0.5);
}
.btn.danger:hover {
  border-color: var(--cp-red);
  background: rgba(239, 68, 68, 0.1);
}
.log-wrap {
  flex: 1;
  overflow: auto;
  max-height: 520px;
  border: 1px solid var(--cp-border);
  border-radius: 8px;
}
.tbl {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.tbl th {
  position: sticky;
  top: 0;
  text-align: left;
  color: var(--cp-dim);
  font-weight: 500;
  padding: 8px;
  background: var(--cp-panel-2);
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 7px 8px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
}
.w-time {
  width: 90px;
}
.w-type {
  width: 90px;
}
.w-model {
  width: 200px;
}
.detail {
  font-size: 12px;
}
.empty {
  text-align: center;
  color: var(--cp-dim);
  padding: 24px 0;
}
.summary {
  margin-top: 10px;
  display: flex;
  gap: 24px;
  color: var(--cp-dim);
  font-size: 13px;
}
</style>
