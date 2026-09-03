//! CoderProxy 桌面壳（Tauri 2）。
//!
//! 职责（M4 spec §6.3）：
//! 1. spawn relay sidecar（PyInstaller 打包的 `relay-sidecar`，经 `bundle.externalBin`
//!    随壳分发），解析 stdout 的 `[relay-ready]` 行拿到实际端口与 API Key；
//! 2. 向前端暴露 invoke 命令：
//!    - `relay`：通用代理，GUI → Rust → HTTP(localhost:port) 转发 `/v1/*`；
//!    - `relay_status`：当前 sidecar 就绪状态（端口 / key / 工具模式）；
//!    - `relay_stop`：停止 sidecar；
//!    - `relay_restart`：按指定端口重启 sidecar（后台线程，前端轮询就绪）。
//! 3. 应用退出时杀掉 sidecar。
//!
//! M5：sidecar 用 PyInstaller onefile exe（`binaries/relay-sidecar-<triple>.exe`），
//! 数据目录固定为 `app_data_dir`（打包态 `__file__` 指向解包目录，不可落数据）。

use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::{Manager, State};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

/// Windows 作业对象：把 PyInstaller onefile 的整个进程树（bootloader + 实际 relay）
/// 纳入统一回收。句柄关闭（含宿主进程被强杀时内核自动关句柄）即终止作业内全部进程，
/// 避免 `child.kill()` 只杀 bootloader 而残留 uvicorn 子进程占住端口。
#[cfg(windows)]
mod job {
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };
    use windows_sys::Win32::System::Threading::{
        OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    pub struct JobObject(HANDLE);

    // HANDLE 在 windows-sys 里是 `*mut c_void`，非 Send/Sync；
    // 句柄由 Mutex 保护、仅持有者 Drop，跨线程共享安全。
    // SAFETY: JobObject 只包一个进程句柄，赋值/关闭操作本身线程安全。
    unsafe impl Send for JobObject {}
    unsafe impl Sync for JobObject {}

    impl JobObject {
        /// 创建带 KILL_ON_CLOSE 的匿名作业对象；失败返回 None（调用方降级为仅 kill 直子）。
        pub fn new() -> Option<JobObject> {
            // SAFETY: 匿名作业对象，无需 SECURITY_ATTRIBUTES / 名称
            let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
            if handle.is_null() {
                return None;
            }
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            // SAFETY: info 指向已初始化的有效内存
            let ok = unsafe {
                SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &info as *const _ as *const _,
                    std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                )
            };
            if ok == 0 {
                // SAFETY: 关闭作业句柄
                unsafe { CloseHandle(handle) };
                return None;
            }
            Some(JobObject(handle))
        }

        /// 把 pid 进程纳入作业；其子进程默认继承作业成员，故一次 assign 覆盖整棵进程树。
        pub fn assign(&self, pid: u32) -> bool {
            // SAFETY: 按 pid 打开进程句柄（设置配额 + 终止权限）
            let h = unsafe { OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid) };
            if h.is_null() {
                return false;
            }
            // SAFETY: 分配进程到作业（Windows 8+ 支持嵌套作业，普通双击启动无冲突）
            let ok = unsafe { AssignProcessToJobObject(self.0, h) };
            // SAFETY: 关闭进程句柄
            unsafe { CloseHandle(h) };
            ok != 0
        }
    }

    impl Drop for JobObject {
        fn drop(&mut self) {
            // 关闭作业句柄 → KILL_ON_CLOSE 终止作业内全部进程
            // SAFETY: 关闭作业句柄
            unsafe { CloseHandle(self.0) };
        }
    }
}

/// 非 Windows 占位（本项目仅 Windows 分发，保编译完整）。
#[cfg(not(windows))]
mod job {
    pub struct JobObject;
    impl JobObject {
        pub fn new() -> Option<JobObject> {
            None
        }
        pub fn assign(&self, _pid: u32) -> bool {
            false
        }
    }
}

/// sidecar 就绪信息（`[relay-ready]` 行解析结果）。
#[derive(Clone, Serialize)]
struct ReadyInfo {
    port: u16,
    api_key: String,
    tool_mode: String,
}

