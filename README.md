# 可转债持有人 & 历史下修查询系统（kezhuanzhai）

一个面向可转债（可转换公司债券）投资者的轻量级数据工具：输入转债代码，即可查看该转债的
**历次报告期十大持有人 / 基金持仓**、**历史下修记录**，以及强赎预警、下修提醒、每日收盘、
公告、双低策略、转债体检卡、小盘债等衍生分析与榜单。数据来自公开披露平台的聚合，首次查询
自动抓取入库，之后直接读库、不再重复爬取。

> 在线示例（如有）：https://kzz.bukui.fun

## 功能特性

### 基础查询
- **十大持有人 / 基金持仓**：按报告期展示历次十大持有人，标注性质（基金 / 一般机构 / 个人）、
  持有数量（万张）与占比；含自然人持仓市值估算。
- **历史下修记录**：下修次数、最近一次下修的股东大会日与生效日；独立 SEO 专题页 `/xiuxie/<code>`，
  含结构化数据（Breadcrumb / FAQ JSON-LD）、canonical、OG 标签。
- **首页搜索**：支持代码 / 名称 / 拼音首字母检索全市场转债；命中后直接跳转详情页，并显示
  「转债详情」「下修记录」快捷链接；首页展示「最近检索的可转债」快捷入口。
- **全部转债列表 `/bonds`**：汇总所有已采集可转债；支持按代码 / 名称 / 正股模糊检索，按退市状态、
  历史下修筛选，并可按代码、下修次数、最近更新、持有人数、到期日排序，分页展示。

### 榜单与持有人视图
- **自然人榜（牛散榜）/ 机构榜**：首页并列展示 Top 10，完整榜见 `/persons`、`/institutions`
  （按持仓市值排序；**已退市转债不计入**，保证榜单口径）。
- **统一持有人视图 `/holder/<name>`**：转债详情页中的持有人名称可点击，跳转统一视图
  （按数据自动区分「自然人持有人」/「机构持有人」并注入结构化数据）。

### 预警与提醒
- **强赎预警**：转股期内正股连续满足强赎触发价（≥ 转股价 130%）的天数统计；列表页「强赎预警」列、
  详情页 `.warn-banner` 横幅、`/redemption_warnings` 独立页面（urgent / high / normal 三级）。
- **下修提醒**：滚动窗口内正股收盘 ≤ 转股价 × 85% 的天数统计；`/down_revise_warnings` 独立页面
  （approaching / triggered 双状态，蓝系配色）+ 详情页 `.revise-banner` 横幅。已公告强赎的转债
  自动跳过下修计算。
- **每日收盘 & 等权指数**：收盘后写入 `daily_close`（转债 + 正股），详情页双 Y 轴走势图；
  全站「数据截至 X」新鲜度条（取自 `daily_close` 最新交易日）。

### 公告与策略
- **公告模块 `/announcements`**：保留真实事件——即将发行 / 强赎 / 不强赎 / 下修，按 `signal`
  标记（buy 下修 / sell 强赎 / neutral 即将发行）驱动页面「信号」列；每日 08:30 定时采集。
- **双低策略 `/double_low`**：当前前 20 双低转债（双低值 = 转债价 + 转股溢价率）+ 本周轮动
  进入 / 调出记录 + 历史轮动收益（按每周一真实收盘价计算）；每周一 16:40 轮动快照。
- **转债体检卡 `/bond/<code>/checkup`**：债性 / 股性 / 条款三维（纯债价值、YTM、溢价率、
  弹性、到期赎回价等）+ 「🔄 刷新实时行情」按钮（腾讯 `qt.gtimg.cn` 实时价）；退市债不显示刷新。
- **小盘债 `/xiaopanzhai`**：按规模 / 流动性筛选的小盘转债视图，每日收盘后刷新候选。

