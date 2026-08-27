# 可转债持有人系统 · 宝塔面板部署文档

> 适用环境：**Ubuntu 20.04 + 宝塔面板 11.6.0**（其他 11.x 版本界面基本一致，字段名可能微调）
> 代码仓库：`https://github.com/thindor/kezhuanzhai`（分支 `master`）
> 部署域名：`kzz.bukui.fun`（请替换为你自己的域名，并已解析到本服务器 IP）
> 最后更新：2026-08-25

---

## 0. 部署架构与关键约定

分工原则：**宝塔负责它最省事的部分**（Nginx 反代、HTTPS、计划任务、数据库备份、文件管理）；**Gunicorn 常驻进程用 systemd 守护**（路径透明、好排错）。这样所有命令路径都是确定的，不会踩宝塔 Python 项目模块 venv 路径不透明的坑。

| 项 | 值 |
|---|---|
| 代码目录 | `/www/wwwroot/kezhuanzhai` |
| 虚拟环境 | `/www/wwwroot/kezhuanzhai/venv`（手动建，路径固定） |
| 运行用户 | `www-data`（需对项目目录有读写权限，采集会写库） |
| 应用端口 | Gunicorn 监听 `127.0.0.1:8000`（仅本机） |
| 对外端口 | 只开 `80` / `443`，由 Nginx 反代到 8000 |
| Python 版本 | 推荐 3.11（宝塔「版本管理」在线装）；Ubuntu 20.04 自带 3.8 也兼容 |
| 数据库 | SQLite 单文件 `cb_holders.db`（被 `.gitignore`，clone 后需初始化） |

> ⚠️ **最重要的一条**：`*.db` 已被 `.gitignore` 忽略，`git pull` 只更新代码、**不更新数据**。数据靠「首次初始化」+「定时任务」维护。

---

## 1. 系统环境与宝塔准备

### 1.1 系统包（SSH 登录服务器执行）
```bash
sudo apt update
sudo apt install -y git python3-venv build-essential
```
- `build-essential`：akshare/pandas 等包编译安装时需要。
- `python3-venv`：用于建虚拟环境（也可改用宝塔装的 3.11 建）。

### 1.2 宝塔已装组件确认
宝塔面板 → **软件商店**，确认已安装：
- **Nginx**（Web 服务与反代，必装）
- （可选但建议）**Python 项目** 模块 → 「版本管理」里装一个 **Python 3.11**，便于排错和后续升级。

### 1.3 防火墙 / 安全组
- **云厂商安全组**：放行 `22`（SSH）、`80`、`443`；**不要**放行 `8000`。
- **宝塔 → 安全**：同样只放行 `80`、`443`、`22`。

---

## 2. 拉取代码

```bash
cd /www/wwwroot
git clone https://github.com/thindor/kezhuanzhai kezhuanzhai
cd kezhuanzhai
git log --oneline -3      # 确认拉到最新 master
```

> 服务器没装 git？`sudo apt install -y git`。

---

## 3. Python 运行环境（重点：akshare）

### 3.1 建虚拟环境
```bash
cd /www/wwwroot/kezhuanzhai
python3 -m venv venv
source venv/bin/activate
python -V                 # 确认版本（3.8 / 3.11 均可）
```
> 若想用宝塔的 3.11 解释器建 venv，先用宝塔「Python 项目 → 版本管理」装好 3.11，再用其绝对路径建：
> `/www/server/python_manager/<版本目录>/bin/python3.11 -m venv venv`
> （路径在宝塔版本管理界面可查；本项目对 3.8/3.11 均兼容，实测 3.8 可跑。）

### 3.2 安装依赖
```bash
pip install --upgrade pip
pip install -r requirements.txt
pip install "akshare==1.18.81"     # ⚠️ 必须单独装且锁版本！
```

**关于 akshare（必读）**：
- `requirements.txt` 里**只有** `flask / requests / beautifulsoup4 / pypinyin / gunicorn`，**没有 akshare**。
- akshare 是「强赎/不强赎公告」和「历史下修兜底」的**兜底数据源**（`crawler.py` 用 `try/except` 包裹 import）。**未安装时程序不会崩，但强赎预警、公告模块、下修兜底会失效**——这是生产环境不能接受的。
- 务必锁 `1.18.81`：新版 akshare 接口可能变动，导致采集失败。
- akshare 依赖较重（pandas / lxml / html5lib 等），安装可能耗时 **5~10 分钟**，耐心等。