struct AppState {
    sidecar: Mutex<Option<CommandChild>>,
    /// 回收 sidecar 整棵进程树的作业对象（PyInstaller onefile 两层结构防残留）。
    job: Mutex<Option<job::JobObject>>,
    ready: Mutex<Option<ReadyInfo>>,
    /// GUI 配置页期望的端口；0 = 随机端口。重启时按此值 spawn。
    requested_port: AtomicU16,
    client: reqwest::Client,
}

// ─────────────────────────── sidecar 生命周期 ───────────────────────────

/// 解析 `[relay-ready] port=... api_key=... tool_mode=...` 行。
fn parse_ready(line: &str) -> Option<ReadyInfo> {
    let rest = line.strip_prefix("[relay-ready] ")?;
    let mut port = 0u16;
    let mut api_key = String::new();
    let mut tool_mode = String::from("hybrid");
    for kv in rest.split_whitespace() {
        let (k, v) = kv.split_once('=')?;
        match k {
            "port" => port = v.parse().ok()?,
            "api_key" => api_key = v.to_string(),
            "tool_mode" => tool_mode = v.to_string(),
            _ => {}
        }
    }
    if port == 0 || api_key.is_empty() {
        return None;
    }
    Some(ReadyInfo { port, api_key, tool_mode })
}

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<ReadyInfo, String> {
    let state = app.state::<AppState>();
    let port = state.requested_port.load(Ordering::SeqCst);
    spawn_with_port(app, &state, port)
}

/// 按指定端口 spawn 打包 sidecar 并等待 `[relay-ready]`；成功后将 child/ready 写入 AppState。
fn spawn_with_port(app: &tauri::AppHandle, state: &AppState, port: u16) -> Result<ReadyInfo, String> {
    // 数据目录：%APPDATA%/com.coderproxy.desktop（打包态不可用 exe 解包目录）
    let data_dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("解析 app_data_dir 失败: {e}"))?;
    std::fs::create_dir_all(&data_dir).map_err(|e| format!("创建数据目录失败: {e}"))?;

    let (mut rx, child) = app
        .shell()
        .sidecar("relay-sidecar")
        .map_err(|e| format!("解析 sidecar 失败: {e}"))?
        .args(["run", "--port", &port.to_string()])
        .env("RELAY_DATA_DIR", data_dir.to_string_lossy().as_ref())
        .spawn()
        .map_err(|e| format!("spawn sidecar 失败（打包 exe 与依赖就绪？）: {e}"))?;

    // 读线程：逐行扫 stdout，命中 [relay-ready] 即通知主线程
    let (tx, ready_rx) = channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        while let Some(ev) = rx.blocking_recv() {
            match ev {
                CommandEvent::Stdout(bytes) => {
                    for b in bytes {
                        if b == b'\n' {
                            let line = String::from_utf8_lossy(&buf).trim().to_string();
                            buf.clear();
                            if let Some(info) = parse_ready(&line) {
                                let _ = tx.send(info);
                                return;
                            }
                        } else {
                            buf.push(b);
                        }
                    }
                }
                CommandEvent::Terminated(_) => return,
                _ => {}
            }
        }
    });

    match ready_rx.recv_timeout(Duration::from_secs(60)) {
        Ok(info) => {
            // 把 sidecar 整棵进程树（bootloader + 实际 relay）纳入作业对象，
            // 退出（含强杀）时统一回收，避免 onefile 子进程残留占住端口。
            if let Some(j) = job::JobObject::new() {
                j.assign(child.pid());
                *state.job.lock().unwrap() = Some(j);
            }
            *state.sidecar.lock().unwrap() = Some(child);
            *state.ready.lock().unwrap() = Some(info.clone());
            println!("[shell] sidecar 就绪 port={} tool_mode={}", info.port, info.tool_mode);
            Ok(info)
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            Err("sidecar 60s 内未就绪（检查打包 exe）".to_string())
        }
        Err(RecvTimeoutError::Disconnected) => {
            let _ = child.kill();
            Err("sidecar 提前退出（看上方 stderr）".to_string())
        }
    }
}

/// 停掉当前 sidecar 并清空就绪状态。
fn stop_sidecar(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    if let Some(child) = state.sidecar.lock().unwrap().take() {
        let _ = child.kill();
    }
    // 关闭作业对象句柄 → 终止作业内全部进程（兜底 onefile 子进程）
    if let Some(job) = state.job.lock().unwrap().take() {
        drop(job);
    }
    *state.ready.lock().unwrap() = None;
    println!("[shell] sidecar 已停止");
}

