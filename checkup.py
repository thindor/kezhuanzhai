"""转债体检卡：实时数据分析与核心要素提炼。

数据来源（均为直连、已验证稳定）：
  - 东方财富 RPT_BOND_CB_LIST（全量，无30限制）：票面利率说明、到期赎回条款、
    转股价、评级、到期日、发行规模。据此自算 YTM(到期收益率) 与纯债价值(债底估算)。
  - 集思录 cb_list_new（前30活跃债）：提供精确的债底/YTM/PB/强赎/回售触发价等，
    作为增强（命中即用，未命中不影响主流程）。
  - 腾讯行情 qt.gtimg.cn：转债+正股实时现价与涨跌幅（秒级），供「刷新实时行情」。
  - 本地 SQLite：转债基础信息、十大持有人（机构/牛散结构、第一大持有人）。
  - 复用 crawler 的强赎预警 / 下修提醒计算。

核心结论（抓牛鼻子）按四步生成：
  ① 定基调   —— 价格分层（债性/双低/平衡/股性）
  ② 定安全垫 —— 债底(估算) + YTM（不违约前提下）
  ③ 定弹性   —— 转股溢价率（越低越跟涨）
  ④ 定天花板 —— 距强赎触发价空间 + 下修潜力(PB/下修进度) + 持有人促转股动力
"""
import time
import threading
import re
import requests
from datetime import datetime, date

from db import (get_bond, get_periods_info, get_holders, get_down_revise,
               get_bond_announcements, get_conn)
import crawler

EM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
EM_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_HDR = {"User-Agent": EM_UA,
          "Referer": "https://data.eastmoney.com/",
          "Accept": "application/json, text/plain, */*"}

# 评级 -> 近似信用贴现率（用于纯债价值估算，取市场大致到期收益率水平）
RATING_YIELD = {
    "AAA": 0.026, "AAA-": 0.028, "AA+": 0.030, "AA": 0.038, "AA-": 0.045,
    "A+": 0.060, "A": 0.070, "A-": 0.080, "BBB+": 0.100, "BBB": 0.110,
}

# ---- 集思录实时（前30活跃债，作为精确增强）----
_JSL = {"ts": 0, "data": {}, "lock": threading.Lock()}
_JSL_TTL = 60

# ---- 东财全量基础（主源：票面/赎回/转股价/评级/到期/余额）----
_EM = {"ts": 0, "data": {}, "lock": threading.Lock()}
_EM_TTL = 600


EM_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,CONVERT_STOCK_CODE,RATING,"
            "INTEREST_RATE_EXPLAIN,REDEEM_CLAUSE,COUPON_IR,TRANSFER_VALUE,"
            "INITIAL_TRANSFER_PRICE,EXPIRE_DATE,ACTUAL_ISSUE_SCALE")


def _em_parse_row(row):
    """把东财一行解析成体检卡需要的字段 dict。"""
    code = row.get("SECURITY_CODE")
    if not code:
        return None
    rates = parse_coupon_rates(row.get("INTEREST_RATE_EXPLAIN"))
    last = rates[-1] if rates else 0.0
    redeem = parse_redeem_price(row.get("REDEEM_CLAUSE"), last)
    tp = row.get("TRANSFER_VALUE") or row.get("INITIAL_TRANSFER_PRICE")
    try:
        tp = float(tp) if tp else None
    except (ValueError, TypeError):
        tp = None
    yl = years_left(row.get("EXPIRE_DATE"))
    try:
        scale = float(row.get("ACTUAL_ISSUE_SCALE")) if row.get("ACTUAL_ISSUE_SCALE") else None
    except (ValueError, TypeError):
        scale = None
    return {
        "bond_name": row.get("SECURITY_NAME_ABBR"),
        "stock_code": row.get("CONVERT_STOCK_CODE"),
        "rating": row.get("RATING"),
        "coupon_rates": rates,
        "redeem_price": redeem,
        "transfer_price": tp,
        "years_left": yl,
        "issue_scale": scale,
    }


def _em_fetch_raw(params):
    """东财单页请求，返回 list[dict] 或 None（异常）。"""
    try:
        r = requests.get(EM_BASE, params=params, headers=EM_HDR, timeout=20)
        d = r.json()
        if not (d.get("success") and d.get("result") and d["result"].get("data")):
            return None
        return d["result"]["data"]
    except Exception:
        return None


