# 裸机部署指南（Linux + Nginx + Gunicorn + systemd）

适用：Ubuntu 22.04 / 24.04（其他 Debian 系类似）。代码仓库：https://github.com/thindor/kezhuanzhai

> 用 **宝塔面板** 部署更省事？见同目录 [`BAOTA.md`](./BAOTA.md)（宝塔自动建 venv / 反代 / SSL / 进程守护）。

## 0. 约定
- 代码目录：`/opt/kezhuanzhai`
- 虚拟环境：`/opt/kezhuanzhai/venv`
- 服务端口：Gunicorn 监听 `127.0.0.1:8000`，由 Nginx 反代
- 域名：`kzz.bukui.fun`（替换成你自己的）

## 1. 安装系统依赖
```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx git
```

## 2. 拉代码 + 建虚拟环境
```bash
sudo mkdir -p /opt/kezhuanzhai
sudo chown -R $USER:$USER /opt/kezhuanzhai
cd /opt/kezhuanzhai
git clone https://github.com/thindor/kezhuanzhai .
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install gunicorn
```

## 3. 配置环境变量（密钥 / 密码）
```bash
cp .env.example .env
vim .env   # 填入 CB_SECRET_KEY / ADMIN_PASS，FLASK_DEBUG 保持 0
sudo chmod 600 .env && sudo chown root:root .env
```
> `CB_SECRET_KEY` 用 `python3 -c "import secrets;print(secrets.token_hex(32))"` 生成一段随机串。

## 4. 首次数据播种（可选但推荐）
```bash
venv/bin/python seed_bonds.py          # 全市场基础资料
venv/bin/python seed_down_revise.py    # 全市场历史下修（带限速，可断点续跑）
```
不跑也能用：首次访问某转债会自动抓取入库。

## 5. 配置 systemd 守护
```bash
sudo cp deploy/kzz.service /etc/systemd/system/kzz.service
sudo systemctl daemon-reload
sudo systemctl enable --now kzz
sudo systemctl status kzz   # 应显示 active (running)
```

## 6. 配置 Nginx 反代 + HTTPS
```bash
sudo cp deploy/nginx-kzz.conf /etc/nginx/sites-available/kzz.bukui.fun
sudo ln -s /etc/nginx/sites-available/kzz.bukui.fun /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d kzz.bukui.fun   # 自动申请证书并改写为 HTTPS + 301 跳转
```

## 7. 防火墙（只开 80/443，8000 不对外）
```bash
sudo ufw allow 80,443/tcp
sudo ufw enable
```

## 8. 更新代码
```bash
cd /opt/kezhuanzhai
git pull
sudo systemctl restart kzz
```

## 9. 备份 SQLite
```bash
# crontab -e 加一行，每日 4 点备份
0 4 * * * cp /opt/kezhuanzhai/cb_holders.db /opt/kezhuanzhai/backups/cb_holders.db.$(date +\%F)
```

## 常见问题
- **502 Bad Gateway**：`kzz` 服务没起来 → `sudo journalctl -u kzz -n 50` 看报错。
- **改了代码不生效**：`sudo systemctl restart kzz`。
- **页面能开但接口 404**：检查 nginx `server_name` 与域名解析是否一致。
- **想换目录**：把 `/opt/kezhuanzhai` 全局替换为你的路径（同时改 `kzz.service` 与 `.env` 位置）。
