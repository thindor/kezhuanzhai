"""本地 SQLite 存储层：转债基础信息 + 各报告期十大持有人。"""
import sqlite3
from config import DB_PATH


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    for col, ctype in [("is_delisted", "INTEGER"), ("delist_date", "TEXT")]:
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
    # 可转债公告（强赎/不强赎/下修/提议下修/临近强赎/临近下修/即将发行等）
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
        UNIQUE(bond_code, announce_type)
    )""")
    conn.commit()
    conn.close()
    # 启动即按到期日/摘牌日幂等回填退市标记（覆盖存量债券）
    try:
        backfill_delist_status()
    except Exception:
        pass


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
    cur.execute(
        "SELECT b.*, COALESCE(h.holder_count,0) AS holder_count, h.latest_period "
        + base + where_sql + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size])
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
        a["top"].append({"bond_name": v["bond_name"] or v["bond_code"], "mv_wan": mv})

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
        a["top"].append({"bond_name": v["bond_name"] or v["bond_code"], "mv_wan": mv})

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
    保证「临近强赎/临近下修」等状态每天重算时只更新不堆积。"""
    now = _now_str()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO announcements (bond_code, bond_name, announce_type, title,
                               announce_date, source, official_url, updated_at)
    VALUES (:bond_code, :bond_name, :announce_type, :title,
            :announce_date, :source, :official_url, :updated_at)
    ON CONFLICT(bond_code, announce_type) DO UPDATE SET
        bond_name=excluded.bond_name,
        title=excluded.title,
        announce_date=excluded.announce_date,
        source=excluded.source,
        official_url=excluded.official_url,
        updated_at=excluded.updated_at
    """, {
        "bond_code": a.get("bond_code"),
        "bond_name": a.get("bond_name"),
        "announce_type": a.get("announce_type"),
        "title": a.get("title"),
        "announce_date": a.get("announce_date"),
        "source": a.get("source", "东财"),
        "official_url": a.get("official_url"),
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