def fetch_em_cb_basics(force=False):
    """批量拉东财全市场可转债基础，解析票面利率/赎回价/转股价/评级/到期/余额。

    东财该接口单页最多 500 条且忽略 p/page 等分页参数（每次都返回排序后的前 500）。
    故用「升序一页 + 降序一页」双排序覆盖约 1000 只；剩余中段约 50 只（123023~123072）
    由 get_checkup 在按需时通过 filter 单只补取，保证全量覆盖。
    返回 {bond_code: {...}}。内存缓存 10min。任意异常降级返回旧缓存。"""
    global _EM
    now = time.time()
    with _EM["lock"]:
        if not force and _EM["data"] and (now - _EM["ts"]) < _EM_TTL:
            return _EM["data"]
    out = {}
    try:
        base = {"reportName": "RPT_BOND_CB_LIST", "columns": EM_COLS,
                "pageSize": 500, "source": "WEB", "client": "WEB", "p": 1}
        # 升序（默认）：110002...123022
        for row in (_em_fetch_raw(base) or []):
            parsed = _em_parse_row(row)
            if parsed:
                out[row.get("SECURITY_CODE")] = parsed
        # 降序：404005...123073（覆盖高代码段，含 123xxx 高位）
        desc = dict(base)
        desc["sortColumns"] = "SECURITY_CODE"
        desc["sortTypes"] = "-1"
        for row in (_em_fetch_raw(desc) or []):
            parsed = _em_parse_row(row)
            if parsed:
                out[row.get("SECURITY_CODE")] = parsed
    except Exception:
        if _EM["data"]:
            return _EM["data"]
        raise
    with _EM["lock"]:
        _EM["data"] = out
        _EM["ts"] = time.time()
    return out


def fetch_em_one(code):
    """东财按代码单只补取（覆盖双排序遗漏的中段债），返回解析 dict 或 None。"""
    params = {"reportName": "RPT_BOND_CB_LIST", "columns": EM_COLS,
              "pageSize": 10, "source": "WEB", "client": "WEB", "p": 1,
              "filter": '(SECURITY_CODE="%s")' % code}
    rows = _em_fetch_raw(params) or []
    for row in rows:
        if row.get("SECURITY_CODE") == code:
            return _em_parse_row(row)
    return None


def parse_coupon_rates(text):
    """从『第一年0.20%、第二年为0.40%...』解析各年票息(百分比数值)，返回 list[float]。"""
    if not text:
        return None
    rates = [float(m) for m in re.findall(r"第[一二三四五六七八九十\d]+\s*年[^%]{0,4}?([\d.]+)\s*%", text)]
    return rates or None


def parse_redeem_price(text, last_coupon):
    """从赎回条款解析到期赎回价(元)。优先『面值的107%』，否则 100+末年票息。"""
    if not text:
        return None
    m = re.search(r"面值[的]*\s*(\d+(?:\.\d+)?)\s*%", text)
    if m:
        return float(m.group(1))
    if last_coupon:
        return 100.0 + last_coupon
    return None


def years_left(expire_str):
    """到期日 -> 剩余年限(年，小数)。已过期返回 0。"""
    if not expire_str:
        return None
    try:
        ed = datetime.strptime(str(expire_str)[:10], "%Y-%m-%d").date()
        return max(0.0, (ed - date.today()).days / 365.0)
    except (ValueError, TypeError):
        return None


def compute_ytm(price, coupon_rates, redeem_price, yleft):
    """到期收益率(%)：已知现价/各年票息(百分比即元)/赎回价/剩余年限，二分法求解。
    票息按年付息近似（未对齐已过的付息期，量级准确，用于体检卡参考）。"""
    if price is None or not coupon_rates or redeem_price is None or yleft is None or yleft <= 0:
        return None
    k = max(1, int(round(yleft)))
    cf = list(coupon_rates)
    if len(cf) > k:
        cf = cf[:k]
    elif len(cf) < k:
        cf = cf + [cf[-1]] * (k - len(cf))

    def pv(y):
        s = 0.0
        for i in range(k - 1):
            s += cf[i] / (1 + y) ** (i + 1)
        s += (cf[-1] + redeem_price) / (1 + y) ** k
        return s

    lo, hi = -0.5, 0.5
    if pv(lo) < price:  # 极高收益（价格极低），直接返回下界
        return lo * 100
    for _ in range(100):
        mid = (lo + hi) / 2
        if pv(mid) > price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


