"""本地 SQLite 存储层：转债基础信息 + 各报告期十大持有人。"""
import sqlite3
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 采集进程长事务写库时，让读请求等待而非直接报 "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bonds (
        bond_code            TEXT PRIMARY KEY,
        bond_name            TEXT,
        stock_code           TEXT,
        stock_name           TEXT,
        rating               TEXT,
        issue_scale          REAL,
        listing_date         TEXT,
        expire_date          TEXT,
        current_transfer_price TEXT,
        current_price        REAL,
        remaining_scale      REAL,
        redeem_price         REAL,
        data_source          TEXT,
        created_at           TEXT,
        updated_at           TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS holders (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        bond_code       TEXT,
        report_period   TEXT,
        rank            INTEGER,
        holder_name     TEXT,
        holder_nature   TEXT,
        is_natural      INTEGER DEFAULT 0,
        hold_amount     REAL,
        hold_ratio      REAL,
        data_source     TEXT,
        fetched_at      TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_holders_bond ON holders(bond_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_holders_period ON holders(bond_code, report_period)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_holders_natural ON holders(is_natural)")
    # 兼容旧库：holders 表缺 is_natural 列时补列，并回填存量自然人标记
    cur.execute("PRAGMA table_info(holders)")
    cols = [r[1] for r in cur.fetchall()]
    if "is_natural" not in cols:
        cur.execute("ALTER TABLE holders ADD COLUMN is_natural INTEGER DEFAULT 0")
        cur.execute("UPDATE holders SET is_natural=1 WHERE holder_nature='个人'")
    # 兼容旧库：bonds 表缺 current_price 列时补列
    cur.execute("PRAGMA table_info(bonds)")
    bcols = [r[1] for r in cur.fetchall()]
    if "current_price" not in bcols:
        cur.execute("ALTER TABLE bonds ADD COLUMN current_price REAL")
    # 兼容旧库：下修数据字段（历史下修次数 / 明细 JSON / 采集时间）
    cur.execute("PRAGMA table_info(bonds)")
    bcols2 = [r[1] for r in cur.fetchall()]
    for col, ctype in [
        ("down_revise_count", "INTEGER"),
        ("down_revise_json", "TEXT"),
        ("down_revise_updated_at", "TEXT"),
    ]:
        if col not in bcols2:
            cur.execute("ALTER TABLE bonds ADD COLUMN %s %s" % (col, ctype))
    # 兼容旧库：退市标记字段（is_delisted / delist_date）
    cur.execute("PRAGMA table_info(bonds)")
    bcols3 = [r[1] for r in cur.fetchall()]
    for col, ctype in [("is_delisted", "INTEGER"), ("delist_date", "TEXT"), ("remaining_scale", "REAL"), ("redeem_price", "REAL")]:
        if col not in bcols3:
            cur.execute("ALTER TABLE bonds ADD COLUMN %s %s" % (col, ctype))
    # 最近检索/浏览的转债（首页快捷入口）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recent_bonds (
        bond_code   TEXT PRIMARY KEY,
        bond_name   TEXT,
        stock_name  TEXT,
        viewed_at   TEXT
    )""")
    # 可转债公告（真实事件：强赎/不强赎/下修/即将发行；signal 为交易信号标记）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS announcements (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        bond_code     TEXT NOT NULL,
        bond_name     TEXT,
        announce_type TEXT NOT NULL,
        title         TEXT,
        announce_date TEXT,
        source        TEXT,
        official_url  TEXT,
        updated_at    TEXT,
        signal        TEXT,
        UNIQUE(bond_code, announce_type)
    )""")
    # 兼容旧库：signal 列已存在时 ALTER 会抛错，忽略即可
    try:
        cur.execute("ALTER TABLE announcements ADD COLUMN signal TEXT")
    except Exception:
        pass
    # 每日收盘行情（转债 + 正股）：历史收盘价图表与强赎预警的数据基础
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_close (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bond_code   TEXT NOT NULL,
        trade_date  TEXT NOT NULL,
        bond_close  REAL,
        stock_close REAL,
        updated_at  TEXT,
        UNIQUE(bond_code, trade_date)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS double_low_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        week_start   TEXT NOT NULL,
        rank         INTEGER,
        bond_code    TEXT NOT NULL,
        bond_name    TEXT,
        stock_name   TEXT,
        rating       TEXT,
        double_low   REAL,
        bond_price   REAL,
        premium_rate REAL,
        updated_at   TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_double_low_week ON double_low_log(week_start)")
    # 可转债等权指数（全样本价格等权，每日收盘后重算；基期取数据起点）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS equal_weight_index (
        trade_date   TEXT PRIMARY KEY,
        avg_price    REAL,
        median_price REAL,
        index_value  REAL,
        sample_n     INTEGER,
        updated_at   TEXT
    )""")
    # 站点设置（网站名称 / 域名 / logo）：单行存储，管理后台可改，即时生效无需重启
    cur.execute("""
    CREATE TABLE IF NOT EXISTS site_settings (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        site_name   TEXT,
        site_domain TEXT,
        site_logo   TEXT
    )""")
    conn.commit()
    conn.close()
    # 启动即按到期日/摘牌日幂等回填退市标记（覆盖存量债券）
    try:
        backfill_delist_status()
    except Exception:
        pass


# ---------------- 站点设置（名称 / 域名 / logo） ----------------
def get_site_settings():
    """返回站点设置 dict，缺失字段用默认值兜底。

    logo 存 base64 data URI 或图片 URL 字符串；为空字符串表示未设置。
    """
    defaults = {
        "site_name": "可转债持有人信息",
        "site_domain": "",
        "site_logo": "",
    }
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT site_name, site_domain, site_logo FROM site_settings WHERE id=1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {
                "site_name": row["site_name"] or defaults["site_name"],
                "site_domain": row["site_domain"] or "",
                "site_logo": row["site_logo"] or "",
            }
    except Exception:
        pass
    return defaults


def save_site_settings(site_name, site_domain, site_logo):
    """保存站点设置（upsert id=1）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO site_settings (id, site_name, site_domain, site_logo)
    VALUES (1, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        site_name=excluded.site_name,
        site_domain=excluded.site_domain,
        site_logo=excluded.site_logo
    """, (site_name, site_domain, site_logo))
    conn.commit()
    conn.close()


def upsert_bond(b):
    now = _now_str()
    p = {
        "bond_code": b.get("bond_code"),
        "bond_name": b.get("bond_name"),
        "stock_code": b.get("stock_code"),
        "stock_name": b.get("stock_name"),
        "rating": b.get("rating"),
        "issue_scale": b.get("issue_scale"),
        "listing_date": b.get("listing_date"),
        "expire_date": b.get("expire_date"),
        "current_transfer_price": b.get("current_transfer_price"),
        "current_price": b.get("current_price"),
        "data_source": b.get("data_source"),
        "created_at": b.get("created_at", now),
        "updated_at": b.get("updated_at", now),
        "is_delisted": b.get("is_delisted", 0) or 0,
        "delist_date": b.get("delist_date"),
    }
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO bonds (
        bond_code, bond_name, stock_code, stock_name, rating, issue_scale,
        listing_date, expire_date, current_transfer_price, current_price,
        data_source, created_at, updated_at, is_delisted, delist_date
    ) VALUES (
        :bond_code, :bond_name, :stock_code, :stock_name, :rating, :issue_scale,
        :listing_date, :expire_date, :current_transfer_price, :current_price,
        :data_source, :created_at, :updated_at, :is_delisted, :delist_date
    )
    ON CONFLICT(bond_code) DO UPDATE SET
        bond_name=excluded.bond_name,
        stock_code=excluded.stock_code,
        stock_name=excluded.stock_name,
        rating=excluded.rating,
        issue_scale=excluded.issue_scale,
        listing_date=excluded.listing_date,
        expire_date=excluded.expire_date,
        current_transfer_price=excluded.current_transfer_price,
        current_price=excluded.current_price,
        data_source=excluded.data_source,
        updated_at=excluded.updated_at,
        is_delisted=excluded.is_delisted,
        delist_date=excluded.delist_date
    """, p)
    conn.commit()
    conn.close()


