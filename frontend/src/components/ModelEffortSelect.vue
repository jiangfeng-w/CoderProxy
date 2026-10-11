<script setup lang="ts">
// 模型「思考强度」选择组件（牛码 tab 与 WorkBuddy tab 共用）：
// 「透传」默认档置顶 + 「关」+ 目录档位 + 目录外标准档合并，hover tooltip 说明档位语义；
// 写入 config.thinking_defaults（思考强度透传档位：透传 = agent 传什么发什么；
// 具体档 = 强制下发、无视 agent 传参）。
import { h, type VNodeChild } from 'vue'
import { NSelect, NTooltip } from 'naive-ui'
import type { OaiModel } from '../api'
import { STANDARD_EFFORTS, STANDARD_EFFORT_LABELS, useConfigStore } from '../store'

defineProps<{ model: OaiModel }>()
const config = useConfigStore()

/** 透传档：agent 表态按官方客户端形状归一透传、未表态零注入，软件不干预。 */
const PASSTHROUGH = 'passthrough'

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
 * 具体档 = 强制下发（无视 agent 传参）；目录档位保证生效，目录外标注不保证。
 * 「关」的 GLM-5.3 风险提示仅对 glm-5.3 系列显示（官方文档称该系列不支持关思考）。 */
function effortTip(m: OaiModel, value: string): { head: string; body: string } {
  if (value === PASSTHROUGH) {
    return {
      head: '透传（默认）',
      body: 'agent 传来的思考强度按官方客户端形状归一后原样下发，软件不选档不干预；agent 未表态时零注入，落上游默认行为'
    }
  }
  if (value === 'none') {
    const body = m.id.toLowerCase().startsWith('glm-5.3') ? '注意：GLM-5.3 系列官方不支持关闭思考（直连会报错），牛码网关可能映射为低档位，以实测为准' : '无视 agent 传参，一律显式关闭思考（强制）'
    return { head: '显式关闭思考（强制）', body }
  }
  const head = EFFORT_DESC[value] ?? value
  const body = (m.reasoning_efforts ?? []).includes(value) ? '强制下发该档（无视 agent 传参）；上游档位（官方客户端同款选项），保证生效' : '强制下发该档（无视 agent 传参）；目录外标准档位：上游未列出，网关可能映射到其他档位或报错，不保证生效'
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

/** 思考强度下拉选项：透传（默认）置顶，关（none）次之；目录档位 + 目录外标准档合并后
 * 统一按强度排序（微/低/中/高/超高/最大）。每个档位统一「中文名（level）·来源」格式：
 * 上游档位标「·上游」（无上游中文名时兜底标准名），目录外标「·目录外」，详细说明在 hover tooltip。 */
function effortOptions(m: OaiModel): { label: string; value: string }[] {
  const labels = m.reasoning_labels ?? {}
  const catalogLevels = m.reasoning_efforts ?? []
  const order = (lv: string) => {
    const i = (STANDARD_EFFORTS as readonly string[]).indexOf(lv)
    return i === -1 ? 99 : i
  }
  const all = [...new Set([...catalogLevels, ...STANDARD_EFFORTS])].sort((a, b) => order(a) - order(b))
  const opts = all.map(lv => {
    if (catalogLevels.includes(lv)) {
      const name = labels[lv] ?? STANDARD_EFFORT_LABELS[lv] ?? lv
      return { label: `${name}（${lv}）·上游`, value: lv }
    }
    return { label: `${STANDARD_EFFORT_LABELS[lv] ?? lv}（${lv}）·目录外`, value: lv }
  })
  return [{ label: '透传（默认）', value: PASSTHROUGH }, { label: '关（不思考）', value: 'none' }, ...opts]
}

/** 当前生效的思考档位（未配置 = 透传）。 */
function effortValue(m: OaiModel): string {
  return config.thinking_defaults[m.id] ?? PASSTHROUGH
}

/** 写入某模型的思考档位（整体覆盖写回，收敛走 config store 响应）。 */
async function onEffortChange(m: OaiModel, v: string) {
  try {
    await config.update({ thinking_defaults: { ...config.thinking_defaults, [m.id]: v } })
  } catch (e) {
    console.error(e)
  }
}
</script>

<template>
  <NSelect
    v-if="(model.reasoning_efforts ?? []).length > 0"
    size="small"
    :value="effortValue(model)"
    :options="effortOptions(model)"
    :render-option="effortRenderer(model)"
    :consistent-menu-width="false"
    title="透传（默认）= agent 传什么发什么；选具体档 = 强制下发（无视 agent 传参）。hover 各选项查看说明（仅上游目录档位保证生效）"
    @update:value="(v: string) => onEffortChange(model, v)"
  />
  <span
    v-else
    class="dim"
    >{{ model.supports_reasoning ? '支持（无档位）' : '—' }}</span
  >
</template>

<style scoped>
.dim {
  color: var(--cp-dim);
}
</style>