// ─────────────────────────── tauri 命令 ───────────────────────────

/// 通用代理：GUI → Rust → relay（仅放行 /v1/*，GET/POST）。
#[tauri::command]
async fn relay(
    state: State<'_, AppState>,
    method: String,
    path: String,
    body: Option<serde_json::Value>,
) -> Result<serde_json::Value, String> {
    if !path.starts_with("/v1/") {
        return Err(format!("路径受限（仅 /v1/*）: {path}"));
    }
    let ready = state
        .ready
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "relay 尚未就绪".to_string())?;

    let method = match method.to_uppercase().as_str() {
        "GET" => reqwest::Method::GET,
        "POST" => reqwest::Method::POST,
        _ => return Err("仅支持 GET/POST".to_string()),
    };
    let url = format!("http://127.0.0.1:{}{path}", ready.port);

    let mut req = state
        .client
        .request(method, &url)
        .header("Authorization", format!("Bearer {}", ready.api_key));
    if let Some(b) = body {
        req = req.json(&b);
    }
    let resp = req.send().await.map_err(|e| format!("relay 请求失败: {e}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        return Err(format!("relay HTTP {}: {}", status.as_u16(), text));
    }
    serde_json::from_str(&text)
        .map_err(|e| format!("relay 响应解析失败: {e}（{}）", &text[..text.len().min(200)]))
}

/// sidecar 就绪状态（前端轮询/初始化用）。
#[tauri::command]
fn relay_status(state: State<'_, AppState>) -> serde_json::Value {
    match state.ready.lock().unwrap().clone() {
        Some(r) => serde_json::json!({
            "running": true,
            "port": r.port,
            "api_key": r.api_key,
            "tool_mode": r.tool_mode,
        }),
        None => serde_json::json!({ "running": false }),
    }
}

/// 停止 relay sidecar（GUI「停止服务」按钮）。
#[tauri::command]
fn relay_stop(app: tauri::AppHandle) -> Result<(), String> {
    stop_sidecar(&app);
    Ok(())
}

/// 重启 relay sidecar（GUI「重启服务」按钮）。
///
/// `port`: 新的期望端口（None = 保持当前），0 = 随机端口。
/// 立即返回 `{"restarting": true}`，实际重启在后台线程执行，前端轮询 `relay_status` 感知就绪。
#[tauri::command]
async fn relay_restart(app: tauri::AppHandle, port: Option<u16>) -> Result<serde_json::Value, String> {
    if let Some(p) = port {
        app.state::<AppState>().requested_port.store(p, Ordering::SeqCst);
        println!("[shell] 请求端口 -> {p}");
    }
    let handle = app.clone();
    std::thread::spawn(move || {
        stop_sidecar(&handle);
        if let Err(e) = spawn_sidecar(&handle) {
            eprintln!("[shell] 重启失败: {e}");
        }
    });
    Ok(serde_json::json!({ "restarting": true }))
}

// ─────────────────────────── 入口 ───────────────────────────

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .manage(AppState {
            sidecar: Mutex::new(None),
            job: Mutex::new(None),
            ready: Mutex::new(None),
            requested_port: AtomicU16::new(0),
            client: reqwest::Client::new(),
        })
        .invoke_handler(tauri::generate_handler![relay, relay_status, relay_stop, relay_restart])
        .setup(|app| {
            // 后台线程 spawn，窗口立即显示；就绪后 relay_status 才返回 running=true
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = spawn_sidecar(&handle) {
                    eprintln!("[shell] {e}");
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // 退出时回收 sidecar：kill 直子 + 关作业对象句柄杀整棵进程树。
            // 强杀（任务管理器/崩溃）场景由内核关句柄自动触发 KILL_ON_CLOSE 兜底。
            if let tauri::RunEvent::Exit = event {
                if let Some(child) = app_handle.state::<AppState>().sidecar.lock().unwrap().take() {
                    let _ = child.kill();
                }
                if let Some(job) = app_handle.state::<AppState>().job.lock().unwrap().take() {
                    drop(job);
                }
            }
        });
}
