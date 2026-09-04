<script setup lang="ts">
// 统计页（M8）：/v1/stats 数据源，按 维度（天/小时/模型）聚合 token 与请求数。
// 顶部：时间区间 + 模型 + 类型筛选；维度切换按钮；指标卡；一张自适应图表 + 明细表。
// 图表用 ECharts（dark 主题），折线（天/小时：请求 + token 双轴）、柱状（模型/类型：token）。
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import * as echarts from "echarts";
import {
  NButton, NDatePicker, NEmpty, NSelect, NSpin,
} from "naive-ui";
import {
  fetchStats, fetchLogKinds, fetchLogModels,
  type StatsGroupBy, type StatsRow, type StatsTotal, type StatsQuery,
} from "../api";

// ── 类型元信息（与日志页同口径；stat 筛选下拉用）──
const kindMeta: Record<string, { label: string; cls: string }> = {
  chat_request: { label: "请求", cls: "cyan" },
  chat_done: { label: "完成", cls: "green" },
  chat_error: { label: "错误", cls: "red" },
  auth_401_refresh: { label: "401刷新", cls: "yellow" },
};

// ── 维度切换（模型/日/小时；kind 由后端支持，页面主切换不暴露）──
const dimOptions: { label: string; value: StatsGroupBy }[] = [
  { label: "按天", value: "day" },
  { label: "按小时", value: "hour" },
  { label: "按模型", value: "model" },
];
const groupBy = ref<StatsGroupBy>("day");

// ── 筛选 ──
const kinds = ref<string[]>([]);
const models = ref<string[]>([]);
const kind = ref<string | null>(null); // null = 全部类型
const model = ref<string | null>(null); // null = 全部模型
const timeRange = ref<[number, number] | null>(null); // naive 时间戳（ms）

// ── 数据 ──
const rows = ref<StatsRow[]>([]);
const total = ref<StatsTotal>({
  requests: 0, success: 0, failed: 0,
  prompt_tokens: 0, completion_tokens: 0,
  cached_tokens: 0, reasoning_tokens: 0, total_tokens: 0,
});
const loading = ref(false);
const loadError = ref("");

// ── ECharts ──
const chartEl = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;
let resizeObs: ResizeObserver | null = null;

const kindOptions = computed(() => {
  const opts = Object.entries(kindMeta).map(([key, m]) => ({ label: m.label, value: key }));
  const extra = kinds.value.filter((k) => !(k in kindMeta));
  return [...opts, ...extra.map((k) => ({ label: k, value: k }))];
});
const modelOptions = computed(() => models.value.map((m) => ({ label: m, value: m })));

/** naive 时间戳（ms）→ UTC ISO，后缀与后端存储一致（毫秒 +00:00）。 */
function toUtcIso(ts: number): string {
  return new Date(ts).toISOString().replace("Z", "+00:00");
}

async function fetchKindsModels() {
  try {
    const [k, m] = await Promise.all([fetchLogKinds(), fetchLogModels()]);
    kinds.value = k.kinds;
    models.value = m.models;
  } catch {
    /* 下拉失败不阻断；列表接口同源数据亦兜底 */
  }
}

async function fetchData(silent = false): Promise<void> {
  if (!silent) loading.value = true;
  loadError.value = "";
  try {
    const r = timeRange.value;
    const q: StatsQuery = {
      groupBy: groupBy.value,
      model: model.value ?? undefined,
      kind: kind.value ?? undefined,
      timeFrom: r ? toUtcIso(r[0]) : undefined,
      timeTo: r ? toUtcIso(r[1]) : undefined,
    };
    const res = await fetchStats(q);
    rows.value = res.rows;
    total.value = res.total;
    await nextTick();
    renderChart();
  } catch (e) {
    loadError.value = String(e);
  } finally {
    if (!silent) loading.value = false;
  }
}

function onFilterChange() {
  void fetchData();
}

function resetFilter() {
  kind.value = null;
  model.value = null;
  timeRange.value = null;
  onFilterChange();
}

