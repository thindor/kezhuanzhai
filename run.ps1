# run.ps1 - 一键启动「可转债持有人系统」(Flask)
# 用法：
#   终端运行  : .\run.ps1
#   双击运行  : 右键本文件 -> 使用 PowerShell 运行
# 说明：启动后访问 http://127.0.0.1:5000/

$py     = 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
$wd     = 'H:\trybuddy\yonghui\cb_holder_system'
$logOut = "$wd\_flask_out.log"
$logErr = "$wd\_flask_err.log"

# 1) 清理 PATH/Path/path 重复键（每条 PowerShell 都是独立会话，必须先清，
#    否则 Start-Process 报「已添加相同键」）
[System.Environment]::SetEnvironmentVariable('PATH', $null)
[System.Environment]::SetEnvironmentVariable('path', $null)
[System.Environment]::SetEnvironmentVariable('Path', 'C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts;C:\Windows\system32;C:\Windows')

# 2) 若 5000 端口已被占用（旧的 Flask 没关），先停掉，避免重复进程
try {
    $old = Get-NetTCPConnection -LocalPort 5000 -ErrorAction Stop | Select-Object -First 1
    if ($old) {
        Stop-Process -Id $old.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        Write-Host "[*] 已停止旧 Flask 进程 PID=$($old.OwningProcess)"
    }
} catch {
    # 端口未占用，正常
}

# 3) 后台启动 Flask（debug=False，避免 reloader 子进程被 tsbx 限制只读）
try {
    Start-Process -FilePath $py -ArgumentList 'app.py' `
        -WorkingDirectory $wd -WindowStyle Hidden `
        -RedirectStandardOutput $logOut -RedirectStandardError $logErr
} catch {
    Write-Host "[!] 启动失败: $_"
    Read-Host "按回车退出"
    exit 1
}

# 4) 等待并验证端口开放
Start-Sleep -Seconds 4
try {
    $c = Get-NetTCPConnection -LocalPort 5000 -ErrorAction Stop | Select-Object -First 1
    Write-Host "[OK] Flask 已启动 PID=$($c.OwningProcess)"
    Write-Host "[OK] 访问地址: http://127.0.0.1:5000/"
} catch {
    Write-Host "[!] 启动后端口未开放，请查看日志: $logErr"
}

Write-Host ""
Read-Host "按回车退出"
