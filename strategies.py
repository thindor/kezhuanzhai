# -*- coding: utf-8 -*-
"""多策略轮动（双低 / 小额债 / 低价债）。

三个策略共用同一套框架：同一候选池、同样取 20 只、同样等权、同样每周一轮动，
唯一的区别是"按什么排序"。这样它们的收益才有可比性。

统一口径
--------
1. 候选池（三策略共用，不因策略而异）：
   - 未退市、未到期、已上市（db.get_active_trading_bonds）
   - 未停牌（db.get_suspended_bond_codes）
   - 未公告强赎（db.get_redeemed_bond_codes）
   - 最近 5 个交易日有真实成交价（行情新鲜度守卫，防僵尸价）
   ⚠️ **不排除 ST 与银行正股**：本页是策略横向对比，名单差异会污染对比结果；
      「回避银行/ST」属于 /double-low 那套实盘策略的额外风控，不在这里生效。
2. 每策略取 20 只，等权，每只 1 万元（与双低页口径一致）。
3. 每周一轮动，幂等写入 strategy_log（同一周重复运行会覆盖而非追加）。
4. 收益口径：调入价 = 首次进入周的周一真实收盘价；现价 = 最新收盘价；
   浮动收益 = (现价 - 调入价) / 调入价。已调出债的已实现收益不在持仓表内统计。
5. metric_value = 该策略的展示指标。双低策略是双低值、小额债是剩余规模，
   低价债是**到期收益率**（全站统一口径 checkup.ytm_of_bond，含各年票息的精确
   现金流贴现；仅作「便宜还是陷阱」的参考，**不参与排序**：低价债仍按转债价格升序选取）。
"""

import datetime as _dt

import db
import checkup

TOPN = 20

STRATEGIES = [
    {
        "code": "double_low",
        "name": "双低策略",
        "metric_label": "双低值",
        "metric_key": "double_low",
        "desc": "双低值 = 转债价 + 转股溢价率(%)，取最低的 20 只。不区分银行与 ST。",
        "rule": "双低值升序",
    },
    {
        "code": "small_size",
        "name": "小额债策略",
        "metric_label": "剩余规模(亿)",
        "metric_key": "remaining_scale",
        "desc": "剩余规模 < 4 亿且剩余期限 > 1 年，按规模从小到大取 20 只。",
        "rule": "剩余规模升序（<4 亿，期限>1 年）",
    },
    {
        "code": "low_price",
        "name": "低价债策略",
        "metric_label": "到期收益率(%)",
        "metric_key": "ytm",
        "desc": "不看溢价率、不看规模，单纯按转债价格从低到高取 20 只。表中的「到期收益率」"
                "为含各年票息的精确现金流贴现（与 /bonds 列表页同一口径），用来判断这只"
                "便宜债是「真便宜」还是「被信用风险折价」，<strong>不参与排序</strong>。",
        "rule": "转债价升序",
    },
]

STRATEGY_MAP = {s["code"]: s for s in STRATEGIES}


def get_strategy(code):
    return STRATEGY_MAP.get(code)


def this_monday(today=None):
    """本周周一日期字符串。"""
    d = today or _dt.date.today()
    return (d - _dt.timedelta(days=d.weekday())).strftime("%Y-%m-%d")


