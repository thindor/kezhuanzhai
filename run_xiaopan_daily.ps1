# run_xiaopan_daily.ps1 - 每日收盘后自动刷新小盘债（金陵式迷你弹性转债）数据
# 数据源：腾讯实时行情(checkup.get_realtime) + 东财赎回价 + 新浪长周期日K(历史最高)
# 行为：重算候选小盘债的 current_price / redemption_price / mini_hist_max，写回 bonds 表。
# 计划任务：每日 16:38（晚于 KZZ_Seed_Daily 16:35）由 KZZ_Xiaopan_Daily 调用。
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$wd = 'H:\trybuddy\yonghui\cb_holder_system'
[System.Environment]::SetEnvironmentVariable('PATH', $null)
[System.Environment]::SetEnvironmentVariable('path', $null)
[System.Environment]::SetEnvironmentVariable('Path', 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Windows\system32;C:\Windows')
Start-Process -FilePath $py -ArgumentList "run_xiaopan_daily.py" -WorkingDirectory $wd -WindowStyle Hidden -RedirectStandardOutput "$wd\_xiaopan_out.log" -RedirectStandardError "$wd\_xiaopan_err.log"
Write-Host "已启动小盘债每日刷新（mini_bond.refresh_all），详见 _xiaopan_out.log"