### 数据治理与后台
- **退市标记 & 数据冻结**：已退市转债自动标记；一旦标记，**已采集历史数据不再支持更新**
  （详情页「更新数据」隐藏，后台批量 / 单只更新跳过，避免对终止上市转债反复抓取）。
- **管理后台 `/admin`**：单只 / 批量更新、删除转债及其持有人数据（账号见配置）。后台开关
  「采集数据时忽略已退市可转债」（默认勾选）。
- **搜索引擎友好**：详情页 / 下修页 / 公告页 / 双低页等做基础 SEO 优化，含 `sitemap.xml`、
  `robots.txt`、Open Graph。

## 技术栈

- 后端：Python 3.8+（推荐 3.11）/ Flask 3.x
- 存储：SQLite（单文件 `cb_holders.db`，运行时生成）
- 数据源：
  - 东方财富数据中心（十大持有人、基础资料、可转债列表）
  - 集思录 `adj_logs`（历史下修）
  - 腾讯 `qt.gtimg.cn`（实时 / 行情价）
  - akshare（强赎 / 不强赎公告、历史下修兜底的兜底源，**必须安装 1.18.81**）
- 前端：Jinja2 模板 + 原生 CSS（无构建步骤）

## 目录结构

```
cb_holder_system/
├── app.py                # Flask 应用（路由 / 鉴权 / SEO / 自动索引）
├── crawler.py            # 爬虫：十大持有人 + 历史下修 + 强赎/下修兜底(akshare)
├── db.py                 # SQLite 存储层（holders / bonds / announcements / daily_close / double_low_log ...）
├── config.py             # 配置（账号 / 接口 / 缓存策略）
├── checkup.py            # 转债体检卡（债性/股性/条款三维 + 实时行情）
├── mini_bond.py          # 小盘债候选筛选与每日刷新
├── seed_bonds.py         # 播种：全市场转债基础资料入 bonds 表
├── seed_down_revise.py   # 播种：批量采集全市场历史下修记录
├── crawl_all.py          # 全量十大持有人抓取（限速 / 断点续跑 / 跳过冻结）
├── collect_daily.py      # 每日采集总入口（行情 + 基础 + 小盘债 + 等权指数 + 第⑨步自检）
├── verify_integrity.py   # 数据质量自检（双低快照无未上市债 / 等权指数 chg% 自洽），collect_daily 第⑨步调用
├── fetch_daily.py        # 每日收盘价采集（collect_daily 内部调用）
├── fetch_announcements.py# 每日公告采集（强赎/不强赎/下修/即将发行）
├── rotate_double_low.py  # 双低策略每周轮动
├── requirements.txt      # ⚠️ 未含 akshare，部署时需额外安装
├── .env.example          # 环境变量模板（CB_SECRET_KEY / ADMIN_PASS / FLASK_DEBUG / PORT）
├── run.ps1 / start_flask.bat  # Windows 一键启动
├── templates/            # 页面模板（index / bond / checkup / announcements / double_low / ...）
├── static/style.css      # 样式
├── deploy/               # 部署配置
│   ├── BAOTA.md          # 宝塔面板部署（Ubuntu 20.04 + 宝塔 11.6）
│   ├── README.md         # 裸机部署（Nginx + Gunicorn + systemd）
│   ├── kzz.service       # systemd 服务文件
│   └── nginx-kzz.conf    # Nginx 反代配置样例
└── README.md
```

## 安装与运行（开发模式）

```bash
cd cb_holder_system
pip install -r requirements.txt
pip install "akshare==1.18.81"   # 强赎/公告/下修兜底依赖，requirements 未含
python app.py
```

打开 http://127.0.0.1:5000 （开发默认端口 5000）。

- **首页**：输入转债代码（如 `127061`）或名称 / 拼音首字母查询；未收录则自动爬取入库。
- **管理后台**：http://127.0.0.1:5000/admin ，默认账号 **admin / admin888**（生产请改）。

### Windows 一键启动

