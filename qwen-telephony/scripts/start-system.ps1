param(
  [string]$WslDistro = "Ubuntu",
  [switch]$RestartInfra,
  [switch]$NoMicroSIP,
  [switch]$SkipPrewarm,
  [string]$ExplicitAgentName = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$appRoot = Join-Path $repoRoot.Path "qwen-telephony"
$venvRoot = Join-Path $appRoot ".venv-win"
$python = Join-Path $venvRoot "Scripts\python.exe"
$activate = Join-Path $venvRoot "Scripts\Activate.ps1"
$logsDir = Join-Path $appRoot "logs"
$serverLog = Join-Path $appRoot "server-current.out.log"
$agentLog = Join-Path $appRoot "agent-current.out.log"

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

function Write-Step {
  param([string]$Message)
  Write-Host ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message)
}

function Quote-PS {
  param([string]$Value)
  return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToWslPath {
  param([string]$Path)
  $resolved = Resolve-Path $Path
  return "/mnt/" + $resolved.Path.Substring(0, 1).ToLower() + $resolved.Path.Substring(2).Replace("\", "/")
}

function Import-EnvFile {
  param(
    [string]$Path,
    [bool]$Override = $true
  )
  if (-not (Test-Path $Path)) {
    return
  }

  foreach ($line in Get-Content $Path) {
    $trimmed = $line.Trim()
    if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
      continue
    }

    $parts = $trimmed.Split("=", 2)
    $name = $parts[0].Trim()
    $value = $parts[1].Trim().Trim('"').Trim("'")
    if ($Override -or -not [Environment]::GetEnvironmentVariable($name, "Process")) {
      [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
  }
}

function Env-OrDefault {
  param(
    [string]$Name,
    [string]$Default
  )
  $value = [Environment]::GetEnvironmentVariable($Name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    return $Default
  }
  return $value
}

function Test-Http {
  param([string]$Url)
  try {
    Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Wait-Http {
  param(
    [string]$Url,
    [string]$Name,
    [int]$TimeoutSeconds = 60
  )

  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-Http $Url) {
      Write-Step "$Name is ready: $Url"
      return
    }
    Start-Sleep -Seconds 1
  }

  throw "$Name did not become ready in ${TimeoutSeconds}s: $Url"
}

function Invoke-Wsl {
  param([string]$Command)
  wsl -d $WslDistro -- bash -lc $Command
  if ($LASTEXITCODE -ne 0) {
    throw "WSL command failed: $Command"
  }
}

function Test-WslInfra {
  $states = @(wsl -d $WslDistro -- bash -lc "docker inspect -f '{{.State.Running}}' qwen-livekit-redis qwen-livekit qwen-livekit-sip 2>/dev/null")
  if ($LASTEXITCODE -ne 0 -or $states.Count -ne 3) {
    return $false
  }
  foreach ($state in $states) {
    if ($state.Trim() -ne "true") {
      return $false
    }
  }
  return $true
}

function Ensure-WindowsVenv {
  if (-not (Test-Path $python)) {
    throw "Windows venv not found: $python"
  }

  & $python -c "import fastapi, uvicorn, livekit.agents" 2>$null
  if ($LASTEXITCODE -eq 0) {
    return
  }

  Write-Step "Installing missing Windows Python dependencies"
  & $python -m pip install -r (Join-Path $appRoot "requirements.txt")
  if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed"
  }
}

function Stop-ProjectWindowsProcesses {
  $patterns = @(
    "uvicorn server.main:app",
    "agent\phone_agent.py start",
    "agent/phone_agent.py start",
    "phone_agent.py start",
    "phone_agent.py dev"
  )

  $currentPid = $PID
  $processes = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    if (-not $cmd -or $_.ProcessId -eq $currentPid) {
      return $false
    }
    foreach ($pattern in $patterns) {
      if ($cmd -like "*$pattern*") {
        return $true
      }
    }
    return $false
  }

  foreach ($process in $processes) {
    Write-Step "Stopping stale Windows process pid=$($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  }
}

function Stop-WslAgent {
  $cleanup = "pkill -f 'python -u phone_agent.py start' >/dev/null 2>&1 || true; pkill -f 'python -u phone_agent.py dev' >/dev/null 2>&1 || true; pkill -f 'multiprocessing.forkserver.*livekit.plugins' >/dev/null 2>&1 || true"
  wsl -d $WslDistro -- bash -lc $cleanup | Out-Null
}

function Ensure-Infrastructure {
  $livekitHttpUrl = Env-OrDefault "LIVEKIT_HTTP_URL" "http://127.0.0.1:7880"
  $wslRoot = Convert-ToWslPath $repoRoot.Path

  if ($RestartInfra -or -not (Test-WslInfra) -or -not (Test-Http $livekitHttpUrl)) {
    Write-Step "Starting LiveKit and SIP containers"
    Invoke-Wsl "cd '$wslRoot' && qwen-telephony/scripts/start-infra-wsl.sh"
  } else {
    Write-Step "LiveKit and SIP containers are already healthy"
  }

  Write-Step "Ensuring SIP trunk and dispatch rule"
  Invoke-Wsl "cd '$wslRoot' && qwen-telephony/scripts/init-sip-wsl.sh"
  Wait-Http $livekitHttpUrl "LiveKit" 60
}

function Start-DialogueServer {
  Write-Step "Starting dialogue backend on 127.0.0.1:8091"
  $command = @"
Set-Location $(Quote-PS $appRoot)
. $(Quote-PS $activate)
`$env:PYTHONPATH = $(Quote-PS $appRoot)
`$env:QWEN_TTS_USE_SSE = 'false'
`$env:QWEN_TTS_CACHE_ENABLED = 'true'
& $(Quote-PS $python) -m uvicorn server.main:app --host 127.0.0.1 --port 8091 *> $(Quote-PS $serverLog)
"@
  Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) -WindowStyle Hidden | Out-Null
  Wait-Http "http://127.0.0.1:8091/api/dialogue/config" "Dialogue backend" 60
}

function Prewarm-DialogueAudio {
  if ($SkipPrewarm) {
    Write-Step "Dialogue TTS prewarm skipped"
    return
  }

  $sceneId = Env-OrDefault "QWEN_DIALOGUE_SCENE_ID" "2"
  Write-Step "Prewarming cached TTS audio for scene=$sceneId"
  Push-Location $appRoot
  try {
    & $python "scripts\prewarm-dialogue-audio.py" --scene-id $sceneId
    if ($LASTEXITCODE -ne 0) {
      Write-Step "TTS prewarm failed; services will still start"
    }
  } finally {
    Pop-Location
  }
}

function Start-Agent {
  $livekitUrl = Env-OrDefault "LIVEKIT_URL" "ws://127.0.0.1:7880"
  $apiKey = Env-OrDefault "LIVEKIT_API_KEY" "devkey"
  $apiSecret = Env-OrDefault "LIVEKIT_API_SECRET" "secret"
  $dialogueUrl = Env-OrDefault "QWEN_DIALOGUE_URL" "http://127.0.0.1:8091/api/dialogue/turn"
  $sceneId = Env-OrDefault "QWEN_DIALOGUE_SCENE_ID" "2"
  $nluEnabled = Env-OrDefault "QWEN_NLU_ENABLED" "true"
  $loadThreshold = Env-OrDefault "QWEN_AGENT_LOAD_THRESHOLD" "0.95"
  $roomAudioSampleRate = Env-OrDefault "QWEN_ROOM_AUDIO_SAMPLE_RATE" "24000"

  Write-Step "Starting LiveKit Agent on 127.0.0.1:18081"
  if ([string]::IsNullOrWhiteSpace($ExplicitAgentName)) {
    Write-Step "Agent dispatch mode: automatic direct-call dispatch"
  } else {
    Write-Step "Agent dispatch mode: explicit agent name '$ExplicitAgentName'"
  }

  $command = @"
Set-Location $(Quote-PS $appRoot)
. $(Quote-PS $activate)
`$env:PYTHONPATH = $(Quote-PS $appRoot)
`$env:LIVEKIT_URL = $(Quote-PS $livekitUrl)
`$env:LIVEKIT_API_KEY = $(Quote-PS $apiKey)
`$env:LIVEKIT_API_SECRET = $(Quote-PS $apiSecret)
`$env:QWEN_DIALOGUE_URL = $(Quote-PS $dialogueUrl)
`$env:QWEN_DIALOGUE_SCENE_ID = $(Quote-PS $sceneId)
`$env:QWEN_NLU_ENABLED = $(Quote-PS $nluEnabled)
`$env:QWEN_TTS_USE_SSE = 'false'
`$env:QWEN_TTS_CACHE_ENABLED = 'true'
`$env:QWEN_ROOM_AUDIO_SAMPLE_RATE = $(Quote-PS $roomAudioSampleRate)
`$env:QWEN_AGENT_LOAD_THRESHOLD = $(Quote-PS $loadThreshold)
`$env:QWEN_AGENT_EXPLICIT_NAME = $(Quote-PS $ExplicitAgentName)
`$env:LIVEKIT_AGENT_NAME = $(Quote-PS $ExplicitAgentName)
& $(Quote-PS $python) agent\phone_agent.py start *> $(Quote-PS $agentLog)
"@
  Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) -WindowStyle Hidden | Out-Null
  Wait-Http "http://127.0.0.1:18081/worker" "LiveKit Agent" 60
}

function Update-MicroSipAudioConfig {
  $microSipIni = Join-Path $repoRoot.Path "tools\microsip\microsip.ini"
  if (-not (Test-Path $microSipIni)) {
    return
  }

  $preferredCodecs = "G722/16000/1"
  $content = Get-Content -Raw $microSipIni
  $content = $content -replace "(?m)^audioCodecs=.*$", "audioCodecs=$preferredCodecs"
  $content = $content -replace "(?m)^forceCodec=.*$", "forceCodec=1"
  $content = $content -replace "(?m)^opusStereo=.*$", "opusStereo=0"
  Set-Content -Path $microSipIni -Value $content -Encoding ASCII
  Write-Step "MicroSIP audio codecs: $preferredCodecs"
}

function Start-MicroSIP {
  if ($NoMicroSIP) {
    Write-Step "MicroSIP launch skipped"
    return
  }

  $microSip = Join-Path $repoRoot.Path "tools\microsip\MicroSIP.exe"
  if (-not (Test-Path $microSip)) {
    Write-Step "MicroSIP not found: $microSip"
    return
  }

  Update-MicroSipAudioConfig
  Write-Step "Starting MicroSIP"
  Start-Process -FilePath $microSip -WorkingDirectory (Split-Path $microSip) | Out-Null
}

Import-EnvFile (Join-Path $repoRoot.Path ".env") $true
Import-EnvFile (Join-Path $appRoot "config\local.env") $true
[Environment]::SetEnvironmentVariable("QWEN_TTS_USE_SSE", "false", "Process")
[Environment]::SetEnvironmentVariable("QWEN_TTS_CACHE_ENABLED", "true", "Process")
[Environment]::SetEnvironmentVariable("QWEN_ROOM_AUDIO_SAMPLE_RATE", "24000", "Process")

Write-Step "One-click startup begins"
Write-Step "Fixes covered: backend process, Windows venv deps, SIP init, auto-dispatch agent mode, cached TTS prewarm"

Ensure-WindowsVenv
Ensure-Infrastructure
Stop-ProjectWindowsProcesses
Stop-WslAgent
Start-DialogueServer
Prewarm-DialogueAudio
Start-Agent
Start-MicroSIP

$sipNumber = Env-OrDefault "SIP_INBOUND_NUMBER" "1000"
$sipPort = Env-OrDefault "SIP_PORT" "5066"
Write-Step "System ready"
Write-Host "Ops UI:     http://127.0.0.1:8091/"
Write-Host "LiveKit:    $(Env-OrDefault 'LIVEKIT_URL' 'ws://127.0.0.1:7880')"
Write-Host "Agent:      http://127.0.0.1:18081/worker"
Write-Host "MicroSIP:   sip:${sipNumber}@127.0.0.1:${sipPort}"
Write-Host "Server log: $serverLog"
Write-Host "Agent log:  $agentLog"
