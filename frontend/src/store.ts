// 全局共享状态（Pinia）：运行时/进程状态 + 当前导航页 + 模型可用状态。
//
// 历史说明：旧版为模块级 reactive 单例；现迁移为 Pinia `useRuntimeStore`，并保留
// 模块级单例 `store` 导出（组件按 `import { store } from "./store"` 访问无需改动）。
// 工具模式 / 端口等**配置展示**不在此 store——见 `stores/config.ts`（useConfigStore），
// 顶栏/配置页以 config store 为唯一来源，relay_status 只报进程状态（running/port）。
import { defineStore } from 'pinia'
import type { AuthStatus, RelayStatus } from './api'
import { pinia } from './pinia'

export const useRuntimeStore = defineStore('runtime', {
  state: () => ({
    /** relay 进程状态（running/port，来自壳 relay_status；不含任何配置字段）。 */
    relay: { running: false, port: 0 } as RelayStatus,
    auth: { status: 'not_logged_in' } as AuthStatus,
    /** 对 agent 的 OpenAI /v1 服务是否可用（relay 侧：已登录 且 未手动停止）。 */
    serviceEnabled: false,
    view: 'overview' as string,
    /** 模型连通状态：success / failure / testing（无记录 = 未测试）。跨页面共享，
     * 不持久化；与白名单启用无关——启用与否看 model_whitelist（isEnabled）。
     * 只由模型页手动测试写入（2026-10-07 起不再有登录后自动批量测试）。 */
    modelStatus: {} as Record<string, string>,
    /** 模型目录同步信号：账号区/Shell 自动同步成功后自增，牛码模型区 watch 它刷新列表。 */
    syncTick: 0
  })
})

/** 模块级单例实例（等价旧 store.ts 的 reactive 导出，组件用法不变）。 */
export const store = useRuntimeStore(pinia)

export { configStore, useConfigStore } from './stores/config'

/** 工具模式（tool_mode）在页面上的通俗叫法；代码/API 内部仍用原名 hybrid/strict/passthrough。 */
const TOOL_MODE_LABELS: Record<string, string> = {
  hybrid: '智能适配',
  strict: '严格模式',
  passthrough: '原样转发'
}

export function toolModeLabel(mode?: string): string {
  return TOOL_MODE_LABELS[mode ?? ''] ?? mode ?? '—'
}

/** 思考档位标准集（按强度升序；none 即「关」不入列）。
 * 依据国产厂商官方文档：智谱 GLM-5.2+ 官方枚举即 minimal/low/medium/high/xhigh/max
 * （none/minimal=放弃思考，low/medium→high，xhigh→max）；DeepSeek V4=high/max；
 * Kimi K3=low/high/max。上游目录 thinkingLevels 只是官方客户端 UI 选项集、非网关
 * 白名单（实测目录外档位如 deepseek 传 low 也被接受），故模型页下拉在目录档位外
 * 补全此集合并标注「不保证生效」。 */
export const STANDARD_EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const

/** 标准档位的兜底中文名（目录档位优先用上游自带标签，此处仅用于目录外档位/日志兜底）。 */
export const STANDARD_EFFORT_LABELS: Record<string, string> = {
  minimal: '微',
  low: '低',
  medium: '中',
  high: '高',
  xhigh: '超高',
  max: '极致'
}

/** GLM-5.3 系列上游（智谱）强制思考、不支持关闭：官方文档明确该系列
 * "use forced thinking and cannot be disabled"（仅认 low/high/max，直连关参数报 400/1210）；
 * 2026-10-11 WB 小号实测网关对 none/disabled/minimal 不报错也不降档，静默忽略、模型仍思考。
 * 模型 id 带平台前缀（如 牛码/glm-5.3-flash），故用包含匹配而非 startsWith。 */
export function offThinkingUnsupported(model?: string | null): boolean {
  return !!model && model.toLowerCase().includes('glm-5.3')
}
