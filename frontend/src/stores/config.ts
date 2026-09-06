// 配置单一真值的前端镜像（useConfigStore）。
//
// 真值 = relay 磁盘持久化配置（GET/POST /v1/auth/config）；本 store 是唯一前端镜像：
// - 页面/顶栏只读本 store 展示（不再各自 getConfig 局部轮询）；
// - 写操作统一走 update()：后端「校验→落盘→改运行值→返回完整 config」成功后，
//   apply() 用响应整体替换本 store，并同步 api 层当前鉴权 key（D3）；
// - 所有消费方随响应自动收敛，无需手工局部同步。
import { defineStore } from 'pinia'
import { getConfig, setAuthKey, updateConfig, type Config } from '../api'
import { pinia } from '../pinia'

interface ConfigState extends Config {
  /** 是否已从 relay 装载过（就绪轮询据此补一次 load，装载前写操作应门控）。 */
  loaded: boolean
}

export const useConfigStore = defineStore('config', {
  state: (): ConfigState => ({
    loaded: false,
    base_url: '',
    host: '127.0.0.1',
    port: 3601,
    api_key: '',
    tool_mode: 'hybrid',
    model_whitelist: [],
    thinking_defaults: {},
    thinking_unset_mode: 'default'
  }),
  actions: {
    /** 用后端返回的完整 config 整体替换本 store（唯一收敛入口）。 */
    apply(cfg: Config): void {
      this.$patch({ loaded: true, ...cfg })
      setAuthKey(cfg.api_key)
    },
    /** 拉取后端当前配置（应用启动 / relay 重启就绪后 / 页面挂载兜底）。 */
    async load(): Promise<void> {
      this.apply(await getConfig())
    },
    /** 写配置：成功后以响应整体回写 store；失败抛错（store 保持原值）。 */
    async update(patch: Record<string, unknown>): Promise<Config> {
      const cfg = await updateConfig(patch)
      this.apply(cfg)
      return cfg
    }
  }
})

/** 全局单例实例（跨组件共享，等价旧 store.ts 的模块级 reactive 导出）。 */
export const configStore = useConfigStore(pinia)
