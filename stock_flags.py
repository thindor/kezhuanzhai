# -*- coding: utf-8 -*-
"""正股标签检测（每日）：银行股 / ST 风险警示 / 退市整理期。

数据源：腾讯实时行情 qt.gtimg.cn，取**正股**名称字段 [1]。
A 股的 ST / *ST / 退市整理期标记直接体现在股票简称上，是权威且实时的口径：
    ST中装、*ST闻泰、*ST三房、*ST航图、xxx退（退市整理期）

为什么要每日检测
----------------
1. 双低策略按「价格 + 转股溢价率」排序，天然偏好低价债。而正股被 ST 后，
   转债价格的下跌往往是**信用风险定价**而非低估——「便宜」是陷阱不是机会。
2. 银行转债（AAA、规模大、波动小）双低值长期偏低，会稳定占用双低榜名额，
   但正股普遍破净导致转股价下不去、溢价率高、弹性极差，很难触发强赎，
   与「打强赎为主」的收益逻辑相悖。
3. 被 ST 是**事件**，发生时间不可预测，必须每天扫一遍才能「及时」发现。

设计约束
--------
1. 行情对某只正股无返回时**不判定**（保持原状），避免网络抖动造成误标/误清。
2. 名称非空才回写 bonds.stock_name——空名称通常是股票已退市，覆盖会丢信息。
3. 摘帽（ST → 正常）会自动清除标记，双向可逆。

用法
----
    python stock_flags.py                  # 单独跑一次
    from stock_flags import refresh_stock_flags   # 供 collect_daily.py 编排调用
"""
import re
import urllib.request

BATCH = 50

# 腾讯行情：urllib 会读系统代理（本项目机器上是 127.0.0.1:7897 之类），
# 该端口未监听时直接 ConnectionRefused(10061)。必须显式绕过。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ST 前缀：ST / *ST / SST / S*ST（A 股风险警示的全部形态）
_ST_RE = re.compile(r"^S?\*?ST")


def _stock_symbol(stock_code):
    """正股代码 -> 腾讯行情符号。⚠️ 与转债规则不同：6/9/5=沪，0/2/3=深。"""
    c = str(stock_code).strip()
    return ("sh" + c) if c[:1] in ("6", "9", "5") else ("sz" + c)


def is_st_name(name):
    """股票简称是否为风险警示 / 退市整理期。"""
    if not name:
        return False
    n = name.strip()
    if _ST_RE.match(n):        # ST / *ST / SST / S*ST
        return True
    return n.endswith("退")     # 退市整理期，如「xxx退」


def is_bank_name(name):
    """股票简称是否为银行。A 股银行简称均含「银行」二字。"""
    return bool(name) and ("银行" in name)


def _fetch_names(syms):
    """批量拉腾讯行情，返回 {sym: 股票简称}。"""
    out = {}
    syms = list(syms)
    for i in range(0, len(syms), BATCH):
        batch = syms[i:i + BATCH]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            txt = _OPENER.open(req, timeout=20).read().decode("gbk", "ignore")
        except Exception as e:
            print("[stock_flags] 行情拉取失败(batch%d): %r" % (i, e))
            continue
        for line in txt.strip().split(";"):
            line = line.strip()
            if "=" not in line or '"' not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            body = line.split('"')[1]
            f = body.split("~")
            if len(f) < 4:
                continue
            out[sym] = f[1]
    return out


def refresh_stock_flags():
    """检测全市场正股标签并写回 bonds。

    返回 dict(checked, st_now, bank_now, new_st, cleared_st, skipped_reason)。
    """
    import db

    db.ensure_stock_flag_columns()
    bonds = db.get_bonds_for_suspend_check()   # 未退市 + 已上市 + 未到期
    if not bonds:
        return {"checked": 0, "st_now": 0, "bank_now": 0, "new_st": [],
                "cleared_st": [], "skipped_reason": "无待检测转债"}

    # 一只正股可能对应多只转债（如中装转债 / 中装转2），按正股去重抓行情
    sym_by_code = {}
    for b in bonds:
        sc = b["stock_code"]          # row_factory=sqlite3.Row，无 .get()，直接索引
        if sc:
            sym_by_code[b["bond_code"]] = _stock_symbol(sc)

    if not sym_by_code:
        return {"checked": 0, "st_now": 0, "bank_now": 0, "new_st": [],
                "cleared_st": [], "skipped_reason": "无有效正股代码"}

    names = _fetch_names(set(sym_by_code.values()))
    if not names:
        return {"checked": 0, "st_now": 0, "bank_now": 0, "new_st": [],
                "cleared_st": [], "skipped_reason": "行情源无返回，跳过检测"}

    conn = db.get_conn()
    cur = conn.cursor()
    now = db._now_str()
    new_st, cleared_st = [], []
    st_now = bank_now = checked = 0

    for b in bonds:
        code = b["bond_code"]
        sym = sym_by_code.get(code)
        if not sym:
            continue
        name = names.get(sym)
        if not name:
            continue  # 行情缺失不判定（防网络抖动误标）

        checked += 1
        is_st = 1 if is_st_name(name) else 0
        is_bank = 1 if is_bank_name(name) else 0
        row = cur.execute(
            "SELECT COALESCE(stock_is_st,0) FROM bonds WHERE bond_code=?", (code,)).fetchone()
        prev_st = row[0] if row else 0

        if is_st:
            st_now += 1
            if not prev_st:
                new_st.append(code)
        elif prev_st:
            cleared_st.append(code)   # 摘帽

        if is_bank:
            bank_now += 1

        # 名称非空才回写，避免股票退市后行情返回空名把 stock_name 洗掉
        cur.execute(
            "UPDATE bonds SET stock_is_st=?, stock_is_bank=?, stock_name=?, "
            "stock_flags_checked_at=? WHERE bond_code=?",
            (is_st, is_bank, name, now, code))

    conn.commit()
    conn.close()
    return {"checked": checked, "st_now": st_now, "bank_now": bank_now,
            "new_st": new_st, "cleared_st": cleared_st, "skipped_reason": None}


def _summary(r):
    if r.get("skipped_reason"):
        return "跳过：%s" % r["skipped_reason"]
    return "检测 %d 只，ST/退市整理 %d 只（新标记 %s，摘帽 %s），银行正股 %d 只" % (
        r["checked"], r["st_now"],
        ",".join(r["new_st"]) or "0 只",
        ",".join(r["cleared_st"]) or "0 只",
        r["bank_now"])


if __name__ == "__main__":
    import db
    db.init_db()
    r = refresh_stock_flags()
    print("[stock_flags] " + _summary(r))
    st = db.get_st_bond_codes()
    bank = db.get_bank_bond_codes()
    print("  库内 ST 债: %s" % (sorted(st) or "无"))
    print("  库内银行债: %s" % (sorted(bank) or "无"))