def compute_pure_value(coupon_rates, redeem_price, yleft, disc):
    """纯债价值(债底)估算：各期票息+赎回价按评级近似信用收益率贴现。"""
    if not coupon_rates or redeem_price is None or yleft is None:
        return None
    k = max(1, int(round(yleft)))
    cf = list(coupon_rates)
    if len(cf) > k:
        cf = cf[:k]
    elif len(cf) < k:
        cf = cf + [cf[-1]] * (k - len(cf))
    s = 0.0
    for i in range(k - 1):
        s += cf[i] / (1 + disc) ** (i + 1)
    s += (cf[-1] + redeem_price) / (1 + disc) ** k
    return s


def fetch_jsl_cb_list(force=False):
    """集思录前30活跃债实时列表，返回 {bond_id: cell}。内存缓存 60s。
    匿名仅返回前30，作为『精确增强』使用；获取失败静默降级。"""
    global _JSL
    now = time.time()
    with _JSL["lock"]:
        if not force and _JSL["data"] and (now - _JSL["ts"]) < _JSL_TTL:
            return _JSL["data"]
    out = {}
    try:
        t = int(now * 1000)
        url = ("https://www.jisilu.cn/data/cbnew/cb_list_new/"
               "?___jsl=LST___t=%d&page=1&rows=30" % t)
        r = requests.get(url, headers={"User-Agent": EM_UA,
                                        "Referer": "https://www.jisilu.cn/data/cbnew/"},
                         timeout=20)
        rows = r.json().get("rows") or []
        for ro in rows:
            c = ro.get("cell", {})
            bid = c.get("bond_id")
            if bid:
                out[bid] = c
    except Exception:
        if _JSL["data"]:
            return _JSL["data"]
        return {}
    with _JSL["lock"]:
        _JSL["data"] = out
        _JSL["ts"] = time.time()
    return out


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def refresh_remaining_scales():
    """集思录前30活跃债 curr_iss_amt(剩余规模,亿) -> bonds.remaining_scale。

    东财 RPT_BOND_CB_LIST 无剩余规模字段；集思录匿名仅返回前30活跃债，故逐日
    滚动补全（债券进入活跃榜即写入）。返回写入条数；异常时静默返回 0。"""
    try:
        jsl = fetch_jsl_cb_list(force=True)
    except Exception:
        return 0
    if not jsl:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    for bid, c in jsl.items():
        rs = _f(c.get("curr_iss_amt"))
        if rs is not None:
            cur.execute("UPDATE bonds SET remaining_scale=? WHERE bond_code=?", (rs, bid))
            n += 1
    conn.commit()
    conn.close()
    return n


def _qx(code):
    """通用行情前缀：沪市(6/9/11/5/7)->sh，深市(0/2/3/12)->sz。"""
    c = (code or "").strip()
    if c.startswith(("6", "9", "11", "5", "7")):
        return "sh" + c
    return "sz" + c


def fetch_realtime_quotes(bond_code, stock_code=None):
    """腾讯行情实时：返回 {原始6位代码: {price, prev_close, pct}}。失败返回 {}。"""
    codes = []
    if bond_code:
        codes.append(_qx(bond_code))
    if stock_code:
        codes.append(_qx(stock_code))
    if not codes:
        return {}
    try:
        r = requests.get("https://qt.gtimg.cn/q=" + ",".join(codes),
                         headers={"User-Agent": EM_UA}, timeout=8)
        raw = r.content.decode("gbk", "ignore")
    except Exception:
        return {}
    res = {}
    for seg in raw.split(";"):
        seg = seg.strip()
        if "=" not in seg:
            continue
        name, val = seg.split("=", 1)
        val = val.strip().strip('"')
        if not val:
            continue
        f = val.split("~")
        code_part = name.replace("v_", "").strip()
        orig = code_part[2:] if code_part[:2] in ("sh", "sz") else code_part
        price = _f(f[3]) if len(f) > 3 else None
        prev = _f(f[4]) if len(f) > 4 else None
        pct = None
        if price is not None and prev:
            pct = (price - prev) / prev * 100.0
        res[orig] = {"price": price, "prev_close": prev, "pct": pct}
    return res


