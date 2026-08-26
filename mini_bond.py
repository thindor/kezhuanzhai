"""小盘债（金陵式迷你弹性转债）筛选模块。

对标金陵转债画像的筛选标准：
  - 迷你盘：发行规模(issue_scale) < 10 亿（库内无剩余规模字段，以发行规模代理）
  - 愿下修：历史下修次数(down_revise_count) >= 1
  - 非ST / 未退市：stock_name 不含 ST 且 is_delisted=0
  - 价格低位可守 + 历史游资活性：用 current_price / mini_hist_max 展示

数据来源与存储：
  - 基础字段（发行规模/下修/评级/到期/现价）：bonds 表
  - 到期赎回价：bonds.redemption_price 列（东财 REDEEM_CLAUSE 解析回填）
  - 历史最高：bonds.mini_hist_max 列（新浪长周期日K，刷新时回填）
  - 标记/分档：由现价与历史最高实时推导

实时刷新 /api/xiaopanzhai/refresh 会并行重算：
  - 实时价 current_price（腾讯行情，checkup.get_realtime）
  - 到期赎回价 redemption_price（东财，同上返回）
  - 历史最高 mini_hist_max（新浪日K，crawler.fetch_sina_kline）
"""
import threading
import datetime

import crawler
import checkup
from db import get_conn


def ensure_columns():
    """幂等补齐小盘债模块需要的 bonds 列（赎回价 / 历史最高缓存）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(bonds)")
    cols = [r[1] for r in cur.fetchall()]
    for col, ctype in [("redemption_price", "REAL"),
                       ("mini_hist_max", "REAL"),
                       ("mini_hist_updated_at", "TEXT")]:
        if col not in cols:
            cur.execute("ALTER TABLE bonds ADD COLUMN %s %s" % (col, ctype))
    conn.commit()
    conn.close()


def backfill_redemption():
    """一次性回填全市场到期赎回价（东财 REDEEM_CLAUSE 解析）。返回更新条数。"""
    try:
        em = checkup.fetch_em_cb_basics()
    except Exception:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    for code, d in em.items():
        rp = d.get("redeem_price")
        if rp is not None:
            cur.execute("UPDATE bonds SET redemption_price=? WHERE bond_code=?", (rp, code))
            n += 1
    conn.commit()
    conn.close()
    return n


def get_candidates():
    """返回符合硬标准的候选（基础字段 + 赎回价 + 历史最高缓存）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT bond_code, bond_name, stock_name, rating, issue_scale,
               current_price, down_revise_count, expire_date,
               redemption_price, mini_hist_max, mini_hist_updated_at
        FROM bonds
        WHERE COALESCE(is_delisted,0)=0
          AND stock_name NOT LIKE '%ST%'
          AND issue_scale IS NOT NULL AND issue_scale<10
          AND COALESCE(down_revise_count,0)>=1
        ORDER BY issue_scale ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _hist_max(code):
    """新浪长周期日K历史最高收盘价。"""
    try:
        kl = crawler.fetch_sina_kline(code, 500)
    except Exception:
        return None
    closes = [x[1] for x in kl] if kl else []
    return max(closes) if closes else None


def refresh_all():
    """实时刷新：并行拉取每只候选的实时价/赎回价（腾讯+东财）与历史最高（新浪），写回 DB。

    返回本次刷新完成时间字符串。读取/写入均在主线程完成，子线程只做网络抓取，
    避免长时间持锁与多线程写库竞争。
    """
    cands = get_candidates()
    codes = [c["bond_code"] for c in cands]

    # 实时价 + 赎回价（checkup.get_realtime 内部用腾讯行情+东财增强）
    rt = {}
    def work(code):
        try:
            rt[code] = checkup.get_realtime(code)
        except Exception:
            rt[code] = None
    ts = [threading.Thread(target=work, args=(c,)) for c in codes]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    # 历史最高（新浪日K）
    hm = {}
    def hwork(code):
        hm[code] = _hist_max(code)
    hs = [threading.Thread(target=hwork, args=(c,)) for c in codes]
    for t in hs:
        t.start()
    for t in hs:
        t.join()

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    cur = conn.cursor()
    for code in codes:
        r = rt.get(code)
        bp = r.get("bond_price") if r else None
        rp = r.get("redeem_price") if r else None
        hv = hm.get(code)
        cur.execute(
            "UPDATE bonds SET current_price=?, redemption_price=?, "
            "mini_hist_max=?, mini_hist_updated_at=? WHERE bond_code=?",
            (bp, rp, hv, now if hv is not None else None, code))
    conn.commit()
    conn.close()
    return now


def _band(p):
    if p <= 120:
        return "低位守位"
    if p <= 130:
        return "中低位"
    if p <= 150:
        return "中高位"
    return "高位"


def get_rows(fill_missing=True):
    """返回展示行列表 + 最大更新时间。

    fill_missing=True 时，对缺少历史最高的候选并行补抓（首屏自愈，避免空列）；
    之后数据缓存于 DB，再次访问秒开。返回 (rows, max_updated_at)。
    """
    cands = get_candidates()
    need = [c["bond_code"] for c in cands if c["mini_hist_max"] is None]
    if fill_missing and need:
        hm = {}
        def hwork(code):
            hm[code] = _hist_max(code)
        hs = [threading.Thread(target=hwork, args=(c,)) for c in need]
        for t in hs:
            t.start()
        for t in hs:
            t.join()
        if hm:
            conn = get_conn()
            cur = conn.cursor()
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for code, v in hm.items():
                cur.execute(
                    "UPDATE bonds SET mini_hist_max=?, mini_hist_updated_at=? WHERE bond_code=?",
                    (v, now if v is not None else None, code))
            conn.commit()
            conn.close()
            cands = get_candidates()  # 重新读取带历史最高的数据

    rows = []
    max_updated = None
    for c in cands:
        price = c["current_price"] or 0
        hist_max = c["mini_hist_max"]
        redeem = c["redemption_price"]
        diff = round(redeem - price, 2) if (redeem is not None and price) else None
        fired = (hist_max is not None and hist_max >= 170)
        band = _band(price)
        if price <= 120 and (hist_max or 0) >= 150:
            tag = "star"
        elif price <= 120:
            tag = "low"
        else:
            tag = ""
        ua = c["mini_hist_updated_at"]
        if ua:
            max_updated = ua if (max_updated is None or ua > max_updated) else max_updated
        rows.append({
            "bond_code": c["bond_code"],
            "bond_name": c["bond_name"],
            "stock_name": c["stock_name"],
            "rating": c["rating"],
            "issue_scale": c["issue_scale"],
            "down_revise_count": c["down_revise_count"],
            "price": price,
            "hist_max": hist_max,
            "fired": fired,
            "band": band,
            "redeem_price": redeem,
            "diff": diff,
            "expire_date": c["expire_date"],
            "tag": tag,
            "updated_at": ua,
        })

    order = {"star": 0, "low": 1, "": 2}
    rows.sort(key=lambda r: (order[r["tag"]], r["price"]))
    return rows, max_updated
