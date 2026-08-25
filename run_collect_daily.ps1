# 每日采集总入口启动脚本（供 Windows 计划任务 / WorkBuddy 自动化调用）
# 清理原生环境块里的 PATH 大小写重复键，避免 Start-Process 撞键
[System.Environment]::SetEnvironmentVariable("PATH", $null, [System.EnvironmentVariableTarget]::Process)
[System.Environment]::SetEnvironmentVariable("Path", $null, [System.EnvironmentVariableTarget]::Process)
[System.Environment]::SetEnvironmentVariable("path", $null, [System.EnvironmentVariableTarget]::Process)
$env:PATH = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Windows\system32;C:\Windows'

$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$wd = 'H:\trybuddy\yonghui\cb_holder_system'
& $py -u (Join-Path $wd 'collect_daily.py')
