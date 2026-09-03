# M5: diagnostic - inspect real sidecar process tree (parent/child) and job cleanup
$ErrorActionPreference = "Continue"

Get-Process relay-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$exe = "D:\Code\home\own-project\CoderProxy\src-tauri\target\release\coderproxy-desktop.exe"
$app = Start-Process -FilePath $exe -PassThru
Write-Host ("app pid={0}" -f $app.Id)

$deadline = (Get-Date).AddSeconds(45)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 500
  $sc = @(Get-Process relay-sidecar -ErrorAction SilentlyContinue)
  if ($sc.Count -ge 2) { break }
}
Start-Sleep -Seconds 2  # let tree stabilize
$tree = @(Get-CimInstance Win32_Process -Filter "Name='relay-sidecar.exe'")
Write-Host "--- sidecar tree before kill ---"
foreach ($p in $tree) {
  Write-Host ("pid={0} ppid={1}" -f $p.ProcessId, $p.ParentProcessId)
}

# force-kill app
Stop-Process -Id $app.Id -Force
Start-Sleep -Seconds 3
$after = @(Get-CimInstance Win32_Process -Filter "Name='relay-sidecar.exe'")
Write-Host "--- sidecar processes after kill ---"
if ($after.Count -eq 0) {
  Write-Host "none (clean)"
} else {
  foreach ($p in $after) { Write-Host ("pid={0} ppid={1}" -f $p.ProcessId, $p.ParentProcessId) }
}
$after | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