def build_candidates():
    """构建三策略共用的候选池。

    返回 list[dict(bond_code, bond_name, stock_name, rating, bond_price,
                   premium_rate, double_low, remaining_scale, years_left)]。
    double_low / premium_rate 在缺正股价或转股价时为 None（这类债仍可参与
    小额、低价策略，只是进不了双低排序）。
    """
    redeemed = db.get_redeemed_bond_codes()
    suspended = db.get_suspended_bond_codes()
    recent = set(db.get_recent_trade_dates(5))
    out = []
    for b in db.get_active_trading_bonds():
        code = b["bond_code"]
        if code in redeemed or code in suspended:
            continue
        g = db.get_bond(code)
        if not g:
            continue
        q = db.get_latest_quotes(code) or {}
        # 行情新鲜度守卫：无最近成交价的债不能进任何策略
        if recent and q.get("bond_date") not in recent:
            continue
        try:
            bp = float(q.get("bond_close"))
        except (TypeError, ValueError):
            continue
        if not bp or bp <= 0:
            continue
        try:
            scale = float(g.get("remaining_scale")) if g.get("remaining_scale") is not None else None
        except (TypeError, ValueError):
            scale = None
        years = checkup.years_left(g.get("expire_date"))
        years = round(years, 2) if years is not None else None
        # 到期收益率 YTM：走全站统一口径 checkup.ytm_of_bond（含各年票息的精确
        # 现金流贴现，票息来自 bonds.coupon_json），价格用最新收盘 bp（与列表页
        # 「现价」列同源）。缺票息 / 缺赎回价的债返回 None，页面显示 '-'。
        ytm = checkup.ytm_of_bond(g, price=bp)
        # 溢价率 / 双低值（需要正股价 + 转股价）
        prem = dl = None
        if not recent or q.get("stock_date") in recent:
            try:
                tp = float(b.get("current_transfer_price") or 0)
                sc = float(q.get("stock_close"))
                if tp > 0 and sc > 0:
                    cv = 100.0 / tp * sc
                    prem = round((bp / cv - 1.0) * 100.0, 2)
                    dl = round(bp + prem, 2)
            except (TypeError, ValueError, ZeroDivisionError):
                prem = dl = None
        out.append({
            "bond_code": code,
            "bond_name": g.get("bond_name"),
            "stock_name": g.get("stock_name"),
            "rating": g.get("rating"),
            "bond_price": round(bp, 2),
            "premium_rate": prem,
            "double_low": dl,
            "remaining_scale": scale,
            "years_left": years,
            "ytm": ytm,
        })
    return out


def pick(code, cands=None, topn=TOPN):
    """按策略规则排序取前 topn，带 rank 与 metric_value。"""
    s = get_strategy(code)
    if not s:
        return []
    if cands is None:
        cands = build_candidates()
    rows = list(cands)
    if code == "double_low":
        rows = [r for r in rows if r.get("double_low") is not None]
        rows.sort(key=lambda r: r["double_low"])
    elif code == "small_size":
        rows = [r for r in rows
                if r.get("remaining_scale") is not None and r["remaining_scale"] < 4
                and r.get("years_left") is not None and r["years_left"] > 1]
        rows.sort(key=lambda r: r["remaining_scale"])
    elif code == "low_price":
        rows.sort(key=lambda r: r["bond_price"])
    else:
        return []
    rows = rows[:topn]
    key = s["metric_key"]
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["metric_value"] = r.get(key)
    return rows


def rotate(code, force=False):
    """单个策略轮动（幂等）：计算当前名单并写入本周快照。

    返回 dict(code, week_start, rows, entered, exited, skipped)。
    """
    ws = this_monday()
    prev_ws = db.get_strategy_latest_week(code)
    if prev_ws == ws and not force:
        return {"code": code, "week_start": ws, "skipped": True,
                "rows": db.get_strategy_snapshot(code, ws),
                "entered": [], "exited": []}
    prev_rows = db.get_strategy_snapshot(code, prev_ws) if prev_ws else []
    rows = pick(code)
    db.save_strategy_snapshot(code, ws, rows)
    prev_codes = {r["bond_code"] for r in prev_rows}
    cur_codes = {r["bond_code"] for r in rows}
    return {
        "code": code,
        "week_start": ws,
        "skipped": False,
        "rows": rows,
        "entered": [r for r in rows if r["bond_code"] not in prev_codes],
        "exited": [r for r in prev_rows if r["bond_code"] not in cur_codes],
    }


def rotate_all(force=False):
    """所有策略轮动一次（周一管道调用）。"""
    return [rotate(s["code"], force=force) for s in STRATEGIES]


def rotate_all_monday(force=False):
    """周一守卫版：非周一直接跳过（除非 force）。"""
    today = _dt.date.today()
    if today.weekday() != 0 and not force:
        return "跳过（仅周一执行）"
    parts = []
    for r in rotate_all(force=force):
        s = get_strategy(r["code"])
        if r.get("skipped"):
            parts.append("%s 本周已轮动" % s["name"])
            continue
        ent = "、".join(x["bond_name"] for x in r.get("entered", [])) or "无"
        ext = "、".join(x["bond_name"] for x in r.get("exited", [])) or "无"
        parts.append("%s：新进 %s，调出 %s" % (s["name"], ent, ext))
    return "；".join(parts)