def _q(quotes, code):
    return quotes.get(code) or {}


def _eff_transfer_price(code, bond):
    """有效转股价：集思录最新下修后价优先，否则 bonds.current_transfer_price / 东财。"""
    try:
        cnt, recs, _ = get_down_revise(code)
        if cnt and recs:
            p = recs[0].get("price_after")
            if p is not None:
                return _f(p)
    except Exception:
        pass
    return _f(bond.get("current_transfer_price"))


def _price_tier(price):
    if price is None:
        return ("未知", "暂无实时价格")
    if price < 100:
        return ("债性主导", "价格低于面值，债底保护强、弹性弱，主要赌不违约+下修")
    if price < 110:
        return ("双低区", "债底厚+溢价低，攻守兼备，经典双低策略区")
    if price < 130:
        return ("平衡区", "股债属性均衡，看正股与条款博弈")
    return ("股性主导", "价格>130，基本等同正股替代品，关注强赎止盈")


def _holder_summary(code):
    """最新期十大持有人结构：机构/牛散数、第一大持有人、机构占比、是否控股股东。"""
    periods = get_periods_info(code)
    if not periods:
        return None
    holders = get_holders(code, periods[0]["period"])
    if not holders:
        return None
    n = len(holders)
    nat = sum(1 for h in holders if h.get("is_natural"))
    org = n - nat
    top = holders[0]
    top_name = top.get("holder_name")
    top_natural = bool(top.get("is_natural"))
    ctrl_kw = ("集团", "控股", "股东", "国资", "投资", "有限公司", "股份")
    is_controller = (not top_natural) and bool(top_name) and any(k in top_name for k in ctrl_kw)
    org_ratio = sum((h.get("hold_ratio") or 0) for h in holders if not h.get("is_natural"))
    return {
        "period": periods[0]["period"],
        "total": n,
        "natural": nat,
        "institution": org,
        "top_holder": top_name,
        "top_is_natural": top_natural,
        "is_controller": is_controller,
        "institution_ratio": round(org_ratio, 2),
    }


