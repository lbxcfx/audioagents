$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$exe = Join-Path $root "tools\microsip\MicroSIP.exe"

if (!(Test-Path $exe)) {
  throw "MicroSIP.exe not found: $exe"
}

Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
Write-Host "MicroSIP started."
Write-Host "Call sip:1000@127.0.0.1:5066 after running qwen-telephony/scripts/start-system-wsl.sh in WSL."
