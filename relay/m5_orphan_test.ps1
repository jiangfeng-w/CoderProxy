# M5 research: verify PyInstaller onefile orphan behavior when parent is killed
$exe = "D:\Code\home\own-project\CoderProxy\src-tauri\target\release\relay-sidecar.exe"
$out = "$env:TEMP\m5_orphan_out.txt"
$dataDir = Join-Path $env:TEMP ("m5-orphan-" + [guid]::NewGuid().ToString("N"))

Remove-Item $out -ErrorAction SilentlyContinue
$env:RELAY_DATA_DIR = $dataDir
$proc = Start-Process -FilePath $exe -ArgumentList "run","--port","0" -PassThru -RedirectStandardOutput $out

$deadline = (Get-Date).AddSeconds(30)
$ready = $null
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 300
  if (Test-Path $out) {
    $ready = Get-Content $out -Raw -ErrorAction SilentlyContinue
    if ($ready -match "\[relay-ready\] port=(\d+)") { break }
  }
}
if (-not $ready -or $ready -notmatch "\[relay-ready\] port=(\d+)") {
  Write-Host "FAIL not ready"; Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; exit 1
}
$port = $Matches[1]
Write-Host ("OK ready: bootloader pid={0} port={1}" -f $proc.Id, $port)

$children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($proc.Id)"
if (-not $children) {
  Write-Host "note: no child process found (onefile may run single-process)"
} else {
  foreach ($c in $children) { Write-Host ("child: pid={0} name={1}" -f $c.ProcessId, $c.Name) }
}

# Simulate tauri-plugin-shell kill(): terminate only the direct child (bootloader)
Stop-Process -Id $proc.Id -Force
Start-Sleep -Seconds 2

$survivors = @()
foreach ($c in $children) {
  if (Get-Process -Id $c.ProcessId -ErrorAction SilentlyContinue) { $survivors += $c.ProcessId }
}
$portOpen = (Test-NetConnection -ComputerName 127.0.0.1 -Port $port -WarningAction SilentlyContinue).TcpTestSucceeded
Write-Host ("after killing parent: alive children={0} port{1} listening={2}" -f ($survivors -join ","), $port, $portOpen)

if ($survivors.Count -gt 0 -or $portOpen) {
  Write-Host "RESULT: orphan sidecar remains (need process-tree kill / job object)"
  foreach ($s in $survivors) { Stop-Process -Id $s -Force -ErrorAction SilentlyContinue }
  if ($portOpen) { Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } }
  exit 2
} else {
  Write-Host "RESULT: no orphan, single kill cleans up fully"
  exit 0
}