def get_checkup(code):
    """组装转债体检卡数据（东财自算 + 集思录增强 + 腾讯实时价 + db 持有人 + 预警）。"""
    bond = get_bond(code)
    if not bond:
        return None

    # 主源：东财全量基础（票面/赎回/转股价/评级/到期/余额）
    # 双排序批量已覆盖约 1000 只；若代码落在中段遗漏区，按需单只补取。
    em = {}
    try:
        em = fetch_em_cb_basics().get(code)
        if not em:
            em = fetch_em_one(code)
    except Exception:
        em = {}
    em = em or {}
    # 增强：集思录前30（精确债底/YTM/PB/触发价）
    jsl = None
    try:
        jsl = fetch_jsl_cb_list().get(code)
    except Exception:
        jsl = None

    # 有效转股价：集思录下修后优先 -> 东财 -> db
    tp = _eff_transfer_price(code, bond)
    if tp is None and em.get("transfer_price"):
        tp = em["transfer_price"]
    if tp is None:
        tp = _f(bond.get("current_transfer_price"))

    # 腾讯实时价
    quotes = fetch_realtime_quotes(code, bond.get("stock_code"))
    qb = _q(quotes, code)
    qs = _q(quotes, bond.get("stock_code")) if bond.get("stock_code") else {}
    bp = qb.get("price")
    sp = qs.get("price")
    if bp is None:
        bp = _f(jsl.get("price")) if jsl else None
    if bp is None:
        bp = _f(bond.get("current_price"))
    if sp is None:
        sp = _f(jsl.get("stock_price")) if jsl and jsl.get("stock_price") else None

    # 股性实时（腾讯正股价 + 转股价）
    conv_value = None
    premium = None
    if tp and sp and bp:
        conv_value = 100.0 / tp * sp
        if conv_value > 0:
            premium = (bp / conv_value - 1.0) * 100.0

    # 债性：东财自算（全量）为主，集思录精确值覆盖
    ytm = None
    pure_value = None
    pure_value_est = False
    if em.get("coupon_rates") and em.get("redeem_price") is not None and em.get("years_left") and bp:
        ytm = compute_ytm(bp, em["coupon_rates"], em["redeem_price"], em["years_left"])
        disc = RATING_YIELD.get((em.get("rating") or "").upper()) or 0.04
        pure_value = compute_pure_value(em["coupon_rates"], em["redeem_price"],
                                        em["years_left"], disc)
        pure_value_est = True
    if jsl:
        if _f(jsl.get("ytm_rt")) is not None:
            ytm = _f(jsl.get("ytm_rt"))
        if _f(jsl.get("pure_value")) is not None:
            pure_value = _f(jsl.get("pure_value"))
            pure_value_est = False

    rating = (em.get("rating") or (jsl.get("rating_cd") if jsl else None) or bond.get("rating"))
    # 强赎/回售触发价：集思录优先，否则转股价×1.3 / ×0.7（市场通用）
    force_price = _f(jsl.get("force_redeem_price")) if jsl else None
    if force_price is None and tp:
        force_price = round(tp * 1.3, 2)
    put_price = _f(jsl.get("put_convert_price")) if jsl else None
    if put_price is None and tp:
        put_price = round(tp * 0.7, 2)
    pb = _f(jsl.get("pb")) if jsl else None
    year_left = em.get("years_left")
    if year_left is None and jsl:
        year_left = _f(jsl.get("year_left"))
    balance = em.get("issue_scale")
    if balance is None and jsl:
        balance = _f(jsl.get("curr_iss_amt"))
    redeem_price_val = em.get("redeem_price")

    # 分层
    tier_name, tier_desc = _price_tier(bp)

    # 距强赎触发价空间（正股需涨多少才触发，用正股价，单位统一为元/股）
    upside_to_redeem = None
    if force_price and sp:
        upside_to_redeem = (force_price / sp - 1.0) * 100.0

    # 已公告强赎：取该债 announce_type='强赎' 的最新一条公告
    redeem_ann = None
    try:
        rans = get_bond_announcements(code, "强赎")
        if rans:
            redeem_ann = rans[0]
    except Exception:
        redeem_ann = None

    # 预警（复用 crawler）；已公告强赎的转债不会再下修、强赎预警也无意义，跳过计算
    if redeem_ann:
        redeem_warn = None
        revise_warn = None
    else:
        redeem_warn = crawler.compute_redemption_warning(code)
        revise_warn = crawler.compute_down_revise_warning(code)

    # 持有人结构
    holder = _holder_summary(code)

    # 下修潜力
    down_potential = None
    if pb is not None:
        if pb <= 1:
            down_potential = "破净(PB≤1)，下修受净资产限制"
        else:
            down_potential = "PB=%.2f，下修有空间（不低于净资产）" % pb
    elif tp:
        down_potential = "转股价可下修空间需结合每股净资产（见公告条款）"

    # 安全垫
    safety = None
    if ytm is not None:
        if ytm >= 2:
            safety = "YTM≥2%，持有到期有正收益，安全垫厚"
        elif ytm >= 0:
            safety = "YTM 在 0~2%，略有保本空间"
        else:
            safety = "YTM<0，现价高于到期价值，无保本"
    elif pure_value is not None and bp is not None:
        if bp <= pure_value:
            safety = "现价≤债底估算，折价于纯债价值"
        else:
            safety = "现价高于债底估算约 %.1f%%" % ((bp / pure_value - 1) * 100)

    # 弹性
    elasticity = None
    if premium is not None:
        if premium < 10:
            elasticity = "溢价率<10%，股性强、紧跟正股"
        elif premium < 30:
            elasticity = "溢价率 10~30%，股性中等"
        else:
            elasticity = "溢价率>30%，偏债、弹性弱"

    return {
        "code": code,
        "bond_name": bond.get("bond_name") or em.get("bond_name") or code,
        "stock_name": bond.get("stock_name") or em.get("stock_code"),
        "stock_code": bond.get("stock_code") or em.get("stock_code"),
        "rating": rating,
        "is_delisted": bool(bond.get("is_delisted")),
        "expire_date": bond.get("expire_date"),
        # 实时价
        "bond_price": round(bp, 3) if bp else None,
        "bond_pct": round(qb.get("pct"), 2) if qb.get("pct") is not None else None,
        "stock_price": round(sp, 3) if sp else None,
        "stock_pct": round(qs.get("pct"), 2) if qs.get("pct") is not None else None,
        # 债性
        "pure_value": round(pure_value, 2) if pure_value is not None else None,
        "pure_value_est": pure_value_est,
        "ytm": round(ytm, 2) if ytm is not None else None,
        "redeem_price": round(redeem_price_val, 2) if redeem_price_val else None,
        "year_left": round(year_left, 2) if year_left is not None else None,
        "balance": round(balance, 2) if balance is not None else None,
        "pb": round(pb, 2) if pb is not None else None,
        "safety": safety,
        # 股性
        "transfer_price": round(tp, 3) if tp else None,
        "convert_value": round(conv_value, 2) if conv_value else None,
        "premium": round(premium, 2) if premium is not None else None,
        "elasticity": elasticity,
        # 条款
        "force_redeem_price": round(force_price, 2) if force_price else None,
        "put_convert_price": round(put_price, 2) if put_price else None,
        "upside_to_redeem": round(upside_to_redeem, 2) if upside_to_redeem is not None else None,
        # 结论
        "tier_name": tier_name,
        "tier_desc": tier_desc,
        "down_potential": down_potential,
        "redeem_warn": redeem_warn,
        "revise_warn": revise_warn,
        "redeem_ann": redeem_ann,
        "holder": holder,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "东财(自算债底/YTM) + 集思录(增强) + 腾讯行情(实时)",
    }