// ── 图表：按维度组装 option ──
function chartOption(group: StatsGroupBy, data: StatsRow[]): echarts.EChartsOption {
  const keys = data.map((r) => r.key);
  const rotate = keys.length > 8 ? 30 : 0;
  const base = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis" as const },
    grid: { left: 56, right: 64, top: 40, bottom: 36 },
    textStyle: { color: "#94a3b8" },
  };
  if (group === "model" || group === "kind") {
    return {
      ...base,
      legend: { data: ["总 Token"], textStyle: { color: "#94a3b8" } },
      xAxis: { type: "category" as const, data: keys, axisLabel: { rotate } },
      yAxis: { type: "value" as const, name: "Token",
               splitLine: { lineStyle: { color: "#334155" } } },
      series: [{
        type: "bar" as const,
        name: "总 Token",
        data: data.map((r) => r.total_tokens),
        itemStyle: { color: "#22d3ee", borderRadius: [3, 3, 0, 0] },
      }],
    };
  }
  // 天/小时：请求数（左轴）+ 总 Token（右轴）
  return {
    ...base,
    legend: { data: ["请求数", "总 Token"], textStyle: { color: "#94a3b8" } },
    xAxis: { type: "category" as const, data: keys, axisLabel: { rotate } },
    yAxis: [
      { type: "value" as const, name: "请求数",
        splitLine: { lineStyle: { color: "#334155" } } },
      { type: "value" as const, name: "Token", splitLine: { show: false } },
    ],
    series: [
      {
        type: "line" as const, name: "请求数", yAxisIndex: 0, smooth: true,
        data: data.map((r) => r.requests), symbol: "circle", symbolSize: 6,
        itemStyle: { color: "#22d3ee" }, lineStyle: { width: 2 },
        areaStyle: { opacity: 0.12 },
      },
      {
        type: "line" as const, name: "总 Token", yAxisIndex: 1, smooth: true,
        data: data.map((r) => r.total_tokens), symbol: "circle", symbolSize: 6,
        itemStyle: { color: "#f97316" }, lineStyle: { width: 2 },
      },
    ],
  };
}

function renderChart() {
  if (!chartEl.value) return;
  if (!chart) {
    chart = echarts.init(chartEl.value);
  }
  if (rows.value.length === 0) {
    chart.clear();
    return;
  }
  chart.setOption(chartOption(groupBy.value, rows.value), true);
}

watch(groupBy, () => {
  void fetchData();
});

onMounted(() => {
  void Promise.all([fetchKindsModels(), fetchData()]);
  if (chartEl.value) {
    resizeObs = new ResizeObserver(() => chart?.resize());
    resizeObs.observe(chartEl.value);
  }
});

onUnmounted(() => {
  resizeObs?.disconnect();
  chart?.dispose();
  chart = null;
});

// ── 指标/表格格式化 ──
const n = (v?: number) => (v ?? 0).toLocaleString();

function fmtKey(key: string, group: StatsGroupBy): string {
  if (group === "day") return key;
  if (group === "hour") return key.slice(5);
  return key;
}
</script>

