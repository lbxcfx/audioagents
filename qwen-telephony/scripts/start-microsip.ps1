$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$exe = Join-Path $root "tools\microsip\MicroSIP.exe"
$ini = Join-Path $root "tools\microsip\microsip.ini"

if (!(Test-Path $exe)) {
  throw "MicroSIP.exe not found: $exe"
}

if (Test-Path $ini) {
  $preferredCodecs = "G722/16000/1"
  $content = Get-Content -Raw $ini
  $content = $content -replace "(?m)^audioCodecs=.*$", "audioCodecs=$preferredCodecs"
  $content = $content -replace "(?m)^forceCodec=.*$", "forceCodec=1"
  $content = $content -replace "(?m)^opusStereo=.*$", "opusStereo=0"
  Set-Content -Path $ini -Value $content -Encoding ASCII
  Write-Host "MicroSIP audio codecs: $preferredCodecs"
}

Start-Process -FilePath $exe -WorkingDirectory (Split-Path $exe)
Write-Host "MicroSIP started."
Write-Host "Call sip:1000@127.0.0.1:5066 after running qwen-telephony/scripts/start-system-wsl.sh in WSL."
