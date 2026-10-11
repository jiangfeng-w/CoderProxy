# WorkBuddy IDE 账号切换

> **状态**：方案已定稿（2026-10-11；密钥派生/解密全链路已在装机环境实测通过，见「本机实测证据」节）
> **优先级**：核心
> **来源**：用户需求「像 cockpit tools 一样切换 WorkBuddy 登录的账号」；承接「WorkBuddy多账号登录与签到」的账号池（该需求 UI 文案「添加账号并用于 IDE 切换」的预留意图落地）
> **隐私声明**：本文档不含任何真实账号、凭证、token；keyId 为密钥指纹（sha256 前 16 hex），非密钥本身。

## 背景 / 目标

账号池（`platform_workbuddy.json`）目前只服务中转出站（轮询选号）与签到/配额管理；本机官方 WorkBuddy 桌面客户端（`WorkBuddy.exe`）的登录账号只能手动登出重登。本期把账号池凭证**切换写入官方客户端登录态文件并重启客户端**，实现官方客户端登录账号的一键切换。

**用户决策**：
- 官方客户端运行中 → **自动关闭（taskkill）→ 写入 → 自动重启**（非拒绝式）
- 切换语义 = cockpit-tools 同款：写 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\workbuddy-desktop.info`，保留客户端内其它账号历史（accounts/allAccounts 数组），目标号 `lastLogin=true`
- **不做**：CodeBuddy CN IDE（`state.vscdb`）切换；「从本机客户端导入账号到池」；chat 账号池轮询选号逻辑不动（IDE 切换与中转轮询互不影响）

## 参考项目（本地克隆于 `d:\Code\ref-projects\`）

| 项目 | 许可 | 借鉴什么 | 关键文件 |
|---|---|---|---|
| **cockpit-tools**（jlcodes99，Rust） | MIT | **主参考**：`$wbEncrypted` 加解密（AAD 框架/信封格式）、密钥派生（Electron binding）、会话构造与写入流程 | `src-tauri/src/modules/workbuddy_auth_crypto.rs`（KEY_SCRIPT L25-36、`OfficialKey::aad` L49-60、open/seal L62-119）、`src-tauri/src/modules/workbuddy_account.rs`（会话构造 L1326-1559、写入 L1642-1753、路径 L1046-1094） |

> 实施纪律：逻辑照抄参考实现（含错误处理分支），每个文件头加「本地修订」注释说明与原实现的差异（Python 移植 + 接线形态）。

## 加密契约（`$wbEncrypted`，官方 WorkBuddy 5.6 字段加密，已实测）

### 密钥派生（只读，无副作用）

以 `ELECTRON_RUN_AS_NODE=1`（移除 `NODE_OPTIONS`）+ `-e <script>` 运行官方 exe（`CREATE_NO_WINDOW=0x08000000`，stdin 关闭，stderr 丢弃）：

```js
const c = require('crypto');
const p = JSON.parse(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
if (p.version !== 1 || typeof p.atRestSecretKey !== 'string') process.exit(2);
const b = Buffer.from(p.atRestSecretKey, 'base64');
if (b.length !== 32 || b.toString('base64') !== p.atRestSecretKey || b.every(x => x === 0)) process.exit(2);
const key = c.createHash('sha256').update(p.atRestSecretKey, 'utf8').digest();
process.stdout.write(key.toString('base64'));   // 恰 44 字节 = 32 字节密钥
```

- 官方 exe 路径候选（Windows）：`%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe`（本机命中）→ 后续候选 `%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe`，env `RELAY_WB_IDE_EXE` 可覆盖
- 超时 5s；密钥内存缓存 TTL 60s；asyncio 单飞锁防并发派生；密钥不落盘、不进日志

### 字段加密格式

- 包装：`{"$wbEncrypted": 1, "envelope": "<base64(Envelope JSON)>"}`（Envelope 字段序：suite/keyId/nonce/authTag/ciphertext，base64 标准 padding，serde `camelCase`）
- Envelope：`{"suite":1,"keyId":"<sha256(key)前16hex>","nonce":"<b64 12B>","authTag":"<b64 16B>","ciphertext":"<b64>"}`
- 算法：AES-256-GCM，key = sha256(`atRestSecretKey` utf8 文本)，AAD 见下；密文与 tag 分离存（tag = 追加密文末 16B）
- **AAD 框架**（`OfficialKey::aad`，逐字节对齐）：
  ```
  b"WB-AAD\x00\x01"
  + len("WBEV1").to_be_bytes(4) + b"WBEV1"
  + len("sym-v1").to_be_bytes(4) + b"sym-v1"
  + (1).to_be_bytes(4)                        # 计数
  + len(keyId).to_be_bytes(4) + keyId.encode()
  + bytes([2, 0, 0])                          # 字段框架（无序列号/结束位）
  ```

### 加解密字段白名单（仅这些路径允许出现/生成加密包装）

- 解密（decode）：`auth.accessToken`、`auth.refreshToken`；`account`、`accounts[]`、`allAccounts[]` 内的 `phoneNumber/departmentFullName/nickname`；**decode 后仍残留 `$wbEncrypted`（未知路径）→ 拒绝整个事务**（不落明文）
- 加密（encode）：同一字段集；`nickname`/`phoneNumber` 为 null/缺失跳过，空串也加密

## 写入流程（`switch_account`，照抄参考实现 7 步）

1. 前置：账号池有该 uid（404）；官方 exe 存在（否则 409）；taskkill `/IM WorkBuddy.exe /T /F` 关闭客户端
2. 读现有 auth 文件（不存在 = 全新构造）；`.logged-out` 标记文件的 sha256 先记下
3. 现有文件/含加密包装 → prepare 密钥 → decode（未知加密路径 → 拒绝）
4. 会话构造（以现有文件为底座）：
   - `account` 根：现有 `accounts[]`/`allAccounts[]` 中同 uid 项为底座（保留官方字段），补 uid/nickname/enterpriseId + `lastLogin:true`、`pluginEnabled:true`；无则最小构造（`type:"personal"`、`accountType/idp/oneidAccountId:""`、`areaInfoComplete/isCurrentOneIdEnterprise/isFirstLogin:false`、`deployStatus:{statusCode:0,...}`、`sso:{domain:"",domainModifiedTimes:0}`）
   - `auth` 根：最小构造（`accessToken/refreshToken/tokenType:"Bearer"/domain/lastRefreshTime:now`、`expiresAt`（账号池毫秒）+`expiresIn`（秒差）、`refreshExpiresAt=expiresAt`+`refreshExpiresIn`、`scope:"openid profile offline_access email"`）
   - `accounts`/`allAccounts`：同 uid 项替换为构造的 account 值，**其余项 `lastLogin=false`**；数组无该 uid 则 append
   - 顶层其余字段保留现有文件原样
5. encode（重新加密白名单字段，随机 nonce）→ `json.dumps(ensure_ascii=False, indent=2)` + 尾换行 → **hash 前置原子写**：写前重读文件 sha256 与步骤 2 不一致 → 拒绝（「官方客户端在切换期间更新了登录信息」）
6. 回读校验：decode 后 `auth.accessToken == 目标 token` 且 `account/auth/accounts/allAccounts` 四根字段齐全
7. 删 `.logged-out` 标记（hash 前置）；`Popen` 重启官方客户端（DETACHED_PROCESS | CREATE_NO_WINDOW）

## 本机实测证据（2026-10-11，装机环境，只读）

| 步骤 | 结果 |
|---|---|
| WorkBuddy.exe ELECTRON_RUN_AS_NODE 派生 | exit 0，stdout 恰 44 字节 base64，keyId 前缀 `9127dea1b44020a7` |
| 与 auth 文件 envelope.keyId 比对 | 完全一致（suite=1） |
| AES-256-GCM + AAD 解密 `auth.accessToken` | 解出 1383 字节 JWT（`eyJ...`），AAD 框架逐字节正确 |

> 环境快照：`%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe` 存在；auth 文件同时含 `Tencent-Cloud.coding-copilot.info`（CodeBuddy CN 共存）。Python 端 AES-GCM 需 `cryptography`（新增依赖）。

## 设计方案

### 核心决策

| 决策 | 结论 |
|---|---|
| 实现位置 | relay Python（业务逻辑在 relay、Tauri 壳只转发 `/v1/*` 的惯例；subprocess 派生密钥 Python 可做，已实测） |
| 新依赖 | `cryptography>=42`（AES-256-GCM；pyproject 登记 + 装入 `relay/_deps` 供测试/打包） |
| 路由 | 挂现有 `platforms` router，WorkBuddy 专用子路径（`ide-state`/`ide-switch` 是 WB 客户端特有概念，不做进通用 `{p}` 抽象） |
| 方法约束 | 只用 GET/POST（Tauri 壳转发只放行 `/v1/*` 的 GET/POST/DELETE，`src-tauri/src/lib.rs:402-427`） |
| 进程关闭 | `taskkill /IM WorkBuddy.exe /T /F`（按映像名；`/T` 覆盖 crashpad/helper 进程树）——写入前不关客户端会被退出时旧 token 回写覆盖 |
| 状态读取 | `account.uid` 在 auth 文件中**明文**——「当前 IDE 在用账号」无需密钥，零成本读取 |

### 新增/修改文件

```
relay/relay/platforms/workbuddy/ide_switch.py   # 全部新逻辑：密钥派生/加解密/会话构造/写入/进程管理/状态读取
relay/relay/platforms/routes.py                 # +2 端点（ide-state GET / ide-switch POST）+ 错误映射
relay/pyproject.toml                            # dependencies + cryptography
relay/tests/test_wb_ide_switch.py               # keyless 单测（对齐向量 + 全分支）
relay/relay-onefile.spec、relay/relay.spec      # hiddenimports 双保险登记 ide_switch
frontend/src/api.ts                             # WbIdeState 类型 + wbIdeState/wbIdeSwitch
frontend/src/stores/workbuddy.ts                # ideState state + loadIdeState/switchIde
frontend/src/views/providers/WorkBuddyTab.vue   # 「IDE 在用」标记 + 「切到此号」按钮（NPopconfirm）
docs/spec/WorkBuddy-IDE账号切换/探针-IDE切换只读校验.py   # 真机只读冒烟（解密 round-trip，不写）
```

### 端点清单（platforms/routes.py 追加）

| 方法/路径 | 职责 |
|---|---|
| GET `/v1/platforms/workbuddy/ide-state` | `{installed, exe_path, running, auth_exists, logged_out, current_uid}`（running 用 `tasklist /FI`；密钥不参与） |
| POST `/v1/platforms/workbuddy/accounts/{uid}/ide-switch` | 关客户端 → 写登录态 → 重启；返回 `{status:"ok", current_uid}` |

错误映射：账号不存在 → 404；官方客户端未安装/密钥派生失败/格式不支持/hash 冲突 → 409（detail 中文，前端直接展示）；其余 500。

### 单测口径（keyless，不依赖官方客户端/密钥/进程）

- **跨实现对齐向量**：复用 cockpit-tools `workbuddy_auth_crypto.rs` 测试 fixture（key=`[7;32]`、nonce=`[3;12]`、keyId=`4bb06f8e4e3a7715`、密文 `Q5fbdy9aO28OLyg5hXoZuWbUT3o=`、tag `8mXI80XfjtTaNENkKD5E5Q==`），Python 必须解出明文 `fixture-token-测试`——证明与 Rust/官方框架逐字节一致
- round-trip（seal→open）、篡改/错误 key/未知格式拒绝
- 会话构造：底座合并/lastLogin 翻转/新号 append/官方字段保留/auth 最小构造
- hash 前置拒绝、`.logged-out` 清理、路由三分支（mock 密钥/路径/进程函数，monkeypatch）

## 实施步骤（commit 序列）

| # | commit | 验证 |
|---|---|---|
| 1 | `feat: WorkBuddy IDE 登录态加解密与会话构造`（pyproject + _deps + ide_switch.py 加解密/构造 + test_wb_ide_switch.py） | pytest；对齐向量绿 |
| 2 | `feat: WorkBuddy IDE 切号编排与管理端点`（进程管理 + routes 2 端点 + 路由测试） | pytest 全量 + ruff |
| 3 | `feat: 供应商页 WorkBuddy 账号卡片「切到此号」`（api.ts/store/tab） | format:check + build |
| 4 | `chore: 打包登记 ide_switch + spec 文档`（两个 spec + 探针脚本 + 台账） | `py -3.13 build_sidecar.py` 产物冒烟 + 探针只读通过 |
| 5 | 手动验收：切换到账号 A → 客户端自动重启显示 A；切回 B；官方客户端手动改密码/登出后重试 | 硬性规则 5 不涉及（写本地文件，不触发上游风控；token 刷新仍走已有 ensure_token） |

## 已知边界

- 官方客户端版本升级改加密格式（suite≠1/keyId 变化/未知加密路径）→ 拒绝写 + 中文报错，绝不落明文（照抄参考实现保守策略）
- 写入窗口与官方客户端并发写 → hash 前置条件拒绝（重试即可）
- 全新客户端（无 auth 文件）→ 全量最小构造可写；`.logged-out` 存在时写成功后删标记
- 杀进程按映像名 `WorkBuddy.exe`：用户多开窗口一并关闭（切号本就要求全关，符合预期）
- keyId 随官方 `atRestSecretKey` 变化（重装客户端）→ 缓存失效由 TTL 60s + keyId 比对兜底
- 中转出站与 IDE 切号解耦：chat 轮询读账号池 token，不读 IDE 文件，切换动作不影响中转服务
