param(
  [string]$ListenAddress = '0.0.0.0',
  [string]$TargetAddress = '2.0.0.1',
  [int]$StartPort = 16384,
  [int]$EndPort = 16484
)

$ErrorActionPreference = 'Stop'
$targetIp = [System.Net.IPAddress]::Parse($TargetAddress)
$listenIp = [System.Net.IPAddress]::Parse($ListenAddress)
$jobs = @()

for ($port = $StartPort; $port -le $EndPort; $port++) {
  $jobs += Start-Job -ArgumentList $listenIp,$targetIp,$port -ScriptBlock {
    param($listenIp, $targetIp, $port)
    $clientEp = $null
    $fsEp = [System.Net.IPEndPoint]::new($targetIp, $port)
    $udp = [System.Net.Sockets.UdpClient]::new([System.Net.IPEndPoint]::new($listenIp, $port))
    $udp.Client.ReceiveTimeout = 200
    Write-Output "UDP relay ${port} -> $($targetIp.ToString()):${port}"
    while ($true) {
      try {
        $remote = [System.Net.IPEndPoint]::new([System.Net.IPAddress]::Any, 0)
        $buf = $udp.Receive([ref]$remote)
        if ($remote.Address.Equals($targetIp)) {
          if ($clientEp -ne $null) { [void]$udp.Send($buf, $buf.Length, $clientEp) }
        } else {
          $clientEp = $remote
          [void]$udp.Send($buf, $buf.Length, $fsEp)
        }
      } catch [System.Net.Sockets.SocketException] {
        if ($_.Exception.SocketErrorCode -ne [System.Net.Sockets.SocketError]::TimedOut) { throw }
      }
    }
  }
}

Write-Host "Started UDP RTP relay jobs for $StartPort-$EndPort to $TargetAddress. Keep this PowerShell process alive."
while ($true) { Start-Sleep -Seconds 5 }
