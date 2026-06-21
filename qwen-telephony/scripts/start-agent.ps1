$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$agentDir = Join-Path $root.Path "qwen-telephony\agent"
$python = Join-Path $root.Path "qwen-telephony\.venv-win\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Windows venv not found: $python"
}

$env:QWEN_TTS_USE_SSE = "false"
$env:LIVEKIT_URL = "ws://127.0.0.1:7880"
Set-Location $agentDir
& $python phone_agent.py dev
