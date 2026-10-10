# Prompts for the OKU Kolo Slack tokens and stores them as User env vars. Never echoes values.
$names = @{ 'SLACK_OKU_WHEEL_BOT_TOKEN'='xoxb-'; 'SLACK_OKU_WHEEL_APP_TOKEN'='xapp-' }
foreach ($n in 'SLACK_OKU_WHEEL_BOT_TOKEN','SLACK_OKU_WHEEL_APP_TOKEN') {
  do {
    $s = Read-Host "Vloz $n (zacina $($names[$n]))" -AsSecureString
    $v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)).Trim()
    if (-not $v.StartsWith($names[$n])) { Write-Host "Nezacina $($names[$n]), zkus znovu." -ForegroundColor Yellow }
  } until ($v.StartsWith($names[$n]))
  [Environment]::SetEnvironmentVariable($n, $v, 'User')
}
Write-Host "Ulozeno v registru:" -ForegroundColor Green
(Get-Item HKCU:\Environment).Property | Where-Object { $_ -like 'SLACK_OKU_WHEEL*' }
Read-Host "Hotovo, Enter pro zavreni"
