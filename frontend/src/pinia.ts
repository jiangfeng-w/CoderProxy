// Pinia 单例：在 store 模块顶层即可用 store 实例（绕过 setup 内 useStore 限制），
// main.ts 里 `app.use(pinia)` 注入组件树。
import { createPinia } from 'pinia'

export const pinia = createPinia()
