# M5: reproduce orphan - wait for BOTH sidecar processes (bootloader + child), then force-kill app
$ErrorActionPreference = "Continue"

Get-Process relay-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

$exe = "D:\Code\home\own-project\CoderProxy\src-tauri\target\release\coderproxy-desktop.exe"
$app = Start-Process -FilePath $exe -PassThru
Write-Host ("launched app pid={0}" -f $app.Id)

# wait until 2 relay-sidecar processes exist (bootloader + actual relay child)
$deadline = (Get-Date).AddSeconds(45)
$sc = @()
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 500
  $sc = @(Get-Process relay-sidecar -ErrorAction SilentlyContinue)
  if ($sc.Count -ge 2) { break }
}
Start-Sleep -Seconds 2  # let child fully start before killing (else false-PASS during startup)
Write-Host ("sidecar processes found: count={0} pids={1}" -f $sc.Count, (($sc | ForEach-Object { $_.Id }) -join ","))
if ($sc.Count -lt 2) {
  Write-Host "WARN: expected 2 sidecar processes but saw fewer; proceeding anyway"
}

# force-kill the desktop app
Stop-Process -Id $app.Id -Force
Start-Sleep -Seconds 3

$remaining = @(Get-Process relay-sidecar -ErrorAction SilentlyContinue)
if ($remaining.Count -eq 0) {
  Write-Host "RESULT: PASS - no sidecar process remains after force-kill"
  exit 0
} else {
  Write-Host ("RESULT: FAIL - orphan remains: pids={0}" -f (($remaining | ForEach-Object { $_.Id }) -join ","))
  $remaining | Stop-Process -Force -ErrorAction SilentlyContinue
  exit 2
}
