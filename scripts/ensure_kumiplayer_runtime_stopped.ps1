param(
    [int]$BackendPort = 37821,
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$resolvedRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\')
$releaseExecutable = Join-Path $resolvedRoot 'release\KumiPlayer.exe'
$activeShells = @(Get-CimInstance Win32_Process -Filter "Name = 'KumiPlayer.exe'" -ErrorAction SilentlyContinue)
foreach ($shell in $activeShells) {
    if ($shell.ExecutablePath -and [System.IO.Path]::GetFullPath($shell.ExecutablePath) -eq $releaseExecutable) {
        throw "The workspace KumiPlayer.exe is still running (PID $($shell.ProcessId)). Close it normally before building."
    }
}

$listeners = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
    Write-Host "KumiPlayer runtime preflight passed: port $BackendPort is free."
    exit 0
}
if ($listeners.Count -ne 1) {
    throw "Port $BackendPort has multiple listeners. No process was stopped."
}

$listener = $listeners[0]
$owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
if ($null -eq $owner) {
    throw "The listener on port $BackendPort cannot be inspected. No process was stopped."
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$BackendPort/api/health" -TimeoutSec 2
} catch {
    throw "Port $BackendPort belongs to another program or an unresponsive service. No process was stopped."
}
if ($health.app -ne 'KumiPlayer') {
    throw "Port $BackendPort belongs to another program. No process was stopped."
}

$allowedNames = @('pythonw.exe', 'python.exe', 'KumiPlayerBackend.exe')
if ($owner.Name -notin $allowedNames) {
    throw "Port $BackendPort returned KumiPlayer health, but process $($owner.Name) is not allowlisted. No process was stopped."
}

$parent = $null
if ([int]$owner.ParentProcessId -gt 0) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($owner.ParentProcessId)" -ErrorAction SilentlyContinue
}
if ($null -ne $parent) {
    throw "The KumiPlayer backend still has an active parent $($parent.Name) (PID $($parent.ProcessId)). No process was stopped."
}

$orphanPid = [int]$owner.ProcessId
Stop-Process -Id $orphanPid -Force
Start-Sleep -Milliseconds 500
if (Get-Process -Id $orphanPid -ErrorAction SilentlyContinue) {
    throw "The verified orphaned KumiPlayer backend PID $orphanPid did not stop."
}
$remaining = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue)
if ($remaining.Count -gt 0) {
    throw "The orphaned backend stopped, but port $BackendPort was reclaimed by another program."
}

Write-Host "Stopped verified orphaned KumiPlayer backend PID $orphanPid."