def backfill_transfer_prices():
    """回填 bonds.current_transfer_price 中为空的行。

    口径：优先用集思录最新下修后转股价（down_revise_json 第一条 price_after，
    对下修过的债才是真正的当前价），回退东方财富 TRANSFER_VALUE。仅覆盖
    当前为空/缺失的行，不动已有值。返回更新的行数。
    """
    import json as _json
    import crawler as _crawler
    emap = _crawler.fetch_all_transfer_prices()
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute("SELECT bond_code, down_revise_json, current_transfer_price FROM bonds").fetchall()
    n = 0
    for code, raw, cur_tp in rows:
        price = None
        if raw:
            try:
                recs = _json.loads(raw)
                if recs and recs[0].get("price_after") is not None:
                    price = recs[0]["price_after"]
            except Exception:
                pass
        if price is None:
            price = emap.get(code)
        if (cur_tp is None or cur_tp == "") and price is not None:
            cur.execute("UPDATE bonds SET current_transfer_price=? WHERE bond_code=?", (price, code))
            n += 1
    conn.commit()
    conn.close()
    return n


def _down_revise_price_after(dj):
    """从 down_revise_json 取【最新一条】的下修后转股价（price_after）。

    返回 float 或 None。下修记录按 meeting_date 降序存储，取第一条非空 price_after 即
    当前生效转股价（所有历史记录均已生效）。无下修/无价则返 None。"""
    if not dj:
        return None
    import json as _json
    try:
        recs = _json.loads(dj) if isinstance(dj, str) else dj
    except Exception:
        return None
    if not recs:
        return None
    pa = recs[0].get("price_after")
    if pa is None:
        return None
    try:
        return float(pa)
    except (TypeError, ValueError):
        return None


def effective_transfer_price(bond):
    """单一权威『有效转股价』：有下修记录时以下修后最新价为准，否则用 bonds.current_transfer_price。

    详情页与列表页共用此函数，确保两处转股价值/转股溢价率口径完全一致、永不漂移。
    bond 需含 current_transfer_price 与 down_revise_json 字段。"""
    dj = bond.get("down_revise_json") if isinstance(bond, dict) else None
    pa = _down_revise_price_after(dj)
    if pa is not None:
        return pa
    tp = bond.get("current_transfer_price") if isinstance(bond, dict) else None
    try:
        return float(tp) if tp not in (None, "") else None
    except (TypeError, ValueError):
        return None


def reconcile_transfer_prices():
    """一次性/可复用：将 bonds.current_transfer_price 全量对齐为 effective_transfer_price。

    修复『有下修记录的转债，列表页转股价/转股价值与详情页不一致』问题（过去 akshare 回填
    覆盖了下修后价）。返回更新的行数。"""
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT bond_code, current_transfer_price, down_revise_json FROM bonds").fetchall()
    n = 0
    for code, cur_tp, dj in rows:
        eff = effective_transfer_price({"current_transfer_price": cur_tp, "down_revise_json": dj})
        if eff is None:
            continue
        try:
            cur_f = float(cur_tp) if cur_tp not in (None, "") else None
        except (TypeError, ValueError):
            cur_f = None
        if cur_f is None or abs(cur_f - eff) > 1e-6:
            cur.execute("UPDATE bonds SET current_transfer_price=? WHERE bond_code=?", (eff, code))
            n += 1
    conn.commit()
    conn.close()
    return n


