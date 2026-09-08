# -*- coding: utf-8 -*-
"""可转债停牌检测（每日）。

数据源：腾讯实时行情 qt.gtimg.cn（本项目现价主源，GBK 编码，字段见下）。

判定口径
--------
停牌 = 当日「今开价」= 0 且「成交量」= 0。

实测依据（2026-09-07）：127033 中装转2 自 2025-09-03 起停牌，腾讯返回
    今开=0.000、成交量=0，而现价字段仍是停牌前的 89.726
—— 这正是「僵尸价」的来源：现价保留最后成交价，但已经没有成交。
daily_close 里停牌债 bond_close 为 NULL，而 db.get_latest_quotes() 取的是
「最近一条非空」记录，于是把一年前的旧价当成最新价喂给了双低等策略。

设计约束（踩过的坑）
--------------------
1. 必须每天检测**全部**未退市债，包括已判定停牌的债——否则一旦复牌也发现不了，
   会永远卡在停牌状态。所以 db.get_bonds_for_suspend_check() 刻意不过滤 is_suspended。
2. 非交易时段（清晨/休市/节假日）全市场成交量都是 0，若不加保护会把全市场误判为停牌。
   故用「有成交标的占比」做守卫，低于阈值则整轮跳过并说明原因。
3. 行情源对单只债无返回时**不判定**（保持原状），避免网络抖动造成误标。

用法
----
    python suspend.py            # 单独跑一次（检测 + 写库 + 打印摘要）
    from suspend import refresh_suspended_status   # 供 collect_daily.py 编排调用
"""
import urllib.request

BATCH = 50


def _market_of(code):
    """转债代码 -> 腾讯行情符号。11xxxx 为沪市转债，12xxxx 为深市转债。"""
    c = str(code).strip()
    return ("sh" + c) if c.startswith("11") else ("sz" + c)


def _fetch_quotes(syms):
    """批量拉腾讯行情，返回 {sym: dict(name, price, open, volume)}。

    腾讯字段（~ 分隔）：[1]名称 [2]代码 [3]现价 [4]昨收 [5]今开 [6]成交量(手)
    """
    out = {}
    syms = list(syms)
    for i in range(0, len(syms), BATCH):
        batch = syms[i:i + BATCH]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            txt = raw.decode("gbk", "ignore")
        except Exception as e:
            print("[suspend] 行情拉取失败(batch%d): %r" % (i, e))
            continue
        for line in txt.strip().split(";"):
            line = line.strip()
            if "=" not in line or '"' not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            body = line.split('"')[1]
            f = body.split("~")
            if len(f) < 7:
                continue
            try:
                out[sym] = {
                    "name": f[1],
                    "price": float(f[3] or 0),
                    "open": float(f[5] or 0),
                    "volume": float(f[6] or 0),
                }
            except (ValueError, IndexError):
                continue
    return out


def refresh_suspended_status(min_active_ratio=0.05):
    """检测全市场停牌状态并写回 bonds.is_suspended / suspended_since / suspend_checked_at。

    min_active_ratio：有成交标的占比低于此值则判定为非交易时段，整轮跳过（防误判）。

    返回 dict(checked, suspended_now, new_suspended, resumed, skipped_reason)。
    """
    import db

    db.ensure_suspend_columns()
    bonds = db.get_bonds_for_suspend_check()
    if not bonds:
        return {"checked": 0, "suspended_now": 0, "new_suspended": 0,
                "resumed": 0, "skipped_reason": "无待检测转债"}

    sym_by_code = {b["bond_code"]: _market_of(b["bond_code"]) for b in bonds}
    quotes = _fetch_quotes(set(sym_by_code.values()))

    if not quotes:
        return {"checked": 0, "suspended_now": 0, "new_suspended": 0,
                "resumed": 0, "skipped_reason": "行情源无返回，跳过检测"}

    # 非交易时段 / 行情源异常守卫
    active = [q for q in quotes.values() if q["volume"] > 0 or q["open"] > 0]
    ratio = len(active) / len(quotes)
    if ratio < min_active_ratio:
        return {"checked": 0, "suspended_now": 0, "new_suspended": 0, "resumed": 0,
                "skipped_reason": "非交易时段或行情源异常（有成交占比 %.1f%% < %.1f%%），跳过检测"
                                  % (ratio * 100, min_active_ratio * 100)}

    conn = db.get_conn()
    cur = conn.cursor()
    now = db._now_str()
    new_susp, resumed, susp_now, checked = [], [], 0, 0

    for b in bonds:
        code = b["bond_code"]
        q = quotes.get(sym_by_code[code])
        if not q:
            continue  # 行情缺失不判定（防网络抖动误标）
        checked += 1
        is_susp = (q["volume"] == 0 and q["open"] == 0)
        row = cur.execute(
            "SELECT COALESCE(is_suspended,0) FROM bonds WHERE bond_code=?", (code,)).fetchone()
        prev_susp = row[0] if row else 0

        # 停牌起点 = 库内最后一条有有效收盘价的交易日
        lastrow = cur.execute(
            "SELECT MAX(trade_date) FROM daily_close "
            "WHERE bond_code=? AND bond_close IS NOT NULL", (code,)).fetchone()
        last = lastrow[0] if lastrow and lastrow[0] else None

        if is_susp and last is None:
            # 从未有过成交记录：两种可能——① 新上市债，现价恒为 100.000 占位面值
            # （如 113710 四方转债、123283 丰茂转债，2026-09-07 实测被误判）；
            # ② 行情尚未采集到。二者都不是「停牌」，保守不判定，防误杀新债。
            is_susp = False

        if is_susp:
            susp_now += 1
            if not prev_susp:
                new_susp.append(code)
            cur.execute(
                "UPDATE bonds SET is_suspended=1, suspended_since=?, suspend_checked_at=? "
                "WHERE bond_code=?", (last, now, code))
        else:
            if prev_susp:
                resumed.append(code)
                cur.execute(
                    "UPDATE bonds SET is_suspended=0, suspended_since=NULL, suspend_checked_at=? "
                    "WHERE bond_code=?", (now, code))
            else:
                cur.execute(
                    "UPDATE bonds SET suspend_checked_at=? WHERE bond_code=?", (now, code))

    conn.commit()
    conn.close()
    return {"checked": checked, "suspended_now": susp_now,
            "new_suspended": new_susp, "resumed": resumed, "skipped_reason": None}


def _summary(r):
    """把检测结果转成一行可读日志。"""
    if r.get("skipped_reason"):
        return "跳过：%s" % r["skipped_reason"]
    return "检测 %d 只，停牌 %d 只（新停牌 %s，复牌 %s）" % (
        r["checked"], r["suspended_now"],
        ",".join(r["new_suspended"]) or "0 只",
        ",".join(r["resumed"]) or "0 只")


if __name__ == "__main__":
    import db
    db.init_db()
    r = refresh_suspended_status()
    print("[suspend] " + _summary(r))