def get_realtime(code):
    """仅刷新实时行情部分（腾讯），返回供前端局部无刷新更新。
    若纯债价值/到期赎回价在东财缓存中缺失（首载冷启动网络失败），顺带重试
    单只补取并回填缓存，供前端一并更新。"""
    bond = get_bond(code)
    if not bond:
        return None
    tp = _eff_transfer_price(code, bond)
    quotes = fetch_realtime_quotes(code, bond.get("stock_code"))
    qb = _q(quotes, code)
    qs = _q(quotes, bond.get("stock_code")) if bond.get("stock_code") else {}

    bp = qb.get("price")
    sp = qs.get("price")
    if bp is None:
        bp = _f(bond.get("current_price"))
    conv_value = None
    premium = None
    if tp and sp and bp:
        conv_value = 100.0 / tp * sp
        if conv_value > 0:
            premium = (bp / conv_value - 1.0) * 100.0

    # 静态字段补救：仅当 EM 缓存中该债的票面/赎回价缺失时才走一次东财单只补取，
    # 命中后写回缓存；常见情况（缓存已有）走纯 dict 查找，零网络。
    pure_value = None
    redeem_price = None
    need_recover = False
    try:
        cached = fetch_em_cb_basics().get(code)
    except Exception:
        cached = None
    if not cached or cached.get("coupon_rates") is None or cached.get("redeem_price") is None:
        need_recover = True
    if need_recover:
        try:
            fresh = fetch_em_one(code)
            if fresh:
                with _EM["lock"]:
                    _EM["data"][code] = fresh
                cached = fresh
        except Exception:
            pass
    if cached:
        cr = cached.get("coupon_rates")
        rp = cached.get("redeem_price")
        yl = cached.get("years_left")
        if cr and rp is not None and yl and bp:
            disc = RATING_YIELD.get((cached.get("rating") or "").upper()) or 0.04
            pv = compute_pure_value(cr, rp, yl, disc)
            if pv is not None:
                pure_value = round(pv, 2)
        if rp is not None:
            redeem_price = round(rp, 2)

    return {
        "bond_price": round(bp, 3) if bp else None,
        "bond_pct": round(qb.get("pct"), 2) if qb.get("pct") is not None else None,
        "stock_price": round(sp, 3) if sp else None,
        "stock_pct": round(qs.get("pct"), 2) if qs.get("pct") is not None else None,
        "convert_value": round(conv_value, 2) if conv_value else None,
        "premium": round(premium, 2) if premium is not None else None,
        "pure_value": pure_value,
        "redeem_price": redeem_price,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
