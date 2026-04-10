$ErrorActionPreference = "SilentlyContinue"

# Always run from this script directory
Set-Location -Path $PSScriptRoot

$port = 8000

function Get-ListeningPids([int]$p) {
  # Keep it simple: match ':<port>' anywhere in the line
  $raw = netstat -ano
  $lines = $raw | Select-String (":{0}" -f $p)
  $pids = @()
  foreach ($ln in $lines) {
    $parts = ($ln.ToString() -split "\\s+") | Where-Object { $_ -ne "" }
    if ($parts.Count -ge 5) {
      $pid = $parts[-1]
      if ($pid -match "^[0-9]+$") { $pids += [int]$pid }
    }
  }
  return ($pids | Sort-Object -Unique)
}

# Kill-loop to avoid races (watchers/reloaders can respawn quickly)
for ($i = 0; $i -lt 6; $i++) {
  Write-Host ("[run_server] Checking port {0} (attempt {1}/6)..." -f $port, ($i + 1))
  $pids = Get-ListeningPids $port
  if ($pids.Count -eq 0) {
    Write-Host ("[run_server] Port {0} is free." -f $port)
    break
  }
  Write-Host ("[run_server] Port {0} is in use by PID(s): {1}. Killing..." -f $port, ($pids -join ", "))
  foreach ($pid in $pids) {
    try { taskkill /PID $pid /T /F | Out-Null } catch {}
  }
  Start-Sleep -Milliseconds 600
}

Write-Host "[run_server] Starting backend..."
& C:/Users/Han/miniconda3/envs/lglc/python.exe "$PSScriptRoot/graph.py"