def delete_holders(bond_code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM holders WHERE bond_code=?", (bond_code,))
    conn.commit()
    conn.close()


def insert_holders(rows):
    if not rows:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("""
    INSERT INTO holders (
        bond_code, report_period, rank, holder_name, holder_nature, is_natural,
        hold_amount, hold_ratio, data_source, fetched_at
    ) VALUES (
        :bond_code, :report_period, :rank, :holder_name, :holder_nature, :is_natural,
        :hold_amount, :hold_ratio, :data_source, :fetched_at
    )""", rows)
    conn.commit()
    conn.close()


def get_bond(bond_code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bonds WHERE bond_code=?", (bond_code,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None


def get_periods(bond_code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT report_period FROM holders WHERE bond_code=? ORDER BY report_period DESC",
        (bond_code,),
    )
    rows = [x[0] for x in cur.fetchall()]
    conn.close()
    return rows


def get_periods_info(bond_code):
    """返回该转债全部有持有人数据的报告期及每期持有人条数，按报告期倒序。

    用于详情页期数切换入口展示（如 [2023-06-30 (10), 2022-12-31 (10)]）。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT report_period, COUNT(*) AS cnt FROM holders WHERE bond_code=? "
        "GROUP BY report_period ORDER BY report_period DESC",
        (bond_code,),
    )
    rows = [{"period": r[0], "cnt": r[1]} for r in cur.fetchall()]
    conn.close()
    return rows



def get_holders(bond_code, period):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM holders WHERE bond_code=? AND report_period=? ORDER BY rank",
        (bond_code, period),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_natural_holders(bond_code=None):
    """自然人持有人查询（后续功能用）。bond_code 为空则返回全部转债的自然人持仓。

    返回字段含 bond_code / report_period / rank / holder_name / holder_nature /
    hold_amount / hold_ratio 等，可直接用于统计或展示。
    """
    conn = get_conn()
    cur = conn.cursor()
    if bond_code:
        cur.execute(
            "SELECT * FROM holders WHERE bond_code=? AND is_natural=1 "
            "ORDER BY report_period DESC, rank",
            (bond_code,),
        )
    else:
        cur.execute(
            "SELECT * FROM holders WHERE is_natural=1 "
            "ORDER BY bond_code, report_period DESC, rank"
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def list_bonds():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.*,
               (SELECT COUNT(*) FROM holders h WHERE h.bond_code=b.bond_code) AS holder_count,
               (SELECT COUNT(*) FROM holders h WHERE h.bond_code=b.bond_code AND h.is_natural=1) AS natural_count,
               (SELECT MAX(report_period) FROM holders h WHERE h.bond_code=b.bond_code) AS latest_period
        FROM bonds b
        WHERE (SELECT COUNT(*) FROM holders h WHERE h.bond_code=b.bond_code) > 0
        ORDER BY b.updated_at DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def list_market_bonds():
    """全市场可交易可转债（bonds 全量），并标注每只是否已采集持有人。

    返回字段：bond_code, bond_name, stock_code, stock_name, rating,
    current_price, issue_scale, holder_count(持有人记录数),
    latest_period(最近采集报告期)。holder_count>0 表示已采集持有人。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.bond_code, b.bond_name, b.stock_code, b.stock_name, b.rating,
               b.current_price, b.issue_scale, b.down_revise_count, b.is_delisted,
               (SELECT COUNT(*) FROM holders h WHERE h.bond_code=b.bond_code) AS holder_count,
               (SELECT MAX(report_period) FROM holders h WHERE h.bond_code=b.bond_code) AS latest_period
        FROM bonds b
        ORDER BY b.bond_code
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def search_bonds(code="", delisted="all", has_down="all", down_min=None,
                 sort="code", page=1, page_size=50):
    """检索已采集可转债，支持按编号/名称、退市状态、下修次数过滤与排序。

    参数：
      code     编号/名称/正股 模糊匹配
      delisted 'all' | 'delisted'(仅已退市) | 'active'(仅未退市)
      has_down 'all' | 'yes'(有下修) | 'no'(无下修)
      down_min 最小下修次数（int，可选）
      sort     'code' | 'down'(下修次数降) | 'updated'(最近更新降) |
               'holder'(持有人数降) | 'expire'(到期日升)
    返回 (rows, total)。
    """
    where = []
    params = []
    if code:
        like = "%" + code + "%"
        where.append("(b.bond_code LIKE ? OR b.bond_name LIKE ? OR b.stock_name LIKE ?)")
        params.extend([like, like, like])
    if delisted == "delisted":
        where.append("COALESCE(b.is_delisted,0)=1")
    elif delisted == "active":
        where.append("COALESCE(b.is_delisted,0)=0")
    if has_down == "yes":
        where.append("COALESCE(b.down_revise_count,0) > 0")
    elif has_down == "no":
        where.append("COALESCE(b.down_revise_count,0) = 0")
    if down_min is not None:
        where.append("COALESCE(b.down_revise_count,0) >= ?")
        params.append(down_min)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    order_map = {
        "code": "b.bond_code",
        "down": "COALESCE(b.down_revise_count,0) DESC, b.bond_code",
        "updated": "b.updated_at DESC",
        "holder": "holder_count DESC, b.bond_code",
        "expire": "b.expire_date ASC",
    }
    order = order_map.get(sort, "b.bond_code")
    # 排序时，已退市可转债统一排在最后（is_delisted=0 在前，1 在后）
    order = "COALESCE(b.is_delisted,0) ASC, " + order

    # 持有人数 / 最近采集期 用一次子查询聚合，避免逐行相关子查询
    base = (
        "FROM bonds b "
        "LEFT JOIN (SELECT bond_code, COUNT(*) AS holder_count, "
        "                  MAX(report_period) AS latest_period "
        "           FROM holders GROUP BY bond_code) h ON h.bond_code = b.bond_code"
    )
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) " + base + where_sql, params)
    total = cur.fetchone()[0]
    # page_size<=0 表示取全部匹配行（调用方自行在 Python 层排序/分页）
    if page_size and page_size > 0:
        cur.execute(
            "SELECT b.*, COALESCE(h.holder_count,0) AS holder_count, h.latest_period "
            + base + where_sql + " ORDER BY " + order + " LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size])
    else:
        cur.execute(
            "SELECT b.*, COALESCE(h.holder_count,0) AS holder_count, h.latest_period "
            + base + where_sql + " ORDER BY " + order, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows, total


def count_bonds():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bonds")
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_all_natural_persons(limit=2000, offset=0):
    """自然人聚合榜：按持仓估算市值（万元）降序排列，支持分页。

    市值口径（与 get_natural_ranking / 详情页一致）：每个（自然人, 转债）取最新报告期
    持仓，市值 = 持有量(万张) × 现价(元/张)；现价缺失按面值 100 元/张 估算。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        WITH latest AS (
            SELECT h.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.holder_name, h.bond_code
                       ORDER BY h.report_period DESC
                   ) AS rn
            FROM holders h
            WHERE h.is_natural = 1
        )
        SELECT l.holder_name,
               COUNT(*)                        AS record_count,
               COUNT(DISTINCT l.bond_code)     AS bond_count,
               ROUND(SUM(l.hold_amount * COALESCE(b.current_price, 100.0)), 2) AS mv_wan
        FROM latest l
        LEFT JOIN bonds b ON l.bond_code = b.bond_code
        WHERE l.rn = 1 AND COALESCE(b.is_delisted, 0) = 0
        GROUP BY l.holder_name
        ORDER BY mv_wan DESC, bond_count DESC, l.holder_name
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def count_natural_persons():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        WITH latest AS (
            SELECT h.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.holder_name, h.bond_code
                       ORDER BY h.report_period DESC
                   ) AS rn
            FROM holders h
            WHERE h.is_natural = 1
        )
        SELECT COUNT(DISTINCT l.holder_name)
        FROM latest l
        LEFT JOIN bonds b ON l.bond_code = b.bond_code
        WHERE l.rn = 1 AND COALESCE(b.is_delisted, 0) = 0
    """)
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_all_institutions(limit=2000, offset=0):
    """机构持有人聚合榜：按持仓估算市值（万元）降序排列，支持分页。

    与 get_all_natural_persons 口径一致，仅筛选 is_natural = 0（机构/基金）。
    市值 = 持有量(万张) × 现价(元/张)，现价缺失按面值 100 元/张 估算。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        WITH latest AS (
            SELECT h.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY h.holder_name, h.bond_code
                       ORDER BY h.report_period DESC
                   ) AS rn
            FROM holders h
            WHERE h.is_natural = 0
        )
        SELECT l.holder_name,
               COUNT(*)                        AS record_count,
               COUNT(DISTINCT l.bond_code)     AS bond_count,
               ROUND(SUM(l.hold_amount * COALESCE(b.current_price, 100.0)), 2) AS mv_wan
        FROM latest l
        LEFT JOIN bonds b ON l.bond_code = b.bond_code
        WHERE l.rn = 1 AND COALESCE(b.is_delisted, 0) = 0
        GROUP BY l.holder_name
        ORDER BY mv_wan DESC, bond_count DESC, l.holder_name
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def count_institutions():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT holder_name) FROM holders WHERE is_natural = 0")
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_person_holdings(name):
    """某自然人持有的全部债券明细（跨转债、跨报告期）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT h.*, b.bond_name, b.stock_name, b.stock_code, b.rating, b.current_price, b.is_delisted
        FROM holders h
        LEFT JOIN bonds b ON h.bond_code = b.bond_code
        WHERE h.holder_name = ?
        ORDER BY h.bond_code, h.report_period DESC, h.rank
    """, (name,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_person_latest_holdings(name):
    """某自然人去重后的最新持仓汇总（每只转债只取最新报告期）。

    返回列表，每项含：bond_code, bond_name, stock_name, report_period,
    hold_amount(万张), current_price(元/张), periods_count(该转债出现期数),
    mv_wan(该转债估算市值，万元)。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT h.bond_code, h.report_period, h.hold_amount,
               b.bond_name, b.stock_name, b.current_price, b.is_delisted, b.delist_date
        FROM holders h
        LEFT JOIN bonds b ON h.bond_code = b.bond_code
        WHERE h.holder_name = ?
        ORDER BY h.bond_code, h.report_period DESC
    """, (name,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    best = {}
    for r in rows:
        code = r["bond_code"]
        rp = r["report_period"] or ""
        if code not in best or rp > (best[code]["report_period"] or ""):
            best[code] = r

    out = []
    for code, r in best.items():
        price = r["current_price"] if r["current_price"] else 100.0
        mv = (r["hold_amount"] or 0) * price
        out.append({
            "bond_code": code,
            "bond_name": r["bond_name"] or code,
            "stock_name": r["stock_name"] or "-",
            "report_period": r["report_period"] or "-",
            "hold_amount": r["hold_amount"],
            "current_price": price,
            "is_delisted": r["is_delisted"],
            "delist_date": r.get("delist_date"),
            "periods_count": sum(1 for x in rows if x["bond_code"] == code),
            "mv_wan": round(mv, 2),
        })
    out.sort(key=lambda x: x["mv_wan"], reverse=True)
    return out


def get_person_market_value(name):
    """某自然人去重后的估算市值（万元）及统计口径。

    关键：同一只转债可能出现在多个报告期，不能把各期持有量简单相加（否则
    会重复计算市值，闹笑话）。口径与 get_natural_ranking 一致——
    每个 (自然人, 转债) 仅取最新报告期持仓，市值 = 持有量(万张) × 现价(元/张)。
    现价缺失按面值 100 元/张 估算。

    返回 (mv_wan, bond_count, record_count)：
      mv_wan      去重估算市值（万元，保留两位小数）
      bond_count  涉及转债数（按 bond_code 去重）
      record_count 持仓记录条数（跨报告期原始记录数）
    """
    holdings = get_person_latest_holdings(name)
    mv = sum(h["mv_wan"] for h in holdings)
    return round(mv, 2), len(holdings), sum(h["periods_count"] for h in holdings)


def get_natural_ranking(limit=50):
    """可转债牛散榜：自然人按持仓市值（估算）降序排列。

    市值估算口径：对每个（自然人, 转债）取最新报告期持仓，市值 = 持有量(万张) × 现价(元/张)
    = 万元；现价缺失时按面值 100 元/张 估算。返回字段：
      holder_name, mv_wan(估算市值,万元), bond_count(涉及转债数),
      record_count(出现次数), top_bonds([{bond_name, mv_wan}...])
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT h.holder_name, h.bond_code, h.report_period, h.hold_amount,
               b.bond_name, b.current_price, b.is_delisted
        FROM holders h
        LEFT JOIN bonds b ON h.bond_code = b.bond_code
        WHERE h.is_natural = 1
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # 每个 (自然人, 转债) 仅保留最新报告期
    best = {}
    for r in rows:
        key = (r["holder_name"], r["bond_code"])
        if key not in best or r["report_period"] > best[key]["report_period"]:
            best[key] = r

    # 按自然人聚合
    agg = {}
    for (name, _code), v in best.items():
        # 已退市可转债不计入自然人持仓市值（与牛散榜口径一致）
        if v.get("is_delisted"):
            continue
        price = v["current_price"] if v["current_price"] else 100.0
        mv = (v["hold_amount"] or 0) * price  # 万元
        a = agg.setdefault(name, {"holder_name": name, "mv_wan": 0.0,
                                  "bonds": set(), "top": []})
        a["mv_wan"] += mv
        a["bonds"].add(v["bond_code"])
        a["top"].append({"bond_name": v["bond_name"] or v["bond_code"], "bond_code": v["bond_code"], "mv_wan": mv})

    result = []
    for name, a in agg.items():
        top = sorted(a["top"], key=lambda x: x["mv_wan"], reverse=True)[:3]
        result.append({
            "holder_name": name,
            "mv_wan": round(a["mv_wan"], 2),
            "bond_count": len(a["bonds"]),
            "record_count": len(a["top"]),
            "top_bonds": top,
        })
    result.sort(key=lambda x: x["mv_wan"], reverse=True)
    return result[:limit]


def get_institution_ranking(limit=50):
    """可转债机构榜：机构（含基金 / 一般机构）按持仓市值（估算）降序排列。

    市值估算口径与 get_natural_ranking 一致：每个（机构, 转债）仅取最新报告期
    持仓，市值 = 持有量(万张) × 现价(元/张) = 万元；现价缺失按面值 100 元/张 估算。
    返回字段：holder_name, mv_wan, bond_count, record_count, top_bonds。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT h.holder_name, h.bond_code, h.report_period, h.hold_amount,
               b.bond_name, b.current_price, b.is_delisted
        FROM holders h
        LEFT JOIN bonds b ON h.bond_code = b.bond_code
        WHERE h.is_natural = 0
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    best = {}
    for r in rows:
        key = (r["holder_name"], r["bond_code"])
        if key not in best or r["report_period"] > best[key]["report_period"]:
            best[key] = r

    agg = {}
    for (name, _code), v in best.items():
        # 已退市可转债不计入机构持仓市值
        if v.get("is_delisted"):
            continue
        price = v["current_price"] if v["current_price"] else 100.0
        mv = (v["hold_amount"] or 0) * price
        a = agg.setdefault(name, {"holder_name": name, "mv_wan": 0.0,
                                  "bonds": set(), "top": []})
        a["mv_wan"] += mv
        a["bonds"].add(v["bond_code"])
        a["top"].append({"bond_name": v["bond_name"] or v["bond_code"], "bond_code": v["bond_code"], "mv_wan": mv})

    result = []
    for name, a in agg.items():
        top = sorted(a["top"], key=lambda x: x["mv_wan"], reverse=True)[:3]
        result.append({
            "holder_name": name,
            "mv_wan": round(a["mv_wan"], 2),
            "bond_count": len(a["bonds"]),
            "record_count": len(a["top"]),
            "top_bonds": top,
        })
    result.sort(key=lambda x: x["mv_wan"], reverse=True)
    return result[:limit]


# ---------------- 历史下修数据（集思录 adj_logs 采集） ----------------
def save_down_revise(bond_code, count, records):
    """保存某转债的历史下修数据。

    count   : 下修次数（整数）
    records : list[dict]，每条含 meeting_date / price_before / price_after /
              effective_date / floor_price / bond_name
    """
    import json as _json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE bonds
        SET down_revise_count = ?,
            down_revise_json = ?,
            down_revise_updated_at = ?
        WHERE bond_code = ?
    """, (count, _json.dumps(records, ensure_ascii=False), _now_str(), bond_code))
    # 同步『有效转股价』列：以最新下修后价为当前转股价，保证列表页/排序与详情页口径一致
    eff = _down_revise_price_after(_json.dumps(records, ensure_ascii=False))
    if eff is not None:
        cur.execute("UPDATE bonds SET current_transfer_price=? WHERE bond_code=?",
                    (eff, bond_code))
    if cur.rowcount == 0:
        # 转债尚不在 bonds 表（理论不会发生，seed 已全量），先插入空壳
        cur.execute("""
            INSERT OR IGNORE INTO bonds (bond_code, down_revise_count,
                down_revise_json, down_revise_updated_at)
            VALUES (?, ?, ?, ?)
        """, (bond_code, count, _json.dumps(records, ensure_ascii=False), _now_str()))
    conn.commit()
    conn.close()


def get_down_revise(bond_code):
    """返回 (count, records, updated_at)。

    count     : 下修次数（None 表示尚未采集）
    records   : 解析后的 list[dict]
    updated_at: 采集时间字符串
    """
    import json as _json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT down_revise_count, down_revise_json, down_revise_updated_at "
        "FROM bonds WHERE bond_code=?", (bond_code,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None, [], None
    count = r[0]
    raw = r[1]
    try:
        records = _json.loads(raw) if raw else []
    except Exception:
        records = []
    return count, records, r[2]


def get_down_revise_count(bond_code):
    """快速取单只转债的下修次数（未采集返回 None）。供列表/个人页批量展示。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT down_revise_count FROM bonds WHERE bond_code=?", (bond_code,))
    r = cur.fetchone()
    conn.close()
    return r[0] if r else None


def _now_str():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def compute_delist(row):
    """根据东方财富可转债行字典判定退市状态。

    返回 (is_delisted:int, delist_date:str|None)。判定信号（任一满足即视为「已退市」）：
      1) DELIST_DATE（摘牌日）存在；
      2) 状态字段（BOND_STATUS / TRADE_STATUS / LISTING_STATUS 等）含「退市/摘牌/终止上市/停止上市/暂停上市」；
      3) EXPIRE_DATE（到期日）早于今日（到期即停止交易，属于退市）。
    第 3 条作为兜底，保证即使接口未返回退市专用字段，已到期债券也能被正确标记。
    """
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    delist_date = (str(row.get("DELIST_DATE") or "") or "")[:10] or None
    status_raw = " ".join(str(row.get(k) or "") for k in (
        "BOND_STATUS", "TRADE_STATUS", "LISTING_STATUS", "BOND_STATE",
        "TRADE_STATUS_NAME", "BOND_STATUS_NAME"))
    is_delisted = 0
    if delist_date:
        is_delisted = 1
    elif any(w in status_raw for w in ("退市", "摘牌", "终止上市", "停止上市", "暂停上市")):
        is_delisted = 1
        delist_date = delist_date or (str(row.get("EXPIRE_DATE") or "") or "")[:10] or None
    if not is_delisted:
        expire = (str(row.get("EXPIRE_DATE") or "") or "")[:10] or None
        if expire and expire < today:
            is_delisted = 1
            delist_date = delist_date or expire
    return is_delisted, delist_date


def backfill_delist_status():
    """按已存储的到期日/摘牌日，幂等回填 bonds 表的 is_delisted / delist_date。

    规则：DELIST_DATE 已存在 → 已退市；EXPIRE_DATE < 今日 → 已到期退市。
    仅对「未标记退市」的行做正向标记，不做反向清除（尊重手动/接口标记的已退市状态）。
    返回本次新标记的行数。
    """
    from datetime import date
    today = date.today().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bond_code, expire_date, delist_date, is_delisted FROM bonds")
    rows = cur.fetchall()
    n = 0
    for code, expire, ddate, is_del in rows:
        if is_del:
            continue
        new_dd = None
        delisted = 0
        if ddate:
            delisted = 1
            new_dd = ddate
        elif expire and expire < today:
            delisted = 1
            new_dd = expire
        if delisted:
            cur.execute("UPDATE bonds SET is_delisted=1, delist_date=? WHERE bond_code=?",
                        (new_dd, code))
            n += 1
    conn.commit()
    conn.close()
    return n


def set_delisted(code, delisted, delist_date=None):
    """手动设置某转债的退市标记（管理后台切换用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE bonds SET is_delisted=?, delist_date=? WHERE bond_code=?",
                (1 if delisted else 0, delist_date, code))
    conn.commit()
    conn.close()