<template>
  <div>
    <div class="card">
      <div class="head">
        <span class="card-title">Token 统计</span>
        <span class="spacer" />
        <n-button size="small" :loading="loading" @click="fetchData()">刷新</n-button>
      </div>

      <div class="filters">
        <n-select
          v-model:value="kind"
          class="ctl"
          :options="kindOptions"
          placeholder="全部类型"
          clearable
          filterable
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
        <div class="dim-switch">
          <button
            v-for="d in dimOptions"
            :key="d.value"
            class="dim-btn"
            :class="{ active: groupBy === d.value }"
            @click="groupBy = d.value"
          >
            {{ d.label }}
          </button>
        </div>
        <n-button size="small" quaternary @click="resetFilter">重置筛选</n-button>
      </div>

      <div v-if="loadError" class="err-banner">加载失败：{{ loadError }}</div>

      <!-- 指标卡：总计 -->
      <div class="kpi-grid">
        <div class="kpi">
          <div class="kpi-num" style="color: var(--cp-cyan)">{{ n(total.requests) }}</div>
          <div class="kpi-label">请求数</div>
        </div>
        <div class="kpi">
          <div class="kpi-num" style="color: var(--cp-green)">{{ n(total.success) }}</div>
          <div class="kpi-label">成功</div>
        </div>
        <div class="kpi">
          <div class="kpi-num" style="color: var(--cp-red)">{{ n(total.failed) }}</div>
          <div class="kpi-label">失败</div>
        </div>
        <div class="kpi">
          <div class="kpi-num" style="color: var(--cp-orange)">{{ n(total.total_tokens) }}</div>
          <div class="kpi-label">总 Token</div>
        </div>
      </div>

      <!-- 图表 -->
      <div class="chart-wrap">
        <n-spin :show="loading">
          <div v-if="rows.length === 0 && !loading" class="empty-wrap">
            <n-empty size="small" description="暂无统计数据" />
          </div>
          <div v-show="rows.length > 0" ref="chartEl" class="chart" />
        </n-spin>
      </div>

      <!-- 明细表 -->
      <div class="table-wrap">
        <table class="tbl">
          <thead>
            <tr>
              <th class="w-key">{{ groupBy === "hour" ? "小时" : groupBy === "day" ? "日期" : groupBy === "model" ? "模型" : "类型" }}</th>
              <th class="num">请求数</th>
              <th class="num">成功</th>
              <th class="num">失败</th>
              <th class="num">Prompt</th>
              <th class="num">Completion</th>
              <th class="num">缓存</th>
              <th class="num">推理</th>
              <th class="num">总计</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.key">
              <td class="mono dim">{{ fmtKey(row.key, groupBy) }}</td>
              <td class="num mono">{{ n(row.requests) }}</td>
              <td class="num mono">{{ n(row.success) }}</td>
              <td class="num mono">{{ n(row.failed) }}</td>
              <td class="num mono">{{ n(row.prompt_tokens) }}</td>
              <td class="num mono">{{ n(row.completion_tokens) }}</td>
              <td class="num mono">{{ n(row.cached_tokens) }}</td>
              <td class="num mono">{{ n(row.reasoning_tokens) }}</td>
              <td class="num mono">{{ n(row.total_tokens) }}</td>
            </tr>
          </tbody>
        </table>
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
.filters {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.dim-switch {
  display: inline-flex;
  gap: 4px;
  background: var(--cp-panel-2);
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  padding: 3px;
}
.dim-btn {
  border: none;
  background: transparent;
  color: var(--cp-dim);
  font-size: 13px;
  padding: 4px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.dim-btn:hover {
  color: var(--cp-text);
}
.dim-btn.active {
  background: rgba(34, 211, 238, 0.15);
  color: var(--cp-cyan);
}
.err-banner {
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: var(--cp-red);
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
  margin-bottom: 10px;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  margin-bottom: 12px;
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
.chart-wrap {
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  min-height: 300px;
  margin-bottom: 12px;
  overflow: hidden;
}
.chart {
  width: 100%;
  height: 300px;
}
.empty-wrap {
  padding: 26px 0;
  display: flex;
  justify-content: center;
  align-items: center;
  height: 300px;
}
.table-wrap {
  border: 1px solid var(--cp-border);
  border-radius: 8px;
  overflow: auto;
  max-height: 360px;
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
  padding: 8px 10px;
  background: var(--cp-panel-2);
  border-bottom: 1px solid var(--cp-border);
}
.tbl td {
  padding: 7px 10px;
  border-bottom: 1px solid rgba(51, 65, 85, 0.5);
  line-height: 1.5;
}
.tbl tbody tr:hover {
  background: rgba(51, 65, 85, 0.35);
}
.tbl tbody tr:last-child td {
  border-bottom: none;
}
.w-key {
  min-width: 140px;
}
.num {
  text-align: right;
}
.mono.dim {
  color: var(--cp-dim);
}
</style>
