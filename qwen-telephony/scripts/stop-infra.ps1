$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$wslRoot = "/mnt/" + $root.Path.Substring(0,1).ToLower() + $root.Path.Substring(2).Replace("\", "/")

wsl -d Ubuntu -- bash -lc "cd '$wslRoot' && qwen-telephony/scripts/stop-infra-wsl.sh"