def record_bond_view(bond_code, bond_name=None, stock_name=None):
    """记录一只转债被检索/浏览，供首页「最近检索」快捷入口。UPSERT 按 viewed_at 更新。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO recent_bonds (bond_code, bond_name, stock_name, viewed_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(bond_code) DO UPDATE SET
        bond_name=excluded.bond_name,
        stock_name=excluded.stock_name,
        viewed_at=excluded.viewed_at
    """, (bond_code, bond_name, stock_name, _now_str()))
    conn.commit()
    conn.close()


def get_recent_bonds(limit=12):
    """返回最近浏览的转债（按 viewed_at 倒序），用于首页快捷入口。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT r.bond_code, r.bond_name, r.stock_name, b.is_delisted "
                "FROM recent_bonds r LEFT JOIN bonds b ON r.bond_code=b.bond_code "
                "ORDER BY r.viewed_at DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------- 可转债公告 ----------------
def upsert_announcement(a):
    """写入/更新一条公告。按 (bond_code, announce_type) 去重（覆盖式），
    保证「下修/强赎」等状态每天重算时只更新不堆积。
    signal: 交易信号标记，buy=买入信号 / sell=持仓离场信号 / neutral=中性观察。"""
    now = _now_str()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO announcements (bond_code, bond_name, announce_type, title,
                               announce_date, source, official_url, signal, updated_at)
    VALUES (:bond_code, :bond_name, :announce_type, :title,
            :announce_date, :source, :official_url, :signal, :updated_at)
    ON CONFLICT(bond_code, announce_type) DO UPDATE SET
        bond_name=excluded.bond_name,
        title=excluded.title,
        announce_date=excluded.announce_date,
        source=excluded.source,
        official_url=excluded.official_url,
        signal=excluded.signal,
        updated_at=excluded.updated_at
    """, {
        "bond_code": a.get("bond_code"),
        "bond_name": a.get("bond_name"),
        "announce_type": a.get("announce_type"),
        "title": a.get("title"),
        "announce_date": a.get("announce_date"),
        "source": a.get("source", "东财"),
        "official_url": a.get("official_url"),
        "signal": a.get("signal", "neutral"),
        "updated_at": now,
    })
    conn.commit()
    conn.close()


