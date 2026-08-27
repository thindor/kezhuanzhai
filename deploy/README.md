# 裸机部署指南（Linux + Nginx + Gunicorn + systemd）

适用：Ubuntu 22.04 / 20.04 / 24.04 等 Debian 系（其他类 Debian 类似）。
代码仓库：https://github.com/thindor/kezhuanzhai

> 用 **宝塔面板** 部署更省事？见同目录 [`BAOTA.md`](./BAOTA.md)（宝塔自动建 venv / 反代 / SSL / 进程守护 / 计划任务）。

## 0. 约定

- 代码目录：`/opt/kezhuanzhai`
- 虚拟环境：`/opt/kezhuanzhai/venv`
- 服务端口：Gunicorn 监听 `127.0.0.1:8000`，由 Nginx 反代
- 域名：`kzz.bukui.fun`（替换成你自己的）
- 运行用户：`www-data`

## 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx git build-essential
```

> `build-essential` 用于 akshare / pandas 等包编译安装；`certbot` + `python3-certbot-nginx` 用于一键 HTTPS。

## 2. 拉代码 + 建虚拟环境

```bash
sudo mkdir -p /opt/kezhuanzhai
sudo chown -R $USER:$USER /opt/kezhuanzhai
cd /opt/kezhuanzhai
git clone https://github.com/thindor/kezhuanzhai .
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install "akshare==1.18.81"   # ⚠️ 必须单独装且锁版本
```

> **关于 akshare（必读）**：`requirements.txt` 未含 akshare。它是强赎 / 不强赎公告、历史下修兜底的
> 兜底数据源（`crawler.py` 用 `try/except` 包裹 import）。**未安装时程序不会崩，但强赎预警 / 公告 /
> 下修兜底会失效**。务必锁 `1.18.81`（新版接口可能变动）；安装较重，可能耗时 5~10 分钟。
> 验证：`venv/bin/python -c "import akshare; print(akshare.__version__)"` 应输出 `1.18.81`。

## 3. 配置环境变量（密钥 / 密码）

```bash
cp .env.example .env
vim .env   # 填入 CB_SECRET_KEY / ADMIN_PASS，FLASK_DEBUG 保持 0
sudo chmod 600 .env && sudo chown root:root .env
```

> `CB_SECRET_KEY` 用 `python3 -c "import secrets;print(secrets.token_hex(32))"` 生成一段随机串。
> `ADMIN_PASS` 务必改掉默认 `admin888`。`config.py` 直接读取这些环境变量。

## 4. 首次数据播种（核心，别漏）

> `*.db` 被 `.gitignore` 忽略，`git clone` 后库为空。不初始化也能开页面（单只转债按需抓取），
> 但榜单 / 双低 / 强赎预警等聚合页为空。强烈建议跑全量初始化。

```bash
venv/bin/python seed_bonds.py          # 全市场基础资料（断点续跑）
venv/bin/python seed_down_revise.py    # 全市场历史下修（限速 + 断点续跑，较慢）
venv/bin/python crawl_all.py           # 全量十大持有人（最慢，建议后台 nohup 跑，断点续跑）
```

> `crawl_all.py` 每完成一只写入 `_crawl_all_progress.txt`，中断后重跑自动跳过已完成。
> 全量跑完前页面也能用（按需抓取），只是聚合页数据不全。

## 5. 配置 systemd 守护

```bash
sudo cp deploy/kzz.service /etc/systemd/system/kzz.service
sudo systemctl daemon-reload
sudo systemctl enable --now kzz
sudo systemctl status kzz   # 应显示 active (running)
```

> `kzz.service` 以 `www-data` 用户运行，需对项目目录可写（采集 / 访问会写 `cb_holders.db`）：
> `sudo chown -R www-data:www-data /opt/kezhuanzhai`
> 查看日志：`sudo journalctl -u kzz -n 50 --no-pager` 或 `sudo journalctl -u kzz -f`

## 6. 配置 Nginx 反代 + HTTPS

```bash
sudo cp deploy/nginx-kzz.conf /etc/nginx/sites-available/kzz.bukui.fun
sudo ln -s /etc/nginx/sites-available/kzz.bukui.fun /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d kzz.bukui.fun   # 自动申请证书并改写为 HTTPS + 301 跳转
```

> certbot 会自动改写 `nginx-kzz.conf`，加上 443 监听与 80→443 跳转，并配置自动续期。
> 如不想用 certbot，可手动补 `listen 443 ssl` 段并配置证书路径。

## 7. 防火墙（只开 80/443，8000 不对外）

```bash
sudo ufw allow 80,443/tcp
sudo ufw enable
```

> 云厂商安全组同样只放 80 / 443；Gunicorn 仅监听 `127.0.0.1:8000`，反代已搞定对外。

## 8. 定时任务（crontab，数据日更必需）

```bash
sudo crontab -e
```

加入以下四条（**缺任何一条，对应数据都会停滞**）：

```cron
# 公告采集（每日 08:30）
30 8   * * *  cd /opt/kezhuanzhai && /opt/kezhuanzhai/venv/bin/python fetch_announcements.py --clear >> /opt/kezhuanzhai/ann_cron.log 2>&1

