# 宝塔面板部署指南（Linux + 宝塔 + Python 项目 + Nginx 反代）

适用：已安装宝塔面板（bt.cn）的 Linux 服务器（Ubuntu / CentOS / Debian 均可）。
代码仓库：https://github.com/thindor/kezhuanzhai
前置：已在域名服务商处把 `kzz.bukui.fun` 解析到本服务器 IP。

> 说明：宝塔的「Python 项目」模块会自动帮你建虚拟环境、配置 Nginx 反代、管理进程，
> 比裸机方案省事。下面按最新版宝塔（9.x）界面描述，老版本（Python 项目管理器）字段名略不同但逻辑一致。

---

## 1. 安装运行环境
进入 **宝塔面板 → 软件商店**，安装 / 确认已安装：
- **Nginx**（Web 服务与反代，必装）
- **Python 项目** 模块（左侧「网站」区或「软件商店 → 运行环境」里，按需安装；它可在线装多个 Python 版本）

在「Python 项目 → 版本管理」里装一个 **Python 3.11 或 3.12**（推荐 3.11）。

---

## 2. 拉取代码
打开 **宝塔面板 → 终端**，执行：
```bash
cd /www/wwwroot
git clone https://github.com/thindor/kezhuanzhai kezhuanzhai
```
代码会落到 `/www/wwwroot/kezhuanzhai`。

> 若服务器没装 git：`apt install -y git`（Ubuntu/Debian）或 `yum install -y git`（CentOS）。

---

## 3. 添加站点（域名 + Nginx）
**宝塔 → 网站 → 添加站点**：
- 域名：`kzz.bukui.fun`
- 根目录：选 `/www/wwwroot/kezhuanzhai`
- FTP：不创建
- 数据库：不创建（本项目用 SQLite，无需 MySQL）
- PHP 版本：纯静态 / 无（我们是 Python）

提交后，宝塔会生成一份 Nginx 配置（后面会被 Python 项目的反代覆盖，无妨）。

---

## 4. 创建 Python 项目（核心）
**宝塔 → Python 项目 → 添加项目**（老版本在「Python 项目管理器」）：
- 项目名称：`kzz`
- 项目路径：`/www/wwwroot/kezhuanzhai`
- Python 版本：选上一步装的 3.11 / 3.12
- 框架：选 **Flask**
- 启动方式：**gunicorn**
- 启动对象 / 启动文件：`app:app`（Flask 应用对象名是 `app`）
- 端口：`8000`
- 启动命令（自定义）：`gunicorn -w 4 -b 0.0.0.0:8000 app:app`
- 勾选「**安装依赖**」（宝塔会用项目里的 `requirements.txt` 自动建 venv 并 `pip install`；现已含 gunicorn）

### 设置环境变量（重要，生产安全必填）
同一界面的「环境变量」或「高级设置」里添加：
```
CB_SECRET_KEY   = 一段随机长字符串（生成：python3 -c "import secrets;print(secrets.token_hex(32))"）
ADMIN_PASS      = 你的强后台密码（覆盖默认 admin888）
FLASK_DEBUG     = 0
```
> - `CB_SECRET_KEY`：Flask 会话签名密钥，必须设，否则用代码里的占位弱密钥。
> - `ADMIN_PASS`：后台 `/admin` 登录密码。**不设置则仍是默认 `admin888`**，公网极危险，务必改。
> - `FLASK_DEBUG=0`：关闭调试模式与自动重载，生产必关。
> - 这些变量会被写入进程的 `os.environ`，`config.py`/`app.py` 直接读取，无需 `.env` 文件。

点击「提交」，宝塔会：建 venv → 装依赖 → 启动 gunicorn → 自动加一条反向代理。

---

## 5. 确认反向代理
**宝塔 → 网站 → kzz.bukui.fun → 反向代理**，应已有一条：
- 名称：随意（如 `kzz_app`）
- 目标 URL：`http://127.0.0.1:8000`
- 发送域名：`$host`

把外网请求 80/443 转发到本地 8000。若没有，手动「添加反代」填上面三项即可。

> 此时用 `http://kzz.bukui.fun` 已能访问；下一步上 HTTPS。

---

## 6. 配置 HTTPS（Let's Encrypt 一键）
**宝塔 → 网站 → kzz.bukui.fun → SSL → Let's Encrypt**：
- 勾选域名 `kzz.bukui.fun`
- 勾选「**强制 HTTPS**」
- 点击「申请」

宝塔会自动申请证书、配置 443、设置 80→443 跳转，并自动续期。

---

## 7. 防火墙（只开 80/443）
**宝塔 → 安全**：
- 放行 `80`、`443`（TCP）
- **不要**对外放行 `8000`（gunicorn 只监听本地，反代已搞定对外）

云厂商控制台（阿里云/腾讯云安全组）同样只放 80/443。

---

## 8. 首次数据播种（可选但推荐）
打开 **宝塔 → 终端**，进入项目 venv 灌数据：
```bash
cd /www/wwwroot/kezhuanzhai
source venv/bin/activate          # 或宝塔 Python 项目里的「终端」按钮
python seed_bonds.py              # 全市场基础资料
python seed_down_revise.py        # 全市场历史下修（带限速，可断点续跑）
deactivate
```
不跑也能用：首次访问某转债会自动抓取并入库。

---

## 9. 日常运维
- **更新代码**：终端 `git pull`，然后在 **Python 项目** 里点「重启」该项目。
- **查看日志 / 排错**：Python 项目 → 该项目 → 「日志」或「终端」；502 多为 gunicorn 没起，看日志报错。
- **重启服务**：Python 项目列表里该项目「重启」按钮即可，无需手动 systemctl。
- **备份 SQLite**：**宝塔 → 计划任务 → 添加任务**，类型「Shell 脚本」，每日执行：
  ```bash
  cp /www/wwwroot/kezhuanzhai/cb_holders.db /www/backup/cb_holders.db.$(date +\%F)
  ```
  （或直接在文件管理器打包 `cb_holders.db`）

---

## 常见问题
- **502 Bad Gateway**：gunicorn 未启动或端口不对 → Python 项目日志看报错；确认启动命令 `app:app` 与端口 `8000` 与反代一致。
- **改了代码不生效**：Python 项目点「重启」；若改了依赖，重新「安装依赖」。
- **后台还是 admin888**：确认「环境变量」里 `ADMIN_PASS` 已设且项目已重启（`config.py` 现在读该变量）。
- **页面能开但接口 404**：检查站点域名解析与「反向代理」目标是否为 `127.0.0.1:8000`。
- **Let's Encrypt 申请失败**：确认域名已正确解析到本机 IP，且 80 端口可通。

---

## 与裸机方案差异
- 进程守护、Nginx、日志、SSL、备份都在宝塔 UI 里点选完成，不用手敲 systemd / certbot。
- 环境变量在宝塔「Python 项目 → 环境变量」填写（等价于裸机方案的 `.env`），无需 `.env` 文件。
- 其余安全、数据采集、备份逻辑完全一致。
