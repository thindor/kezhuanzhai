# run_seed.ps1 - 每日全量刷新在交易可转债基础数据（写入 bonds 表）
# 数据源：东方财富 RPT_BOND_CB_LIST（pageNumber 分页，覆盖全市场约 1050 只）
# 行为：拉全市场基础/行情字段（代码、名称、正股、评级、现价、转股价、上市/到期日、退市标记），
#       批量 upsert 进 bonds 表。不抓十大持有人（持有人保持按需爬取）。
# 计划任务：每日 16:35（收盘后）由 KZZ_Seed_Daily 调用。
$py = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$wd = 'H:\trybuddy\yonghui\cb_holder_system'
[System.Environment]::SetEnvironmentVariable('PATH', $null)
[System.Environment]::SetEnvironmentVariable('path', $null)
[System.Environment]::SetEnvironmentVariable('Path', 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Windows\system32;C:\Windows')
Start-Process -FilePath $py -ArgumentList "seed_bonds.py" -WorkingDirectory $wd -WindowStyle Hidden -RedirectStandardOutput "$wd\_seed_out.log" -RedirectStandardError "$wd\_seed_err.log"
Write-Host "已启动全量基础数据刷新（seed_bonds），详见 _seed_out.log"
