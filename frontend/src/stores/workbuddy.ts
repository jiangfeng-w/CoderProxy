// WorkBuddy 多账号 store（双域工厂化：useWorkbuddyStoreFor(platform)，2026-10-14 定稿）。
//
// 真值 = relay platform_<platform>.json（/v1/platforms/{platform}/*）；本 store 是唯一前端镜像：
// - load() 拉账号列表（签到/配额/删除动作成功后局部收敛对应账号，避免整表重拉闪烁）；
// - 登录流程状态机：startLogin → pending（前端开浏览器 + 轮询）→ success/failed；
// - 轮询节奏由 WorkBuddyTab 组件驱动（2s 间隔，成功/失败/关闭弹窗即停）；
// - 双域：两池状态天然独立 → 按 platform 工厂化（defineStore('wb-'+platform) 惰性注册 +
//   模块级 Map 缓存实例保证幂等；不采用「单 store 加 platform 维度」）。
import { defineStore } from 'pinia'
import { listWbAccounts, wbCheckin, wbCheckinStatus, wbDeleteAccount, wbExport, wbIdeState, wbIdeSwitch, wbImportLocal, wbImportText, wbLoginCancel, wbLoginStart, wbLoginStatus, wbRefreshQuota, type WbAccount, type WbExportResult, type WbIdeState } from '../api'
import { pinia } from '../pinia'

type LoginPhase = 'idle' | 'starting' | 'pending' | 'success' | 'failed'

/** CN 平台 id（默认；存量调用点零改动）。 */
export const WB_CN_PLATFORM = 'workbuddy'
/** 国际版平台 id。 */
export const WB_GLOBAL_PLATFORM = 'workbuddy-global'

function defineWorkbuddyStore(platform: string) {
  return defineStore(`wb-${platform}`, {
    state: () => ({
      platform,
      loaded: false,
      accounts: [] as WbAccount[],
      /** 登录流程：starting（后端已受理未返回）→ pending（等待浏览器授权）→ success/failed。 */
      loginPhase: 'idle' as LoginPhase,
      loginId: '',
      authorizeUrl: '',
      loginError: '',
      /** 操作中的账号（签到/配额/切号按钮 loading 定向到对应卡片）。 */
      busyUid: '' as string,
      /** 本机官方客户端概览（IDE 切号；CN 专属）；null = 尚未拉取/不可用。 */
      ideState: null as WbIdeState | null
    }),
    actions: {
      /** 拉取账号列表（挂载/登录成功后；动作成功走局部收敛不整表重拉）。 */
      async load(): Promise<void> {
        const res = await listWbAccounts(this.platform)
        this.accounts = res.accounts
        this.loaded = true
      },
      /** 拉取本机官方客户端概览（CN 专属；失败静默——概览缺失不阻塞账号区）。 */
      async loadIdeState(): Promise<void> {
        try {
          this.ideState = await wbIdeState()
        } catch {
          this.ideState = null
        }
      },
      /** 用最新 AccountView 局部替换对应账号（签到/配额刷新的收敛入口）。 */
      applyAccount(account: WbAccount): void {
        const i = this.accounts.findIndex(a => a.uid === account.uid)
        if (i >= 0) this.accounts[i] = account
        else this.accounts.push(account)
      },
      async startLogin(): Promise<void> {
        this.loginPhase = 'starting'
        this.loginError = ''
        const res = await wbLoginStart(this.platform)
        this.loginId = res.login_id
        this.authorizeUrl = res.authorize_url
        this.loginPhase = 'pending'
      },
      async pollLogin(): Promise<'pending' | 'success' | 'failed'> {
        if (this.loginPhase !== 'pending' || !this.loginId) return 'failed'
        const res = await wbLoginStatus(this.loginId, this.platform)
        if (res.status === 'success') {
          this.loginPhase = 'success'
          await this.load()
        } else if (res.status === 'failed') {
          this.loginPhase = 'failed'
          this.loginError = res.error || '登录失败'
        }
        return res.status
      },
      async cancelLogin(): Promise<void> {
        if (this.loginId) {
          try {
            await wbLoginCancel(this.loginId, this.platform)
          } catch {
            /* 取消失败不阻塞 UI 复位 */
          }
        }
        this.loginPhase = 'idle'
        this.loginId = ''
        this.authorizeUrl = ''
      },
      async remove(uid: string): Promise<void> {
        await wbDeleteAccount(uid, this.platform)
        this.accounts = this.accounts.filter(a => a.uid !== uid)
      },
      async checkin(uid: string): Promise<{ success: boolean; message: string }> {
        this.busyUid = uid
        try {
          const res = await wbCheckin(uid, this.platform)
          this.applyAccount(res.account)
          return { success: res.result.success, message: res.result.message }
        } finally {
          this.busyUid = ''
        }
      },
      async refreshQuota(uid: string): Promise<void> {
        this.busyUid = uid
        try {
          this.applyAccount(await wbRefreshQuota(uid, this.platform))
        } finally {
          this.busyUid = ''
        }
      },
      async refreshCheckinStatus(uid: string): Promise<void> {
        this.busyUid = uid
        try {
          this.applyAccount(await wbCheckinStatus(uid, this.platform))
        } finally {
          this.busyUid = ''
        }
      },
      /** IDE 切号（CN 专属）：凭证写入本机官方客户端并重启（成功后刷新概览）。 */
      async switchIde(uid: string): Promise<{ current_uid: string }> {
        this.busyUid = uid
        try {
          const res = await wbIdeSwitch(uid)
          await this.loadIdeState()
          return res
        } finally {
          this.busyUid = ''
        }
      },
      /** 粘贴 Token/JSON 或 JSON 文件内容导入（同一解析入口；成功后整表重拉）。 */
      async importText(text: string): Promise<number> {
        const res = await wbImportText(text, this.platform)
        await this.load()
        return res.imported
      },
      /** 从本机官方客户端登录态导入（CN 专属；可能正是 IDE 当前号，顺带刷新概览）。 */
      async importLocal(): Promise<number> {
        const res = await wbImportLocal()
        await this.load()
        await this.loadIdeState()
        return res.imported
      },
      /** 导出（CN 专属；cockpit 兼容 / 9router 读取合并流）；内容由组件负责下载或复制。 */
      async exportAccounts(uids: string[], format: 'cockpit' | '9router'): Promise<WbExportResult> {
        return wbExport(uids, format)
      }
    }
  })
}

// 模块级工厂缓存：同一 platform 恒同 store 实例（Pinia 同 id 重复 defineStore 本身安全，
// 缓存的目的是避免每次调用新建 defineStore 定义与查找开销）
type WbStoreDef = ReturnType<typeof defineWorkbuddyStore>
type WbStore = ReturnType<WbStoreDef>

const storeFactories = new Map<string, WbStoreDef>()
const storeInstances = new Map<string, WbStore>()

/** 按 platform 取 WorkBuddy store（双域工厂：'workbuddy' / 'workbuddy-global'）。 */
export function useWorkbuddyStoreFor(platform: string = WB_CN_PLATFORM): WbStore {
  const key = platform || WB_CN_PLATFORM
  let factory = storeFactories.get(key)
  if (!factory) {
    factory = defineWorkbuddyStore(key)
    storeFactories.set(key, factory)
  }
  let instance = storeInstances.get(key)
  if (!instance) {
    instance = factory(pinia)
    storeInstances.set(key, instance)
  }
  return instance
}

/** 全局单例实例（CN；存量组件/调用点兼容）。 */
export const workbuddyStore = useWorkbuddyStoreFor(WB_CN_PLATFORM)
