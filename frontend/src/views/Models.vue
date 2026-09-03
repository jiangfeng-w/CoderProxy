<script setup lang="ts">
// 模型页：列表 + 搜索 + 白名单启用开关 + 全部启用/禁用。
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useMessage } from "naive-ui";
import { getModels, getConfig, updateConfig, authSync, type OaiModel, type Config } from "../api";

const message = useMessage();
const DISABLE_ALL = "__none__";

const models = ref<OaiModel[]>([]);
const config = ref<Config | null>(null);
const search = ref("");
const syncing = ref(false);
let timer: number | undefined;

const wl = computed(() => config.value?.model_whitelist ?? []);

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase();
  const list = q
    ? models.value.filter(
        (m) => m.id.toLowerCase().includes(q) || (m.name ?? "").toLowerCase().includes(q)
      )
    : models.value;
  return list;
});

function isEnabled(id: string): boolean {
  const w = wl.value;
  if (w.length === 0) return true;
  if (w.length === 1 && w[0] === DISABLE_ALL) return false;
  return w.includes(id);
}

async function saveWl(next: string[]) {
  try {
    config.value = await updateConfig({ model_whitelist: next });
  } catch (e) {
    message.error(String(e));
  }
}

async function onToggle(id: string, on: boolean) {
  const w = wl.value;
  let next: string[];
  if (on) {
    // 启用：从「全部禁用」或已有白名单里加回
    if (w.length === 0) return; // 已全部启用
    if (w.length === 1 && w[0] === DISABLE_ALL) next = [id];
    else next = w.includes(id) ? w : [...w, id];
  } else {
    // 禁用
    if (w.length === 0) {
      next = models.value.map((m) => m.id).filter((x) => x !== id);
    } else if (w.length === 1 && w[0] === DISABLE_ALL) {
      return; // 已全部禁用
    } else {
      next = w.filter((x) => x !== id);
      if (next.length === 0) next = [DISABLE_ALL];
    }
  }
  await saveWl(next);
}

async function onAllEnable() {
  await saveWl([]);
  message.success("已全部启用");
}
async function onAllDisable() {
  await saveWl([DISABLE_ALL]);
  message.success("已全部禁用");
}

/** 同步模型目录并刷新列表（需已登录）。 */
async function onSync() {
  if (syncing.value) return;
  syncing.value = true;
  try {
    const res = await authSync();
    message.success(`同步完成：${res.models.length} 个模型`);
    models.value = (await getModels()).data;
  } catch (e) {
    message.error(String(e));
  } finally {
    syncing.value = false;
  }
}

async function load() {
  try {
    models.value = (await getModels()).data;
  } catch (e) {
    message.error(String(e));
  }
  try {
    config.value = await getConfig();
  } catch (e) {
    message.error(String(e));
  }
}

onMounted(() => {
  load();
  // 空列表时轮询，感知 Shell 登录后自动同步的结果
  timer = window.setInterval(() => {
    if (models.value.length === 0) load();
  }, 3000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div>
    <h2 class="page-title">模型</h2>

    <div class="card">
      <div class="toolbar">
        <input
          v-model="search"
          class="search"
          placeholder="搜索模型名 / 显示名"
        />
        <button class="btn primary" :disabled="syncing" @click="onSync">同步模型</button>
        <button class="btn primary" @click="onAllEnable">全部启用</button>
        <button class="btn danger" @click="onAllDisable">全部禁用</button>
        <span class="count mono">已启用 {{ models.filter((m) => isEnabled(m.id)).length }}/{{ models.length }}</span>
      </div>

      <div v-if="filtered.length === 0" class="empty">
        暂无模型，请先登录并同步模型目录
      </div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>模型名</th>
            <th>显示名</th>
            <th class="r">启用（白名单）</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in filtered" :key="m.id">
            <td class="mono">{{ m.id }}</td>
            <td class="dim">{{ m.name || "—" }}</td>
            <td class="r">
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
  transition: border-color 0.15s, color 0.15s, background 0.15s;
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
.tbl .r {
  text-align: right;
}
.dim {
  color: var(--cp-dim);
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
  content: "";
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
</style>
