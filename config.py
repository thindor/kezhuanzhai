import os

# 项目根目录（用于定位数据库文件，保证可移植）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cb_holders.db")

# 管理后台账号（按需求固定）
ADMIN_USER = "admin"
ADMIN_PASS = "admin888"

# Flask 会话密钥：生产环境请通过环境变量 CB_SECRET_KEY 覆盖
SECRET_KEY = os.environ.get("CB_SECRET_KEY", "cb_holders_local_secret_2026_change_me")

# 东方财富数据中心接口（可转债十大持有人 / 基础资料的权威免费源）
EM_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

# 同花顺 F10（best-effort：用于校正"持有人标识"性质，仅最新一期）
THS_BASE = "https://basic.10jqka.com.cn/{code}/detail.html"

# ---------------- 缓存策略 ----------------
# 核心原则：用户「查看」已采集的转债时，直接返回本地 SQLite 缓存，绝不重新抓取；
#           只有「主动更新」(refresh=1 / 管理后台) 或「未采集过」才去抓取。
# CACHE_TTL_DAYS：已采集数据距上次更新的天数超过该值，标记为「较旧，建议更新」，
#                 但默认仍直接返回缓存（不自动抓取）。设为 0 表示永不过期提示。
CACHE_TTL_DAYS = 90