双击 `run.ps1` 或 `start_flask.bat` 即可（内置 venv 一键启动，适合本机部署）。

## 数据播种（首次部署必做）

`*.db` 被 `.gitignore` 忽略，`git clone` 后库为空。建议先跑播种，把全市场数据预拉入库：

```bash
python seed_bonds.py          # 全市场基础资料（断点续跑）
python seed_down_revise.py    # 全市场历史下修（限速 + 断点续跑，较慢）
python crawl_all.py           # 全量十大持有人（最慢，建议后台 nohup 跑，断点续跑）
```

> `crawl_all.py` 每完成一只写入 `_crawl_all_progress.txt`，中断后重跑自动跳过已完成。
> 不初始化也能开页面（按访问单只转债自动抓取），但榜单 / 双低 / 强赎预警等聚合页为空。

## 部署

本项目提供两种生产部署方式，核心都是 **Flask + Gunicorn + SQLite**，区别在进程守护与 Web 层管理：

- **宝塔面板部署（推荐运维省事）**：见 [`deploy/BAOTA.md`](deploy/BAOTA.md)
  —— 针对 Ubuntu 20.04 + 宝塔 11.6.0，宝塔管 Nginx 反代 / HTTPS / 计划任务 / 备份，Gunicorn 用 systemd 守护。
- **裸机部署（Nginx + Gunicorn + systemd）**：见 [`deploy/README.md`](deploy/README.md)
  —— 适用于任意 Debian / Ubuntu 系，不依赖宝塔。

### 部署通用注意事项（两种方式都适用）

1. **必须额外安装 akshare 1.18.81**：`requirements.txt` 未含 akshare。强赎 / 不强赎公告、历史下修兜底
   依赖它。装完 `requirements.txt` 后务必再 `pip install "akshare==1.18.81"`。未装时程序不崩，
   但强赎预警 / 公告 / 下修兜底会失效。
2. **数据库不随代码更新**：`*.db` 被 `.gitignore` 忽略，`git clone` / `git pull` 只更新代码、
   **不更新数据**。首次部署须执行上述「数据播种」，之后靠定时任务维护。
3. **定时任务（数据日更必需）**，否则页面数据停滞：
   - 每日 08:30：`python fetch_announcements.py --clear`（公告）
   - 每日 16:30：`python collect_daily.py`（收盘总采集：行情 + 基础 + 小盘债 + 等权指数）
   - 每周一 16:40：`python rotate_double_low.py`（双低周轮动）
4. **环境变量必设（生产）**：`CB_SECRET_KEY`（随机串，生成：`python3 -c "import secrets;print(secrets.token_hex(32))"`）、
   `ADMIN_PASS`（改掉默认 `admin888`）、`FLASK_DEBUG=0`。模板见 `.env.example`。
5. **端口**：Gunicorn 监听 `127.0.0.1:8000`，由 Nginx 反代；**外网只开 80 / 443，8000 不对外**。
   开发模式 `python app.py` 监听 `5000`。
6. **数据库备份**：SQLite 单文件 `cb_holders.db`，定时 `cp` 备份即可（见各部署文档的备份步骤）。

## 数据质量自检（verify_integrity.py）

`collect_daily.py` 每日 16:30 跑完核心 8 步后，第 ⑨ 步会执行 `verify_integrity.py` 的
一致性校验，防止历史 bug 回归。校验**不依赖集思录外部源**（避免登录 / 反爬脆弱依赖），
改用「内部自洽」方式：

1. **双低快照无未上市债**：最新一周 `double_low_log` 前 20 只，每只必须
   `daily_close.bond_close` 有数据。未上市债（如 `current_price=100` 占位面值、
   `bond_close` 全空）曾被误轮动调入，此校验防回归。
