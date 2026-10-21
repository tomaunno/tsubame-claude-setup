# Turns on the Windows ssh-agent service, so a key passphrase is typed once instead of at every login.
# Started from enable-ssh-agent.cmd; re-launches itself with administrator rights (Windows asks first).
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
$Host.UI.RawUI.WindowTitle = 'ssh-agent を有効にする'
try {
    Set-Service ssh-agent -StartupType Automatic -ErrorAction Stop
    Start-Service ssh-agent -ErrorAction Stop
    Write-Host '有効にしました。この窓を閉じて、Claude の画面に戻ってください。' -ForegroundColor Green
} catch {
    Write-Host "うまくいきませんでした: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'この窓を閉じて、Claude にそのまま伝えてください。'
}
Read-Host 'Enter で閉じます'
