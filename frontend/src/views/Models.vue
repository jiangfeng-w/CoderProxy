<script setup lang="ts">
// 模型页：列表 + 搜索 + 白名单启用开关 + 全部启用/禁用 + 可用状态检测。
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useMessage } from "naive-ui";
import { getModels, getConfig, updateConfig, authSync, type OaiModel, type Config } from "../api";
import { store } from "../store";
import { testModel, batchTestAll } from "../modelCheck";

const message = useMessage();
const DISABLE_ALL = "__none__";

const models = ref<OaiModel[]>([]);
const config = ref<Config | null>(null);
const search = ref("");
const syncing = ref(false);
const testing = ref<string | null>(null);
let timer: number | undefined;

/** 从全局 store 读取模型状态 */
function getStatus(id: string): string {
  return store.modelStatus[id] ?? "";
}

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

/** 启用开关：先测试连通性，失败则保持关闭。 */
async function onToggle(id: string, on: boolean) {
  if (on) {
    // 先测试连通性
    store.modelStatus[id] = "testing";
    const ok = await testModel(id);
    store.modelStatus[id] = ok ? "available" : "unavailable";
    if (!ok) {
      message.error(`模型 ${id} 连接失败，保持关闭`);
      return;
    }
  }
  const w = wl.value;
  let next: string[];
  if (on) {
    if (w.length === 0) return;
    if (w.length === 1 && w[0] === DISABLE_ALL) next = [id];
    else next = w.includes(id) ? w : [...w, id];
  } else {
    if (w.length === 0) {
      next = models.value.map((m) => m.id).filter((x) => x !== id);
    } else if (w.length === 1 && w[0] === DISABLE_ALL) {
      return;
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

/** 同步模型目录并刷新列表，然后顺序测试所有模型可用性。 */
async function onSync() {
  if (syncing.value) return;
  syncing.value = true;
  try {
    const res = await authSync();
    message.success(`同步完成：${res.models.length} 个模型`);
    models.value = (await getModels(true)).data;
    // 同步完成后顺序测试所有模型（失败自动关闭启用开关）
    const failed = await batchTestAll(models.value);
    if (failed.length > 0) {
      message.warning(`${failed.length} 个模型连接失败，已自动关闭启用`);
    }
  } catch (e) {
    message.error(String(e));
  } finally {
    syncing.value = false;
  }
}

/** 点击模型名即复制到剪贴板。 */
async function onCopy(id: string) {
  try {
    await navigator.clipboard.writeText(id);
    message.success(`已复制模型名：${id}`);
  } catch (e) {
    message.error(`复制失败：${String(e)}`);
  }
}

/** 手动测试单个模型连通性。失败时自动关闭启用开关。 */
async function onTest(id: string) {
  if (testing.value) return;
  testing.value = id;
  store.modelStatus[id] = "testing";
  try {
    const ok = await testModel(id);
    store.modelStatus[id] = ok ? "available" : "unavailable";
    if (ok) {
      message.success(`模型 ${id} 连通`);
    } else {
      message.error(`模型 ${id} 连接失败，已关闭`);
      // 测试失败时自动关闭启用开关
      const w = wl.value;
      if (w.length > 0 && w.includes(id)) {
        const next = w.filter((x) => x !== id);
        await saveWl(next.length === 0 ? [DISABLE_ALL] : next);
      }
    }
  } finally {
    testing.value = null;
  }
}

async function load() {
  try {
    // GUI 用完整目录（all=1），失败模型仍需可见，不能按白名单过滤隐藏
    models.value = (await getModels(true)).data;
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
    <div class="card">
      <div class="toolbar">
        <input
          v-model="search"
          class="search"
          placeholder="搜索模型名"
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
            <th class="r col-status">状态</th>
            <th class="r col-toggle">启用</th>
            <th class="r col-test">测试</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in filtered" :key="m.id">
            <td class="mono copyable" title="点击复制模型名" @click="onCopy(m.id)">{{ m.id }}</td>
            <td class="r">
              <span v-if="getStatus(m.id) === 'available'" class="cp-tag green">可用</span>
              <span v-else-if="getStatus(m.id) === 'testing'" class="cp-tag gray">检测中</span>
              <span v-else-if="getStatus(m.id) === 'unavailable'" class="cp-tag red">不可用</span>
              <span v-else class="cp-tag gray">未检测</span>
            </td>
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
            <td class="r">
              <button class="btn test" :disabled="testing !== null" @click="onTest(m.id)">
                {{ testing === m.id ? "测试中…" : "测试" }}
              </button>
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
.copyable {
  cursor: pointer;
  user-select: text;
  transition: color 0.15s;
}
.copyable:hover {
  color: var(--cp-cyan);
}
.btn.test {
  padding: 3px 12px;
  font-size: 12px;
  color: var(--cp-dim);
}
.btn.test:hover:not(:disabled) {
  color: var(--cp-cyan);
  border-color: var(--cp-cyan);
  background: rgba(34, 211, 238, 0.1);
}
.btn.test:disabled {
  opacity: 0.5;
  cursor: not-allowed;
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
  width: 120px;
}
.col-toggle {
  width: 60px;
}
.col-test {
  width: 120px;
}
</style>
