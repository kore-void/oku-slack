# Set OKU persona Slack tokens (values never echoed)
$apps=[ordered]@{ALENKA='A0C869C5FHN';BOURAK='A0C7BQCGCFL';MARTY='A0C75NR3QAF';PETA='A0C7A1R9HJS';KALOUSEK='A0C76MES88M'}
foreach($k in $apps.Keys){
  $id=$apps[$k]
  Write-Host "`n=== $k ===" -ForegroundColor Magenta
  Start-Process "https://api.slack.com/apps/$id/oauth"
  $b=Read-Host "Vloz Bot Token (xoxb-...) pro $k" -AsSecureString
  Start-Process "https://api.slack.com/apps/$id/general"
  $a=Read-Host "Vloz App Token 'socket' (xapp-...) pro $k" -AsSecureString
  $bv=[Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
  $av=[Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
  if(-not $bv.StartsWith('xoxb-')){Write-Host "Bot token nezacina xoxb-, preskakuju $k" -ForegroundColor Red; continue}
  if(-not $av.StartsWith('xapp-')){Write-Host "App token nezacina xapp-, preskakuju $k" -ForegroundColor Red; continue}
  [Environment]::SetEnvironmentVariable("SLACK_OKU_${k}_BOT_TOKEN",$bv,'User')
  [Environment]::SetEnvironmentVariable("SLACK_OKU_${k}_APP_TOKEN",$av,'User')
  Write-Host "$k OK" -ForegroundColor Green
}
Write-Host "`nHotovo. Napis Nautilovi 'mam'." -ForegroundColor Cyan
