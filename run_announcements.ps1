# 可转债公告 - 每日定时更新包装脚本
# 由 Windows 任务计划程序每日调用，重新拉取并刷新 announcements 表。
$ErrorActionPreference = "Continue"
$venv    = "C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
$script  = "H:\trybuddy\yonghui\cb_holder_system\fetch_announcements.py"
$log     = "H:\trybuddy\yonghui\cb_holder_system\ann_cron.log"
$stamp   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

try {
    $out = & $venv $script --clear 2>&1 | Out-String
    Add-Content -Path $log -Value "$stamp`n$out`n"
} catch {
    Add-Content -Path $log -Value "$stamp ERROR: $_`n"
}