2. **等权指数 chg% 自洽**：最新一日 `equal_weight_index.index_value` 的环比，与
   「各债 chg% 等权平均」独立重算值偏差 < 0.1pp。旧实现用「均价环比」被价格加权污染，
   算成 +0.32% 而集思录为 -0.07%（差 0.39pp）；阈值收紧到 0.1pp 才能抓到这类回归。

第 ⑨ 步失败仅写 `collect_steps` warning（标记「⚠ 一致性校验失败」），**不阻塞**
核心 `ok_all` 判定——核心数据已成功，校验失败只是提醒该排查。可在
管理后台 `/admin/collect-logs` 查看。

手动运行：

```bash
python verify_integrity.py                   # 默认输出 + 退出码（非零=失败，适合 cron 报警）
python verify_integrity.py --json            # JSON 格式，给程序消费
python verify_integrity.py --tolerance 0.005  # 自定义等权指数偏差阈值（默认 0.001 = 0.1pp）
```

开发态更严的护栏：`db.compute_equal_weight_index(_self_check=True)`（或设环境变量
`KZZ_SELF_CHECK=1`）会在写库时 `assert`「index_value 环比」与「等权 chg%」一致，
一旦被改回均价环比会立即抛错，配合 CI / 单测使用。

## 配置说明（`config.py` + `.env`）

| 配置项 | 环境变量 | 说明 |
| --- | --- | --- |
| `ADMIN_USER` / `ADMIN_PASS` | `ADMIN_PASS` | 后台账号密码，默认 `admin / admin888`，**生产务必改** |
| `SECRET_KEY` | `CB_SECRET_KEY` | Flask 会话密钥，生产务必用随机串覆盖默认值 |
| `FLASK_DEBUG` | `FLASK_DEBUG` | 生产必须为 `0`（关闭调试与自动重载） |
| `PORT` | `PORT` | Gunicorn 监听端口，默认 `8000`（部署） |
| `CACHE_TTL_DAYS` | — | 已采集数据超过该天数则提示「较旧，建议更新」（默认仍返回缓存） |
| `EM_BASE` / `EM_HEADERS` | — | 东方财富数据中心接口与请求头 |
| `THS_BASE` | — | 同花顺 F10（best-effort，校正最新一期持有人性质） |

> 环境变量写在 `.env`（已 gitignore，模板 `.env.example`）；宝塔部署亦可填在面板「环境变量」中。

## 数据来源与口径

- **东方财富数据中心** `RPT_BOND_CB_HOLDER`：各报告期十大持有人（权威、免费、含全历史），
  返回 `HOLD_NUM`（单位：张）→ 自动 ÷10000 转为「万张」。
- **东方财富数据中心** `RPT_BOND_CB_LIST`：转债名称、正股、评级、发行规模、到期日等基础信息。
- **集思录** `adj_logs`：历史下修记录（下修前 / 后转股价、下修底价、股东大会日、生效日）。
- **腾讯** `qt.gtimg.cn`：实时 / 行情价（转债与正股）；详情页走势图、体检卡实时行情主源。
- **akshare**：强赎 / 不强赎公告（集思录强赎表）、历史下修兜底的兜底信息源。
- 同花顺 F10（best-effort）：校正最新一期「持有人标识」性质（基金 / 一般机构 / 个人）。

> 可转债十大持有人仅在年报、半年报披露，故数据天然有约半年时滞；一季报 / 三季报通常不含该表。

## 字段说明

- 性质（基金 / 一般机构 / 个人 / 未知）：东方财富不直接提供，本系统**按持有人名称规则推断**，
  并用同花顺「持有人标识」对最新一期做校正。个别机构管理的专户可能误判，属正常误差。
- 交易所后缀规则：代码以 `11` 开头 → 上交所 `.SH`；以 `12` 开头 → 深交所 `.SZ`。

## 合规提示

数据来自交易所指定披露平台的公开信息聚合，仅供个人研究使用；若用于对外售卖 / 再分发，
请自行评估相关授权与合规要求。
