// WorkBuddy 多账号 store（useWorkbuddyStore，对齐 stores/config.ts 拆分先例）。
//
// 真值 = relay platform_workbuddy.json（/v1/platforms/workbuddy/*）；本 store 是唯一前端镜像：
// - load() 拉账号列表（签到/配额/删除动作成功后局部收敛对应账号，避免整表重拉闪烁）；
// - 登录流程状态机：startLogin → pending（前端开浏览器 + 轮询）→ success/failed；
// - 轮询节奏由 WorkBuddyTab 组件驱动（2s 间隔，成功/失败/关闭弹窗即停）。
import { defineStore } from 'pinia'
import { listWbAccounts, wbCheckin, wbCheckinStatus, wbDeleteAccount, wbLoginCancel, wbLoginStart, wbLoginStatus, wbRefreshQuota, type WbAccount } from '../api'
import { pinia } from '../pinia'

type LoginPhase = 'idle' | 'starting' | 'pending' | 'success' | 'failed'

export const useWorkbuddyStore = defineStore('workbuddy', {
  state: () => ({
    loaded: false,
    accounts: [] as WbAccount[],
    /** 登录流程：starting（后端已受理未返回）→ pending（等待浏览器授权）→ success/failed。 */
    loginPhase: 'idle' as LoginPhase,
    loginId: '',
    authorizeUrl: '',
    loginError: '',
    /** 操作中的账号（签到/配额按钮 loading 定向到对应卡片）。 */
    busyUid: '' as string
  }),
  actions: {
    /** 拉取账号列表（挂载/登录成功后；动作成功走局部收敛不整表重拉）。 */
    async load(): Promise<void> {
      const res = await listWbAccounts()
      this.accounts = res.accounts
      this.loaded = true
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
      const res = await wbLoginStart()
      this.loginId = res.login_id
      this.authorizeUrl = res.authorize_url
      this.loginPhase = 'pending'
    },
    async pollLogin(): Promise<'pending' | 'success' | 'failed'> {
      if (this.loginPhase !== 'pending' || !this.loginId) return 'failed'
      const res = await wbLoginStatus(this.loginId)
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
          await wbLoginCancel(this.loginId)
        } catch {
          /* 取消失败不阻塞 UI 复位 */
        }
      }
      this.loginPhase = 'idle'
      this.loginId = ''
      this.authorizeUrl = ''
    },
    async remove(uid: string): Promise<void> {
      await wbDeleteAccount(uid)
      this.accounts = this.accounts.filter(a => a.uid !== uid)
    },
    async checkin(uid: string): Promise<{ success: boolean; message: string }> {
      this.busyUid = uid
      try {
        const res = await wbCheckin(uid)
        this.applyAccount(res.account)
        return { success: res.result.success, message: res.result.message }
      } finally {
        this.busyUid = ''
      }
    },
    async refreshQuota(uid: string): Promise<void> {
      this.busyUid = uid
      try {
        this.applyAccount(await wbRefreshQuota(uid))
      } finally {
        this.busyUid = ''
      }
    },
    async refreshCheckinStatus(uid: string): Promise<void> {
      this.busyUid = uid
      try {
        this.applyAccount(await wbCheckinStatus(uid))
      } finally {
        this.busyUid = ''
      }
    }
  }
})

/** 全局单例实例（跨组件共享）。 */
export const workbuddyStore = useWorkbuddyStore(pinia)