验证安装：
```bash
python -c "import akshare; print(akshare.__version__)"   # 应输出 1.18.81
```

### 3.3 配置环境变量（密钥 / 密码）
```bash
cp .env.example .env
chmod 600 .env
```
编辑 `.env`（用宝塔「文件」管理器或 `vim .env`）：
```ini
# Flask 会话签名密钥（生产必填），生成方式：
#   python3 -c "import secrets;print(secrets.token_hex(32))"
CB_SECRET_KEY=<粘贴一段随机长字符串>

# 管理后台密码（务必改掉默认 admin888）
ADMIN_PASS=<你的强密码>

# 生产必须为 0
FLASK_DEBUG=0

# Gunicorn 监听端口（需与 systemd / nginx 一致）
PORT=8000
```
> `config.py` 读取 `CB_SECRET_KEY` / `ADMIN_PASS`。不设则分别是弱占位密钥和默认密码 `admin888`，**公网极危险**。

---

## 4. 启动 Gunicorn（systemd 守护）

### 4.1 准备 service 文件
项目已带 `deploy/kzz.service`（通用路径 `/opt/kezhuanzhai`），改成宝塔目录并启用：
```bash
sudo cp /www/wwwroot/kezhuanzhai/deploy/kzz.service /etc/systemd/system/kzz.service
sudo sed -i 's#/opt/kezhuanzhai#/www/wwwroot/kezhuanzhai#g' /etc/systemd/system/kzz.service
cat /etc/systemd/system/kzz.service      # 确认路径已替换
```

把项目目录属主交给运行用户 `www-data`（采集/访问要写 `cb_holders.db`）：
```bash
sudo chown -R www-data:www-data /www/wwwroot/kezhuanzhai
```

### 4.2 启动并设置开机自启
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kzz
sudo systemctl status kzz          # 应显示 active (running)
```
查看日志：
```bash
sudo journalctl -u kzz -n 50 --no-pager     # 最近 50 行
sudo journalctl -u kzz -f                    # 实时跟踪
```

### 4.3 本机验证
```bash
curl -s http://127.0.0.1:8000/ | head -c 200
```
能看到 HTML 即 Gunicorn 正常。

---

## 5. 宝塔：添加站点 + 反向代理 + HTTPS

### 5.1 添加站点
宝塔 → **网站** → **添加站点**：
- 域名：`kzz.bukui.fun`
- 根目录：`/www/wwwroot/kezhuanzhai`
- FTP：不创建
- 数据库：不创建（SQLite，无需 MySQL）
- PHP 版本：纯静态 / 无

### 5.2 反向代理
宝塔 → **网站** → `kzz.bukui.fun` → **反向代理** → **添加反代**：
- 代理名称：`kzz_app`（随意）
- 目标 URL：`http://127.0.0.1:8000`
- 发送域名：`$host`
- 提交。

> 此时用 `http://kzz.bukui.fun` 已能打开（Gunicorn 在 8000 跑，Nginx 反代）。
> 本项目静态文件（如 `/static/style.css`）由 Flask 经 Gunicorn 返回，反代 `/` 已覆盖，无需额外配置静态目录。

### 5.3 HTTPS（Let's Encrypt 一键）
宝塔 → **网站** → `kzz.bukui.fun` → **SSL** → **Let's Encrypt**：
- 勾选域名 `kzz.bukui.fun`
- 勾选「**强制 HTTPS**」
- 点击「申请」

宝塔会自动申请证书、配置 443、设置 80→443 跳转，并自动续期。

---

## 6. 首次数据初始化（核心，别漏）

> `*.db` 未被 git 跟踪，clone 后库是空的。**不初始化也能打开页面**，但市场榜 / 自然人榜 / 双低 / 强赎预警 / 公告等都是空的，只有首次访问单只转债才会按需抓取。强烈建议跑全量初始化。

进入 venv：
```bash
cd /www/wwwroot/kezhuanzhai
source venv/bin/activate
```

