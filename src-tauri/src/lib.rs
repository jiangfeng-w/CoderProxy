//! CoderProxy 桌面壳（Tauri 2）。
//!
//! 职责（M4 spec §6.3）：
//! 1. spawn Python relay sidecar（`python run.py run --port <期望端口>`），解析 stdout 的
//!    `[relay-ready]` 行拿到实际端口与 API Key；
//! 2. 向前端暴露 invoke 命令：
//!    - `relay`：通用代理，GUI → Rust → HTTP(localhost:port) 转发 `/v1/*`；
//!    - `relay_status`：当前 sidecar 就绪状态（端口 / key / 工具模式）；
//!    - `relay_stop`：停止 sidecar；
//!    - `relay_restart`：按指定端口重启 sidecar（后台线程，前端轮询就绪）。
//! 3. 应用退出时杀掉 sidecar。
//!
//! 开发期 sidecar 用本机 python 跑 `relay/run.py`；M5 再切 PyInstaller 单文件 exe。

use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicU16, Ordering};
use std::sync::mpsc::{channel, RecvTimeoutError};
use std::sync::Mutex;
use std::time::Duration;

use serde::Serialize;
use tauri::{Manager, State};

/// sidecar 就绪信息（`[relay-ready]` 行解析结果）。
#[derive(Clone, Serialize)]
struct ReadyInfo {
    port: u16,
    api_key: String,
    tool_mode: String,
}

struct AppState {
    sidecar: Mutex<Option<Child>>,
    ready: Mutex<Option<ReadyInfo>>,
    /// GUI 配置页期望的端口；0 = 随机端口。重启时按此值 spawn。
    requested_port: AtomicU16,
    relay_dir: String,
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

fn resolve_python() -> String {
    if let Ok(p) = std::env::var("CODERPROXY_PYTHON") {
        if !p.trim().is_empty() {
            return p;
        }
    }
    "python".to_string()
}

/// 开发期 relay 目录：exe 在 `src-tauri/target/<profile>/` 下，relay 在其 ../../../relay。
/// （打包期 M5 再切为「exe 同目录」的捆绑布局。）
fn relay_dir(exe_dir: &std::path::Path) -> std::path::PathBuf {
    exe_dir.join("..").join("..").join("..").join("relay")
}

fn spawn_sidecar(app: &tauri::AppHandle) -> Result<ReadyInfo, String> {
    let state = app.state::<AppState>();
    let port = state.requested_port.load(Ordering::SeqCst);
    spawn_with_port(&state, port)
}

/// 按指定端口 spawn 并等待 `[relay-ready]`；成功后将 child/ready 写入 AppState。
fn spawn_with_port(state: &AppState, port: u16) -> Result<ReadyInfo, String> {
    let mut child = Command::new(resolve_python())
        .args(["run.py", "run", "--port", &port.to_string()])
        .current_dir(&state.relay_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit()) // 开发期控制台可见；打包后走 M5 日志落盘
        .spawn()
        .map_err(|e| format!("spawn sidecar 失败（python 与 relay 依赖就绪？）: {e}"))?;

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "无法获取 sidecar stdout".to_string())?;

    // 读线程：逐行扫 stdout，命中 [relay-ready] 即通知主线程
    let (tx, rx) = channel();
    std::thread::spawn(move || {
        for line in std::io::BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(info) = parse_ready(&line) {
                let _ = tx.send(info);
                break;
            }
        }
    });

    match rx.recv_timeout(Duration::from_secs(30)) {
        Ok(info) => {
            *state.sidecar.lock().unwrap() = Some(child);
            *state.ready.lock().unwrap() = Some(info.clone());
            println!("[shell] sidecar 就绪 port={} tool_mode={}", info.port, info.tool_mode);
            Ok(info)
        }
        Err(RecvTimeoutError::Timeout) => {
            let _ = child.kill();
            Err("sidecar 30s 内未就绪（检查 python 与 relay 依赖）".to_string())
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
    if let Some(mut child) = state.sidecar.lock().unwrap().take() {
        let _ = child.kill();
        let _ = child.wait();
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
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|p| p.to_path_buf()))
        .unwrap_or_default();
    let relay_dir = relay_dir(&exe_dir);

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            sidecar: Mutex::new(None),
            ready: Mutex::new(None),
            requested_port: AtomicU16::new(0),
            relay_dir: relay_dir.to_string_lossy().into_owned(),
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
            // 退出时杀掉 sidecar
            if let tauri::RunEvent::Exit = event {
                if let Some(mut child) = app_handle.state::<AppState>().sidecar.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        });
}