# 每日收盘总采集（每日 16:30）：行情 + 基础 + 小盘债 + 等权指数
30 16  * * *  cd /opt/kezhuanzhai && /opt/kezhuanzhai/venv/bin/python collect_daily.py >> /opt/kezhuanzhai/_collect_daily.log 2>&1

# 双低策略每周轮动（每周一 16:40）
40 16  * * 1  cd /opt/kezhuanzhai && /opt/kezhuanzhai/venv/bin/python rotate_double_low.py >> /opt/kezhuanzhai/_double_low.log 2>&1

# 数据库每日备份（每天 04:00，保留 14 天）
0  4   * * *  mkdir -p /opt/kezhuanzhai/backups && cp /opt/kezhuanzhai/cb_holders.db /opt/kezhuanzhai/backups/cb_holders.db.$(date +\%F) && find /opt/kezhuanzhai/backups -name 'cb_holders.db.*' -mtime +14 -delete
```

> `collect_daily.py` 是每日采集总入口，内含行情 (`daily_close`) + 基础数据 (`seed_bonds`) +
> 小盘债 (`mini_bond`)，并自动同步全量列表发现新债、重算等权指数。
>
> **一键安装定时任务（推荐）**：裸机部署直接跑脚本，自动定位目录/venv 并幂等写入（可重复执行）：
> ```bash
> sudo ./deploy/setup_collect_cron.sh
> ```
>
> **采集日志与排错**：`collect_daily.py` 每次运行都会在数据库 `collect_runs` / `collect_steps`
> 表写入结构化日志（每步成败 + 错误原文）。管理员登录后台 →「采集日志」(`/admin/collect-logs`)
> 即可查看每次自动/手动采集的运行列表与逐步明细，**出错时管理员能直接看到报错堆栈**，无需登服务器。
> 原始 stdout 另备份在 `collect_cron.log`。
>
> **交易日守卫**：`collect_daily.py` 内置非交易日跳过（周末 + `config.TRADING_HOLIDAYS`），
> 非交易日只记一条 `skipped` 运行后退出，不会空跑；管理后台「立即采集」按钮用 `--force` 强制运行。
>
> **已退市不再采集**：行情 `fetch_daily_all` 仅取未退市券；`seed_bonds` 对已退市券冻结不回写；
> 其余刷新步骤本身只遍历未退市券。即退市品种数据自动冻结、不再采集。

## 9. 更新代码

```bash
cd /opt/kezhuanzhai
git pull
sudo chown -R www-data:www-data /opt/kezhuanzhai   # root 拉代码后补一次属主
sudo systemctl restart kzz
```

> 改了依赖（新增包）：`venv/bin/pip install ...`，再 `systemctl restart kzz`。

## 10. 常见问题 / 坑

- **502 Bad Gateway**：`kzz` 服务没起来 → `sudo journalctl -u kzz -n 50` 看报错；
  `ss -ltnp | grep 8000` 确认监听。常见原因：`.env` 未建 / 密钥缺失导致启动失败、目录 `www-data` 无权写 `cb_holders.db`、端口与反代不一致。
- **强赎 / 公告页面空或报错**：几乎都是 akshare 没装或版本不对 → `venv/bin/pip show akshare` 确认 `1.18.81`，否则重装。
- **数据不更新**：检查第 8 步 crontab 三条任务是否配齐；确认 `/opt/kezhuanzhai/venv/bin/python` 路径存在。
- **改了代码不生效**：`sudo systemctl restart kzz`（`FLASK_DEBUG=0`，不会热重载）。
- **git pull 后权限拒绝**：root 拉代码后文件属主变 root，`www-data` 无写库权限 → 重新 `chown -R www-data:www-data` 一次。
- **数据库被 gitignore**：`git pull` 只更新代码不更新数据；数据靠第 4 步播种 + 第 8 步定时任务维护。
- **想换目录**：把 `/opt/kezhuanzhai` 全局替换为你的路径（同时改 `kzz.service` 与 `.env` 位置、crontab 路径）。