### 6.1 基础数据（全市场转债条款 / 现价，约几分钟）
```bash
python seed_bonds.py
```

### 6.2 历史下修（较慢，可后台，支持断点续跑）
```bash
nohup python seed_down_revise.py > seed_dr.log 2>&1 &
tail -f seed_dr.log
```

### 6.3 全量十大持有人（最慢，可能数小时；后台跑，断点续跑）
```bash
nohup python crawl_all.py --sleep 2 > _crawl_all.log 2>&1 &
tail -f _crawl_all.log
```
- `crawl_all.py` 每完成一只写进度到 `_crawl_all_progress.txt`，中断后重跑自动跳过已完成。
- 数据库写入已加 `busy_timeout` 防锁，采集与访问可并行。
- **全量跑完前页面也能用**（按需抓取），只是榜单类页面数据不全。

---

## 7. 定时任务（宝塔计划任务 → Shell 脚本）

宝塔 → **计划任务** → **添加任务**，类型选「**Shell 脚本**」。以下四条**都必须配**，否则数据不更新。

### 7.1 公告采集（每日 08:30）
- 任务名称：`kzz_announcements`
- 执行周期：每天 `08:30`
- 脚本内容：
```bash
cd /www/wwwroot/kezhuanzhai && ./venv/bin/python fetch_announcements.py --clear >> ann_cron.log 2>&1
```

### 7.2 每日收盘总采集（每日 16:30）
- 任务名称：`kzz_daily_close`
- 执行周期：每天 `16:30`
- 脚本内容：
```bash
cd /www/wwwroot/kezhuanzhai && ./venv/bin/python collect_daily.py >> _collect_daily.log 2>&1
```
> `collect_daily.py` 是每日采集总入口，内含：行情(`daily_close`) + 基础数据(`seed_bonds`) + 小盘债(`mini_bond`)，并自动同步全量列表发现新债、重算等权指数。等价于原每日收盘编排（强赎/下修时效性强，公告由 7.1 单独跑）。
>
> **采集日志与排错**：`collect_daily.py` 每次运行都会把每步成败 + 错误原文写入数据库 `collect_runs` / `collect_steps` 表。管理员登录后台 → 点「采集日志」(`/admin/collect-logs`) 即可查看每次自动/手动采集的运行列表与逐步明细，**出错能直接看到报错堆栈**，无需登服务器。原始 stdout 另备份在 `_collect_daily.log`。
>
> **交易日守卫**：脚本内置非交易日跳过（周末 + `config.TRADING_HOLIDAYS`），非交易日只记一条 `skipped` 运行后退出、不会空跑。宝塔计划任务建议周期设「每周 1–5 16:35」（收盘后），与内置守卫双保险。
>
> **已退市不再采集**：行情 `fetch_daily_all` 仅取未退市券；`seed_bonds` 对已退市券冻结不回写；其余刷新步骤只遍历未退市券。退市品种数据自动冻结。

### 7.3 双低策略每周轮动（每周一 16:40）
- 任务名称：`kzz_double_low`
- 执行周期：每周 `一` `16:40`
- 脚本内容：
```bash
cd /www/wwwroot/kezhuanzhai && ./venv/bin/python rotate_double_low.py >> _double_low.log 2>&1
```

### 7.4 数据库每日备份（每天 04:00）
- 任务名称：`kzz_db_backup`
- 执行周期：每天 `04:00`
- 脚本内容：
```bash
mkdir -p /www/wwwroot/kezhuanzhai/backups
cp /www/wwwroot/kezhuanzhai/cb_holders.db /www/wwwroot/kezhuanzhai/backups/cb_holders.db.$(date +\%F)
find /www/wwwroot/kezhuanzhai/backups -name 'cb_holders.db.*' -mtime +14 -delete
```

> **venv 路径确认**：脚本里用 `./venv/bin/python`，前提是 venv 在 `/www/wwwroot/kezhuanzhai/venv`（第 3.1 步建的位置）。若你改用宝塔「Python 项目」模块建的 venv，路径不同，需替换为宝塔实际的 python（在宝塔「终端」里 `which python` 查看绝对路径）。

---

## 8. 升级与日常运维

