param(
    [string]$Number = "1000@127.0.0.1:5066",
    [int]$Seconds = 10,
    [double]$MaxFirstFrameSeconds = 3.0
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$app = Join-Path $root.Path "qwen-telephony"
$microSipDir = Join-Path $root.Path "tools\microsip"
$microSip = Join-Path $microSipDir "MicroSIP.exe"
$microSipLog = Join-Path $microSipDir "MicroSIP_log.txt"

if (-not (Test-Path $microSip)) {
    throw "MicroSIP.exe not found: $microSip"
}

try {
    Invoke-RestMethod -Uri "http://127.0.0.1:18081/worker" -TimeoutSec 5 | Out-Null
} catch {
    throw "Agent worker is not reachable on http://127.0.0.1:18081/worker"
}

Start-Process -FilePath $microSip -WorkingDirectory $microSipDir -ArgumentList "/hangupall" -WindowStyle Hidden
Start-Sleep -Seconds 1
Get-Process MicroSIP -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

if (Test-Path $microSipLog) {
    Clear-Content -Path $microSipLog
}

$startedUtc = (Get-Date).ToUniversalTime().ToString("o")
Start-Process -FilePath $microSip -WorkingDirectory $microSipDir -ArgumentList $Number -WindowStyle Hidden
Start-Sleep -Seconds $Seconds
Start-Process -FilePath $microSip -WorkingDirectory $microSipDir -ArgumentList "/hangupall" -WindowStyle Hidden
Start-Sleep -Seconds 2
Start-Process -FilePath $microSip -WorkingDirectory $microSipDir -ArgumentList "/exit" -WindowStyle Hidden
Start-Sleep -Seconds 2
Get-Process MicroSIP -ErrorAction SilentlyContinue | Stop-Process -Force

$agentLog = Join-Path $app "logs\agent.log"
$analyzer = Join-Path $app "scripts\analyze-greeting-log.py"
$wslAgentLog = "/mnt/" + $agentLog.Substring(0,1).ToLower() + $agentLog.Substring(2).Replace("\", "/")
$wslAnalyzer = "/mnt/" + $analyzer.Substring(0,1).ToLower() + $analyzer.Substring(2).Replace("\", "/")
$agentSummary = wsl -d Ubuntu -- python3 $wslAnalyzer `
    --log $wslAgentLog `
    --started-utc $startedUtc `
    --max-first-frame-seconds $MaxFirstFrameSeconds

$microSipSummary = @()
if (Test-Path $microSipLog) {
    $microSipSummary = Get-Content $microSipLog |
        Select-String -Pattern "Response msg 200|CONFIRMED|Jitter buffer starts returning normal frames|BYE" |
        Select-Object -Last 20
}

Write-Host "MicroSIP timing:"
$microSipSummary | ForEach-Object { Write-Host $_.Line }
Write-Host ""
Write-Host "Agent timing:"
$agentSummary | ForEach-Object { Write-Host $_ }

if (($LASTEXITCODE -ne 0) -or ($agentSummary -match "^RESULT fail")) {
    throw "Greeting automatic test failed"
}
