# run_daily.ps1 - 每日收盘后采集可转债 + 正股收盘价（写入 daily_close 表）
# 用法：
#   终端/计划任务： .\run_daily.ps1            # 增量采集（最近 10 个交易日）
#   首次补全历史：  .\run_daily.ps1 --history  # 补全最近 320 个交易日
# 说明：数据源新浪财经日K线（直连稳定）；每日 16:30 后运行取当日收盘价。
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$wd = 'H:\trybuddy\yonghui\cb_holder_system'
[System.Environment]::SetEnvironmentVariable('PATH', $null)
[System.Environment]::SetEnvironmentVariable('path', $null)
[System.Environment]::SetEnvironmentVariable('Path', 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Windows\system32;C:\Windows')
$arg = if ($args -contains '--history') { '--history' } else { '' }
Start-Process -FilePath $py -ArgumentList "fetch_daily.py $arg" -WorkingDirectory $wd -WindowStyle Hidden -RedirectStandardOutput "$wd\_daily_out.log" -RedirectStandardError "$wd\_daily_err.log"
Write-Host "已启动每日收盘价采集（$(if($arg){'历史补全'}else{'增量'})），详见 _daily_out.log"