**更新代码**：
```bash
cd /www/wwwroot/kezhuanzhai
git pull
sudo chown -R www-data:www-data /www/wwwroot/kezhuanzhai   # 若用 root 拉，补一次属主
sudo systemctl restart kzz
```
> 改了依赖（新增包）：`source venv/bin/activate && pip install ...`，再 `systemctl restart kzz`。

**查看日志**：
- 应用：`sudo journalctl -u kzz -f`
- 采集：各 `.log` 文件（`ann_cron.log`、`_collect_daily.log`、`_crawl_all.log`、`_double_low.log`）

**重启 Gunicorn**：`sudo systemctl restart kzz`

**备份恢复**：把备份的 `cb_holders.db` 覆盖回 `/www/wwwroot/kezhuanzhai/cb_holders.db` 即可（覆盖前先停服务或确保无写入）。

---

## 9. 常见问题 / 坑

- **502 Bad Gateway**：Gunicorn 没起 → `sudo systemctl status kzz` 看状态，`journalctl -u kzz` 看报错；确认 8000 在监听 `ss -ltnp | grep 8000`。常见原因：`.env` 未建/密钥缺失导致启动失败、项目目录 `www-data` 无权写 `cb_holders.db`、端口与反代不一致。
- **强赎 / 公告页面空或报错**：几乎都是 akshare 没装或版本不对 → 进 venv `pip show akshare` 确认 `1.18.81`，否则 `pip install "akshare==1.18.81"`。
- **定时任务没跑 / 数据不更新**：看宝塔计划任务日志；确认 `ls /www/wwwroot/kezhuanzhai/venv/bin/python` 存在。若 venv 路径不同，替换脚本里的 python 绝对路径。
- **改了代码不生效**：`sudo systemctl restart kzz`（`FLASK_DEBUG=0`，不会热重载）。
- **后台还是 `admin888`**：确认 `.env` 里 `ADMIN_PASS` 已填且服务已重启。
- **git pull 后页面报权限拒绝**：root 拉代码后文件属主变 root，`www-data` 无写库权限 → 重新 `chown -R www-data:www-data` 一次。
- **Ubuntu 20.04 自带 Python 3.8 够用吗**：够用（兼容 3.8/3.11）。若个别包要求更高，用宝塔装 3.11 重建 venv 即可。
- **数据库被 .gitignore**：`git pull` 只更新代码不更新数据；数据靠第 6 步初始化 + 第 7 步定时任务维护。
- **Let's Encrypt 申请失败**：确认域名已正确解析到本机 IP，且 80 端口可通（云安全组 + 宝塔安全都已放行 80）。

---

## 附录 A：全程用宝塔「Python 项目」模块（不写 systemd）

若更希望常驻进程也由宝塔管理：
1. 宝塔 → **网站** → **Python 项目** → **添加项目**：框架 `Flask`，启动方式 `gunicorn`，启动对象 `app:app`，端口 `8000`，启动命令 `gunicorn -w 4 -b 0.0.0.0:8000 app:app`，勾选「安装依赖」。
2. 在「环境变量」填 `CB_SECRET_KEY` / `ADMIN_PASS` / `FLASK_DEBUG=0`。
3. 宝塔会自动建 venv + 反代。**但宝塔自动建的 venv 不在项目目录**，需用项目详情里的「终端」按钮进入，手动 `pip install akshare==1.18.81`。
4. 第 7 步定时任务里的 `./venv/bin/python` 要换成宝塔实际的 venv python（「终端」里 `which python` 查看绝对路径并替换）。
5. 其余 Nginx / SSL / 数据初始化 / 备份步骤同正文。

## 附录 B：目录与文件速查

- 应用入口：`app.py`（Flask 对象 `app`）
- 部署配置：`deploy/kzz.service`（systemd）、`deploy/nginx-kzz.conf`（裸机 Nginx）、`deploy/README.md`（裸机部署版）
- 数据库：`cb_holders.db`（SQLite，gitignore）
- 环境变量：`.env`（gitignore，模板 `.env.example`）
- 采集脚本：`seed_bonds.py` / `seed_down_revise.py` / `crawl_all.py` / `collect_daily.py` / `fetch_announcements.py` / `fetch_daily.py` / `rotate_double_low.py`