def get_announcements(atype=None, limit=2000):
    """读取公告列表。可按 announce_type 筛选；默认按 announce_date 倒序。"""
    conn = get_conn()
    cur = conn.cursor()
    if atype:
        cur.execute("SELECT * FROM announcements WHERE announce_type=? "
                    "ORDER BY announce_date DESC LIMIT ?", (atype, limit))
    else:
        cur.execute("SELECT * FROM announcements ORDER BY announce_date DESC LIMIT ?",
                    (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_bond_announcements(bond_code, atype=None):
    """返回单只转债的全部公告（按 announce_date 倒序）；可按 announce_type 过滤。

    用于详情页判断该债是否「已公告强赎」等，并展示公告原文链接。
    """
    conn = get_conn()
    cur = conn.cursor()
    if atype:
        cur.execute("SELECT * FROM announcements WHERE bond_code=? AND announce_type=? "
                    "ORDER BY announce_date DESC", (bond_code, atype))
    else:
        cur.execute("SELECT * FROM announcements WHERE bond_code=? "
                    "ORDER BY announce_date DESC", (bond_code,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_redeemed_bond_codes():
    """返回已发布强赎公告（announce_type='强赎'）的可转债代码集合，供各列表页全局打标。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT bond_code FROM announcements WHERE announce_type='强赎'")
    s = {r[0] for r in cur.fetchall()}
    conn.close()
    return s


def get_announcement_type_counts():
    """返回各公告类型的数量，供列表页 tabs 展示。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT announce_type, COUNT(*) n FROM announcements "
                "GROUP BY announce_type")
    rows = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()
    return rows


def clear_announcements():
    """清空公告表（重新全量计算前调用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM announcements")
    conn.commit()
    conn.close()


def get_bonds_with_down_revise(limit=2000):
    """返回有下修历史的转债（down_revise_count>0 且 down_revise_json 非空）。

    供公告模块生成「下修（已下修）」类。返回 list[dict]，含
    bond_code/bond_name/down_revise_json。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT bond_code, bond_name, down_revise_json, stock_code
        FROM bonds
        WHERE down_revise_count > 0 AND down_revise_json IS NOT NULL
          AND TRIM(down_revise_json) <> ''
          AND COALESCE(is_delisted, 0) = 0
        ORDER BY bond_code
        LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ============ 每日收盘行情（转债 + 正股） ============

def upsert_daily_close(bond_code, trade_date, bond_close=None, stock_close=None):
    """写入某转债某交易日的收盘价（转债 / 正股分开设置，互不覆盖）。
    转债与正股日K日期可能不完全对齐，故先 INSERT OR IGNORE 保证行存在，
    再仅更新非 None 的字段（bond_close 或 stock_close）。"""
    now = _now_str()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO daily_close(bond_code, trade_date, updated_at) VALUES(?,?,?)",
                (bond_code, trade_date, now))
    if bond_close is not None:
        cur.execute("UPDATE daily_close SET bond_close=?, updated_at=? WHERE bond_code=? AND trade_date=?",
                    (bond_close, now, bond_code, trade_date))
    if stock_close is not None:
        cur.execute("UPDATE daily_close SET stock_close=?, updated_at=? WHERE bond_code=? AND trade_date=?",
                    (stock_close, now, bond_code, trade_date))
    conn.commit()
    conn.close()


def get_daily_close(bond_code, days=250):
    """返回某转债最近 days 个交易日的收盘价序列，按日期升序。
    每项 dict(trade_date, bond_close, stock_close)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, bond_close, stock_close FROM daily_close
        WHERE bond_code = ?
        ORDER BY trade_date DESC LIMIT ?
    """, (bond_code, days))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    rows.reverse()
    return rows


def get_active_trading_bonds():
    """返回仍在交易的转债（未退市且未到期），供每日采集与强赎预警使用。
    返回 list[dict(bond_code, bond_name, stock_code, current_transfer_price, expire_date)]。
    """
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT bond_code, bond_name, stock_code, current_transfer_price, expire_date
        FROM bonds
        WHERE COALESCE(is_delisted, 0) = 0
          AND (expire_date IS NULL OR expire_date >= ?)
        ORDER BY bond_code
    """, (today,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ============ 双低策略轮动快照 ============

def get_latest_quotes(bond_code):
    """返回某转债最新非空转债价与正股价（分别取各自最近交易日，互不对齐）。"""
    conn = get_conn()
    cur = conn.cursor()
    bc = cur.execute(
        "SELECT bond_close FROM daily_close WHERE bond_code=? AND bond_close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1", (bond_code,)).fetchone()
    sc = cur.execute(
        "SELECT stock_close FROM daily_close WHERE bond_code=? AND stock_close IS NOT NULL "
        "ORDER BY trade_date DESC LIMIT 1", (bond_code,)).fetchone()
    conn.close()
    return {"bond_close": bc[0] if bc else None, "stock_close": sc[0] if sc else None}


def save_double_low_snapshot(week_start, rows):
    """幂等写入某周某次轮动的前 N 只双低转债快照（先删后插）。
    rows: list[dict(rank, bond_code, bond_name, stock_name, rating,
                    double_low, bond_price, premium_rate)]。"""
    now = _now_str()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM double_low_log WHERE week_start=?", (week_start,))
    for r in rows:
        cur.execute("""
            INSERT INTO double_low_log
                (week_start, rank, bond_code, bond_name, stock_name, rating,
                 double_low, bond_price, premium_rate, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (week_start, r.get("rank"), r.get("bond_code"), r.get("bond_name"),
              r.get("stock_name"), r.get("rating"), r.get("double_low"),
              r.get("bond_price"), r.get("premium_rate"), now))
    conn.commit()
    conn.close()


def get_latest_double_low():
    """返回最近一次轮动（week_start 最大）的前 20 只快照，按 rank 升序。"""
    conn = get_conn()
    cur = conn.cursor()
    ws = cur.execute(
        "SELECT week_start FROM double_low_log GROUP BY week_start "
        "ORDER BY week_start DESC LIMIT 1").fetchone()
    if not ws:
        conn.close()
        return None
    rows = [dict(r) for r in cur.execute(
        "SELECT rank, bond_code, bond_name, stock_name, rating, "
        "double_low, bond_price, premium_rate FROM double_low_log "
        "WHERE week_start=? ORDER BY rank", (ws[0],)).fetchall()]
    conn.close()
    return {"week_start": ws[0], "rows": rows}


def get_double_low_change():
    """返回最新一次轮动相对上一次的变更：{week_start, prev_week_start, current, entered, exited}。
    若无任何快照返回 None；只有一次快照时 entered=全部、exited=空。"""
    conn = get_conn()
    cur = conn.cursor()
    weeks = [r[0] for r in cur.execute(
        "SELECT DISTINCT week_start FROM double_low_log ORDER BY week_start DESC").fetchall()]
    if not weeks:
        conn.close()
        return None
    latest_ws = weeks[0]
    prev_ws = weeks[1] if len(weeks) > 1 else None

    def _load(ws):
        return [dict(r) for r in cur.execute(
            "SELECT rank, bond_code, bond_name, stock_name, rating, "
            "double_low, bond_price, premium_rate FROM double_low_log "
            "WHERE week_start=? ORDER BY rank", (ws,)).fetchall()]

    latest_rows = _load(latest_ws)
    prev_rows = _load(prev_ws) if prev_ws else []
    conn.close()
    latest_codes = {r["bond_code"] for r in latest_rows}
    prev_codes = {r["bond_code"] for r in prev_rows}
    entered = [r for r in latest_rows if r["bond_code"] not in prev_codes]
    exited = [r for r in prev_rows if r["bond_code"] not in latest_codes]
    return {"week_start": latest_ws, "prev_week_start": prev_ws,
            "current": latest_rows, "entered": entered, "exited": exited}


def _load_all_double_low_snapshots():
    """载入全部轮动周快照（升序），返回 (weeks升序, {week_start: [row,...]})。"""
    conn = get_conn()
    cur = conn.cursor()
    weeks = [r[0] for r in cur.execute(
        "SELECT DISTINCT week_start FROM double_low_log ORDER BY week_start ASC").fetchall()]
    week_rows = {}
    for ws in weeks:
        week_rows[ws] = [dict(r) for r in cur.execute(
            "SELECT rank, bond_code, bond_name, stock_name, rating, "
            "double_low, bond_price, premium_rate FROM double_low_log "
            "WHERE week_start=? ORDER BY rank", (ws,)).fetchall()]
    conn.close()
    return weeks, week_rows


def _bond_entry_exit_map(weeks, week_rows):
    """每只债的调入周/价、末次持有周/价（从所有快照重建，无需额外表）。"""
    first_seen, last_seen = {}, {}
    for ws in weeks:  # 已升序
        for r in week_rows[ws]:
            code = r["bond_code"]
            if code not in first_seen:
                first_seen[code] = (ws, r["bond_price"])
            last_seen[code] = (ws, r["bond_price"])
    return first_seen, last_seen


def _load_closes_for_codes(codes):
    """批量载入指定转债的每日收盘价：{code: [(trade_date, bond_close), ...] 升序}。"""
    if not codes:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    ph = ",".join("?" * len(codes))
    rows = cur.execute(
        "SELECT bond_code, trade_date, bond_close FROM daily_close "
        "WHERE bond_code IN (%s)" % ph, codes).fetchall()
    conn.close()
    d = {}
    for code, td, bc in rows:
        if bc is None:
            continue
        d.setdefault(code, []).append((td, bc))
    for code in d:
        d[code].sort()
    return d


def get_latest_stock_closes(codes):
    """批量取每只转债【最新交易日】的非空正股收盘价：{bond_code: stock_close(float)}。

    用于列表页批量计算转股价值/转股溢价率，避免逐行查询。仅取 stock_close>0 的有效值，
    取 trade_date 最大者（与详情页『取 daily 最新非空正股收盘价』口径一致）。"""
    if not codes:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    ph = ",".join("?" * len(codes))
    rows = cur.execute(
        "SELECT bond_code, trade_date, stock_close FROM daily_close "
        "WHERE bond_code IN (%s) AND stock_close IS NOT NULL AND stock_close > 0" % ph,
        codes).fetchall()
    conn.close()
    best = {}
    for code, td, sc in rows:
        try:
            sc = float(sc)
        except (TypeError, ValueError):
            continue
        cur_best = best.get(code)
        if cur_best is None or td > cur_best[0]:
            best[code] = (td, sc)
    return {k: v[1] for k, v in best.items()}


def _close_on_or_before(closes, code, date):
    """取 code 在 <= date 的最近一个交易日收盘价（date 为 'YYYY-MM-DD' 字符串）。"""
    arr = closes.get(code)
    if not arr:
        return None
    lo, hi, res = 0, len(arr) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid][0] <= date:
            res = arr[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return res


def get_latest_data_date():
    """返回 daily_close 中最新的交易日（即全站行情/数据更新截至日期）。

    由每日采集总入口 collect_daily.py 写入；为 None 时表示尚未采集过行情。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        row = cur.execute("SELECT MAX(trade_date) FROM daily_close").fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_double_low_history():
    """返回所有轮动周（降序）明细，含每期调出债的持有收益。

    收益价格口径（修正版）：不再使用轮动快照里曾被限流冻结的 bond_price，
    而是按每只转债的『轮动周一』去 daily_close 取当日真实收盘价（取 <= 该周一的
    最近交易日，daily_close 无数据才回退快照价）。这样收益反映真实市价变动，
    避免限流期间价格冻结导致的 0.00% 失真。
      - 调入价 = 首次进入组合那周周一的真实收盘
      - 调出价 = 本轮被调出那周周一的真实收盘（即实际卖出日）
      - 持有收益 = (调出价-调入价)/调入价
    本期轮动收益 = 本期调出债持有收益的等权平均（None 时不计）。
    """
    weeks, week_rows = _load_all_double_low_snapshots()
    if not weeks:
        return []
    first_seen, _ = _bond_entry_exit_map(weeks, week_rows)
    all_codes = set()
    for ws in week_rows:
        for r in week_rows[ws]:
            all_codes.add(r["bond_code"])
    closes = _load_closes_for_codes(list(all_codes))

    def real_price(code, ws, snap_price):
        c = _close_on_or_before(closes, code, ws)
        return c if c is not None else snap_price

    weeks_desc = list(reversed(weeks))
    out = []
    for i, ws in enumerate(weeks_desc):
        rows = week_rows[ws]
        prev_ws = weeks_desc[i + 1] if i + 1 < len(weeks_desc) else None
        prev_codes = {r["bond_code"] for r in week_rows[prev_ws]} if prev_ws else set()
        cur_codes = {r["bond_code"] for r in rows}
        entered = []
        for r in rows:
            if r["bond_code"] not in prev_codes:
                ew, ep_snap = first_seen[r["bond_code"]]
                entered.append({**r, "entry_week": ew,
                                "entry_price": real_price(r["bond_code"], ew, ep_snap)})
        exited = []
        if prev_ws:
            for r in week_rows[prev_ws]:
                if r["bond_code"] not in cur_codes:
                    code = r["bond_code"]
                    ew, ep_snap = first_seen[code]
                    entry_price = real_price(code, ew, ep_snap)
                    exit_price = real_price(code, ws, r["bond_price"])  # 调出=轮动周一真实收盘
                    ret = None
                    if entry_price and exit_price and entry_price > 0:
                        ret = (exit_price - entry_price) / entry_price * 100.0
                    exited.append({**r, "entry_week": ew,
                                   "entry_price": round(entry_price, 2) if entry_price else None,
                                   "exit_week": ws,
                                   "exit_price": round(exit_price, 2) if exit_price else None,
                                   "return_pct": round(ret, 2) if ret is not None else None})
        rets = [e["return_pct"] for e in exited if e["return_pct"] is not None]
        week_return = round(sum(rets) / len(rets), 2) if rets else None
        out.append({"week_start": ws, "rows": rows, "entered": entered,
                    "exited": exited, "week_return": week_return,
                    "n_entered": len(entered), "n_exited": len(exited)})
    return out


def get_double_low_holds():
    """当前持仓（最新一周快照）含调入价与累计（未实现）收益。

    调入价 = 首次进入周周一真实收盘；当前价 = daily_close 最新可得收盘（mark-to-market），
    全部优先用 daily_close 真实价，缺失才回退快照 bond_price。
    """
    weeks, week_rows = _load_all_double_low_snapshots()
    if not weeks:
        return []
    first_seen, _ = _bond_entry_exit_map(weeks, week_rows)
    latest_ws = weeks[-1]
    all_codes = [r["bond_code"] for r in week_rows[latest_ws]]
    closes = _load_closes_for_codes(all_codes)

    def real_price(code, ws, snap_price):
        c = _close_on_or_before(closes, code, ws)
        return c if c is not None else snap_price

    rows = []
    for r in week_rows[latest_ws]:
        code = r["bond_code"]
        ew, ep_snap = first_seen.get(code, (latest_ws, r["bond_price"]))
        entry_price = real_price(code, ew, ep_snap)
        cur_price = real_price(code, "2099-12-31", r["bond_price"])  # 最新可得收盘
        ret = None
        if entry_price and cur_price and entry_price > 0:
            ret = (cur_price - entry_price) / entry_price * 100.0
        rows.append({**r, "entry_week": ew,
                     "entry_price": round(entry_price, 2) if entry_price else None,
                     "hold_return": round(ret, 2) if ret is not None else None})
    return rows


# ============ 市场概览（中位数 / 均价 / 存量 / 新发 / 退市比例） ============

def _is_eb(code, name=None):
    """判断是否为可交换债 EB（统计时应剔除）。
    EB 代码：沪市 132 开头、深市 120 开头；名称常含 'EB' 或 '可交换'。"""
    if code:
        if code.startswith("132") or code.startswith("120"):
            return True
    if name and ("EB" in name or "可交换" in name):
        return True
    return False


def _eb_filter_sql(alias="b"):
    """生成剔除 EB 的 SQL 条件片段（针对 bonds 表别名 alias）。"""
    return ("(%s.bond_code NOT LIKE '132%%' AND %s.bond_code NOT LIKE '120%%' "
            "AND (%s.bond_name IS NULL OR %s.bond_name NOT LIKE '%%可交换%%'))"
            % (alias, alias, alias, alias))


def compute_market_overview():
    """返回首页「市场概览」核心指标。口径（与用户定义一致，剔除可交换债 EB）：
      - 中位数 / 均价：基于【存量(is_delisted=0) + 非EB + current_price 非空】的转债现价
      - 存量总数：is_delisted=0 且非EB
      - 新发数：is_delisted=0 且非EB 且 listing_date 在最近 180 天内（滚动）
      - 退市比例：以【当年 1 月 1 日存量】为基数，当年(截至今天)退市数 / 基数
        基数 = 在当年1月1日仍为存量的债（未退市，或虽退市但退市日>=当年1月1日）
    返回 dict。"""
    import datetime as _dt, statistics as _st
    today = _dt.date.today()
    year = today.year
    jan1 = "%d-01-01" % year
    cutoff = (today - _dt.timedelta(days=180)).strftime("%Y-%m-%d")
    today_s = today.strftime("%Y-%m-%d")
    eb = _eb_filter_sql("b")
    conn = get_conn()
    cur = conn.cursor()

    # 价格列表（存量、非EB、有现价）
    rows = cur.execute(
        "SELECT b.current_price FROM bonds b WHERE COALESCE(b.is_delisted,0)=0 AND %s "
        "AND b.current_price IS NOT NULL AND b.current_price > 0" % eb
    ).fetchall()
    prices = [r[0] for r in rows]
    n = len(prices)
    median_price = _st.median(prices) if n > 0 else None
    avg_price = (sum(prices) / n) if n > 0 else None

    # 存量总数（非EB）
    active = cur.execute(
        "SELECT COUNT(*) FROM bonds b WHERE COALESCE(b.is_delisted,0)=0 AND %s" % eb
    ).fetchone()[0]

    # 新发数（最近180天上市，滚动）
    new_cnt = cur.execute(
        "SELECT COUNT(*) FROM bonds b WHERE COALESCE(b.is_delisted,0)=0 AND %s "
        "AND b.listing_date IS NOT NULL AND b.listing_date >= ? AND b.listing_date <= ?"
        % eb, (cutoff, today_s)
    ).fetchone()[0]

    # 退市比例：基数=当年1月1日仍为存量的债；当年退市数=今年退市的
    base = cur.execute(
        "SELECT COUNT(*) FROM bonds b WHERE %s AND "
        "(COALESCE(b.is_delisted,0)=0 OR (b.is_delisted=1 AND b.delist_date >= ?))"
        % eb, (jan1,)
    ).fetchone()[0]
    delisted_this_year = cur.execute(
        "SELECT COUNT(*) FROM bonds b WHERE %s AND b.is_delisted=1 "
        "AND b.delist_date >= ? AND b.delist_date <= ?"
        % eb, (jan1, today_s)
    ).fetchone()[0]
    delist_ratio = (delisted_this_year / base) if base else None

    conn.close()
    return {
        "median_price": median_price,
        "avg_price": avg_price,
        "price_sample": n,
        "active_count": active,
        "new_count": new_cnt,
        "new_window_days": 180,
        "delist_ratio": delist_ratio,
        "delist_base": base,
        "delist_count_year": delisted_this_year,
        "year": year,
        "as_of": today_s,
    }


def get_price_trend(days=365, min_sample=50):
    """返回可转债价格中位数 / 均价的历史走势（供首页走势图）。
    数据源：daily_close 按交易日聚合（JOIN bonds 过滤未退市+非EB），
    仅保留当日样本数 >= min_sample 的可信交易日；最后追加一个【实时点】
    （用 compute_market_overview 的当前中位数/均价），使曲线延伸到今天且与卡片一致。
    返回 {dates:[...], median:[...], avg:[...]}。"""
    import datetime as _dt, statistics as _st
    from collections import defaultdict
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    eb = _eb_filter_sql("b")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT d.trade_date, d.bond_close FROM daily_close d "
        "JOIN bonds b ON b.bond_code = d.bond_code "
        "WHERE d.bond_close IS NOT NULL AND d.trade_date >= ? "
        "AND COALESCE(b.is_delisted,0)=0 AND %s ORDER BY d.trade_date" % eb,
        (cutoff,)
    )
    by_day = defaultdict(list)
    for trade_date, price in cur.fetchall():
        by_day[trade_date].append(price)
    conn.close()

    dates, median_series, avg_series = [], [], []
    for trade_date in sorted(by_day.keys()):
        plist = by_day[trade_date]
        if len(plist) < min_sample:
            continue
        plist_sorted = sorted(plist)
        dates.append(trade_date)
        median_series.append(round(_st.median(plist_sorted), 2))
        avg_series.append(round(sum(plist_sorted) / len(plist_sorted), 2))

    # 实时收尾点
    ov = compute_market_overview()
    if ov["median_price"] is not None:
        dates.append(ov["as_of"])
        median_series.append(round(ov["median_price"], 2))
        avg_series.append(round(ov["avg_price"], 2) if ov["avg_price"] is not None else None)
    return {"dates": dates, "median": median_series, "avg": avg_series}


# ============ 可转债等权指数（价格等权，全样本日频） ============

def compute_equal_weight_index(min_sample=50, base_value=1000.0):
    """计算可转债等权指数并写库 equal_weight_index（幂等，可每日重算全量）。

    口径（与 get_price_trend 一致，对标集思录等权指数）：
      - 数据源：daily_close JOIN bonds，过滤 bond_close 非空 + 未退市 + 非EB；
      - 每个交易日算算术平均价 avg_price 与中位数 median_price，样本数 sample_n；
      - 基期 BASE_AVG = 全历史中【最早一个 sample_n>=min_sample 的交易日】的 avg_price；
      - 指数值 index_value = avg_price / BASE_AVG * base_value（首期 = 1000）。

    说明：本地 daily_close 自 2026-08 起才有数据，无法回溯到集思录的 2017-12-29 基期，
    因此采用「数据起点基期 = 1000」的自洽口径——走势形状与集思录一致，
    但绝对点位因基期不同而不可直接比较。返回最新一行 dict 或 None。
    """
    import statistics as _st
    from collections import defaultdict
    eb = _eb_filter_sql("b")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT d.trade_date, d.bond_close FROM daily_close d "
        "JOIN bonds b ON b.bond_code = d.bond_code "
        "WHERE d.bond_close IS NOT NULL "
        "AND COALESCE(b.is_delisted,0)=0 AND %s ORDER BY d.trade_date" % eb
    )
    by_day = defaultdict(list)
    for td, p in cur.fetchall():
        by_day[td].append(p)
    # 先汇总各达标交易日的均价/中位数，确定基期
    day_stat = {}
    for td, plist in by_day.items():
        if len(plist) < min_sample:
            continue
        day_stat[td] = (sum(plist) / len(plist), _st.median(plist), len(plist))
    if not day_stat:
        conn.close()
        return None
    base_date = min(day_stat.keys())
    base_avg = day_stat[base_date][0]
    now = _now_str()
    import datetime as _dtmod
    _latest_td = max(day_stat.keys()) if day_stat else None
    _today = _dtmod.date.today().strftime('%Y-%m-%d')
    _now_hm = _dtmod.datetime.now().strftime('%H:%M')
    # 盘中（<16:00）手动采集到的「今日」为实时价、样本常不完整，不计入指数，
    # 待收盘后(>=16:00)每日任务重算再写入；避免盘中快照污染涨跌幅。
    _is_intraday = (_latest_td == _today and _now_hm < '16:00')
    # 防御：最新交易日若样本数显著低于近期均值，视为「盘中/不完整采集」，
    # 跳过写入当日（不 REPLACE），保留上一完整交易日为指数最新日，避免半截
    # 数据污染涨跌幅；补齐后再次重算即可正常写入。
    _recent = [v[2] for k, v in day_stat.items() if k != _latest_td]
    _recent_avg = (sum(_recent) / len(_recent)) if _recent else 0.0
    for td in sorted(day_stat.keys()):
        avg_p, med_p, n = day_stat[td]
        if td == _latest_td and (_is_intraday or (_recent_avg and (n < 200 or n < _recent_avg * 0.7))):
            continue
        idx_val = round(avg_p / base_avg * base_value, 2) if base_avg else None
        cur.execute(
            "INSERT OR REPLACE INTO equal_weight_index"
            "(trade_date, avg_price, median_price, index_value, sample_n, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (td, round(avg_p, 2), round(med_p, 2), idx_val, n, now)
        )
    conn.commit()
    conn.close()
    return get_equal_weight_latest()


def get_equal_weight_latest():
    """返回最新交易日等权指数行 + 较前一交易日的涨跌/涨跌幅。
    返回 dict(trade_date, avg_price, median_price, index_value, sample_n, chg, chg_pct) 或 None。"""
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT trade_date, avg_price, median_price, index_value, sample_n "
        "FROM equal_weight_index ORDER BY trade_date DESC LIMIT 1"
    ).fetchone()
    if not row:
        conn.close()
        return None
    prev = cur.execute(
        "SELECT index_value FROM equal_weight_index "
        "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", (row["trade_date"],)
    ).fetchone()
    conn.close()
    latest = dict(row)
    prev_val = prev[0] if prev else None
    if prev_val not in (None, 0):
        latest["chg"] = round(latest["index_value"] - prev_val, 2)
        latest["chg_pct"] = round((latest["index_value"] / prev_val - 1) * 100, 2)
    else:
        latest["chg"] = None
        latest["chg_pct"] = None
    return latest


def get_equal_weight_trend(days=365):
    """返回等权指数近 days 天的走势序列，供首页走势图。
    返回 {dates:[...], index:[...], avg:[...]}。"""
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT trade_date, index_value, avg_price FROM equal_weight_index "
        "WHERE trade_date >= ? ORDER BY trade_date", (cutoff,)
    ).fetchall()
    conn.close()
    return {
        "dates": [r["trade_date"] for r in rows],
        "index": [r["index_value"] for r in rows],
        "avg": [r["avg_price"] for r in rows],
    }


def sync_bonds_list(bond_list):
    """将 fetch_all_bonds() 返回的全量列表与 bonds 表同步：找出库中没有的转债，
    upsert 基本信息（含 listing_date），返回新录入的转债数。
    用于每日任务自动发现并录入新上市转债，支撑「新发转债数」滚动统计。
    对新发现的转债 best-effort 用腾讯行情补一个现价（失败留空，由后续采集补齐）。"""
    if not bond_list:
        return 0
    import crawler as _crawler
    conn = get_conn()
    cur = conn.cursor()
    existing = set(r[0] for r in cur.execute("SELECT bond_code FROM bonds").fetchall())
    new_cnt = 0
    for b in bond_list:
        code = b.get("bond_code")
        if not code or code in existing:
            continue
        price = None
        try:
            price = _crawler._fetch_price(code)
        except Exception:
            price = None
        upsert_bond({
            "bond_code": code,
            "bond_name": b.get("bond_name"),
            "stock_code": b.get("stock_code"),
            "stock_name": b.get("stock_name"),
            "listing_date": b.get("listing_date"),
            "is_delisted": 0,
            "current_price": price,
            "data_source": "东方财富数据中心",
        })
        existing.add(code)
        new_cnt += 1
    conn.close()
    return new_cnt


def get_new_bonds(days=180):
    """返回最近 `days` 天内上市(listing_date)的可转债列表（存量、非EB），供 /new-bonds 页面。
    含基本信息：代码、名称、正股、评级、发行规模、上市日期、现价、转股价、到期日、剩余年限。"""
    import datetime as _dt
    cutoff = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    today = _dt.date.today().strftime("%Y-%m-%d")
    eb = _eb_filter_sql("b")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT b.bond_code, b.bond_name, b.stock_code, b.stock_name, b.rating, "
        "b.issue_scale, b.listing_date, b.current_price, b.current_transfer_price, "
        "b.expire_date FROM bonds b "
        "WHERE COALESCE(b.is_delisted,0)=0 AND %s "
        "AND b.listing_date IS NOT NULL AND b.listing_date >= ? AND b.listing_date <= ? "
        "ORDER BY b.listing_date DESC" % eb, (cutoff, today)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    for r in rows:
        exp = r.get("expire_date")
        try:
            r["remain_years"] = round(
                (_dt.date.fromisoformat(exp) - _dt.date.today()).days / 365.0, 2) if exp else None
        except Exception:
            r["remain_years"] = None
    return rows
