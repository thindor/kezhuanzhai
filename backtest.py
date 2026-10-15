"""可转债多因子轮动策略回测引擎。

数据来源
--------
* ``daily_close``：每个交易日各转债收盘价 ``bond_close`` + 正股收盘价 ``stock_close``
* ``bonds``：静态条款（当前转股价、下修历史 down_revise_json、评级、剩余规模、
  发行规模、到期日、上市日、是否 ST/银行）
* ``equal_weight_index``：可转债等权指数（作为基准曲线）

口径说明（页面同步展示）
------------------------
1. 转股价还原：按 ``down_revise_json`` 中每个下修方案的 ``effective_date`` 回溯，
   取该日之前最近一次生效的 ``price_after``；若该日早于全部下修生效日，用最早一
   条记录的 ``price_before``（原始转股价）；无下修记录则全程用 ``current_transfer_price``。
2. 转股价值 = 100 / 当期转股价 × 正股收盘价
3. 转股溢价率(%) = (转债价 / 转股价值 - 1) × 100
4. 双低值 = 转债价 + 转股溢价率(%)
5. 到期收益率 YTM：**简化口径**，只算「期末赎回价与现价之差」的年化，
   不含中间票息现金流 —— ``(赎回价 - 转债价) / 剩余年限 / 转债价 × 100``。
   库内未持久化票息明细，故不做精确现金流贴现。
6. 剩余规模 / 评级 / ST / 银行 使用**当前值**近似，无历史快照。

已知局限（必须在页面提示用户）
------------------------------
* **幸存者偏差**：``daily_close`` 仅覆盖约 325 只转债（基本是当前在市债），
  历史上已退市的 740+ 只债没有转债价格历史。因此回测跑的是「今天的存续债」，
  收益会被系统性高估（样本天然剔除了违约/长期低迷后退市的标的）。
* **样本量**：转债价有效数据自 2020-09 起，2021 年每日仅 15~60 只、2023 年
  128~195 只、2024 年起 200+ 只、2025 年起 325 只。区间越早样本越少，
  结果越不可靠，建议起点不早于 2024-01-01。
* 回测按调仓日收盘价成交，未模拟盘中滑点；手续费按单边比例线性扣除。
"""

import json
import math
from datetime import date as _date
from datetime import datetime

import db

# ---------------------------------------------------------------- 因子定义

FACTORS = {
    "double_low":  {"label": "双低值（转债价 + 溢价率）", "order": "asc",  "unit": ""},
    "premium":     {"label": "转股溢价率",               "order": "asc",  "unit": "%"},
    "price":       {"label": "转债价格",                 "order": "asc",  "unit": "元"},
    "ytm":         {"label": "到期收益率（简化口径）",     "order": "desc", "unit": "%"},
    "conv_value":  {"label": "转股价值",                 "order": "asc",  "unit": "元"},
    "scale":       {"label": "剩余规模",                 "order": "asc",  "unit": "亿"},
    "issue_scale": {"label": "发行规模",                 "order": "asc",  "unit": "亿"},
    "years":       {"label": "剩余年限",                 "order": "desc", "unit": "年"},
}

RATING_ORDER = {
    "AAA": 9, "AAA-": 8.5, "AA+": 8, "AA": 7, "AA-": 6,
    "A+": 5, "A": 4, "A-": 3, "BBB+": 2, "BBB": 1,
}

DEFAULT_START = "2024-01-01"
RF_RATE = 0.02          # 夏普比率无风险利率


# ---------------------------------------------------------------- 静态条款

_META = {"key": None, "data": None}


def _parse_revise(js):
    """解析下修历史，返回按 effective_date 升序的列表。"""
    if not js:
        return []
    try:
        arr = json.loads(js)
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for x in arr:
        if not isinstance(x, dict):
            continue
        ed = str(x.get("effective_date") or "")[:10]
        if len(ed) != 10:
            continue

        def _f(v):
            try:
                return float(v) if v is not None else None
            except (ValueError, TypeError):
                return None

        out.append({"effective_date": ed,
                    "price_after": _f(x.get("price_after")),
                    "price_before": _f(x.get("price_before"))})
    out.sort(key=lambda z: z["effective_date"])
    return out


def bond_meta():
    """全部转债静态条款，带进程内缓存（以 bonds.updated_at 最大值做失效判断）。"""
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(updated_at) FROM bonds")
    row = cur.fetchone()
    key = row[0] if row else None
    if _META["data"] is not None and _META["key"] == key:
        conn.close()
        return _META["data"]

    cur.execute("""
        SELECT bond_code, bond_name, stock_code, stock_name, rating,
               issue_scale, remaining_scale, expire_date, listing_date,
               current_transfer_price, redeem_price, is_delisted, is_suspended,
               stock_is_st, stock_is_bank, down_revise_json
        FROM bonds
    """)
    data = {}
    for r in cur.fetchall():
        d = dict(r)
        d["_rev"] = _parse_revise(d.get("down_revise_json"))
        data[d["bond_code"]] = d
    conn.close()
    _META["key"] = key
    _META["data"] = data
    return data


_PANEL = {"key": None, "code_px": None, "dates": None, "extra": {}}
_PANEL_MAX = 3


def _panel(start, end):
    """加载 [start, end] 区间价格面板：{code: {date: (转债价, 正股价)}} 及交易日列表。

    进程内保留最近 _PANEL_MAX 个区间，避免不同起始日反复全量读库。
    """
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(trade_date), COUNT(*) FROM daily_close")
    r = cur.fetchone()
    key = (r[0], r[1], start, end)
    if _PANEL["key"] == key:
        conn.close()
        return _PANEL["code_px"], _PANEL["dates"]
    if key in _PANEL["extra"]:
        code_px, dates = _PANEL["extra"][key]
        _PANEL["key"], _PANEL["code_px"], _PANEL["dates"] = key, code_px, dates
        conn.close()
        return code_px, dates

    cur.execute("""
        SELECT bond_code, trade_date, bond_close, stock_close
        FROM daily_close
        WHERE bond_close > 0 AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
    """, (start, end))
    code_px = {}
    dates = set()
    for r in cur.fetchall():
        code, d, b, s = r[0], r[1], r[2], r[3]
        code_px.setdefault(code, {})[d] = (
            float(b), float(s) if (s is not None and s > 0) else None)
        dates.add(d)
    conn.close()
    dates = sorted(dates)

    if _PANEL["key"] is not None and _PANEL["code_px"] is not None:
        _PANEL["extra"][_PANEL["key"]] = (_PANEL["code_px"], _PANEL["dates"])
        while len(_PANEL["extra"]) > _PANEL_MAX:
            _PANEL["extra"].pop(next(iter(_PANEL["extra"])))
    _PANEL.update(key=key, code_px=code_px, dates=dates)
    return code_px, dates


# ---------------------------------------------------------------- 单债指标

def years_left(expire_str, ref):
    """到期日到 ref('YYYY-MM-DD') 的剩余年限（小数）。"""
    if not expire_str:
        return None
    try:
        ed = datetime.strptime(str(expire_str)[:10], "%Y-%m-%d").date()
        rd = datetime.strptime(ref, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return max(0.0, (ed - rd).days / 365.0)


def transfer_price_at(m, d):
    """还原 d 当日的有效转股价（考虑历史下修）。"""
    rev = m.get("_rev") or []
    tp = None
    for x in rev:
        if x["effective_date"] <= d:
            if x["price_after"]:
                tp = x["price_after"]
        else:
            break
    if tp:
        return tp
    if rev and rev[0]["price_before"]:
        return rev[0]["price_before"]
    try:
        return float(m["current_transfer_price"]) if m.get("current_transfer_price") else None
    except (ValueError, TypeError):
        return None


def bond_metrics(m, d, bond_px, stock_px):
    """计算单只债在 d 日的全部因子值。"""
    tp = transfer_price_at(m, d)
    conv = (100.0 / tp * stock_px) if (tp and stock_px) else None
    premium = (bond_px / conv - 1.0) * 100.0 if conv else None
    yl = years_left(m.get("expire_date"), d)
    ytm = None
    try:
        rp = float(m["redeem_price"]) if m.get("redeem_price") else None
    except (ValueError, TypeError):
        rp = None
    if rp and yl and yl > 0:
        ytm = (rp - bond_px) / yl / bond_px * 100.0
    return {
        "transfer_price": tp,
        "conv_value": conv,
        "premium": premium,
        "years": yl,
        "ytm": ytm,
        "double_low": (bond_px + premium) if premium is not None else None,
    }


def _factor_value(factor, met, m, bond_px):
    if factor == "price":
        return bond_px
    if factor == "premium":
        return met["premium"]
    if factor == "double_low":
        return met["double_low"]
    if factor == "ytm":
        return met["ytm"]
    if factor == "conv_value":
        return met["conv_value"]
    if factor == "years":
        return met["years"]
    if factor in ("scale", "issue_scale"):
        v = m.get("remaining_scale") if factor == "scale" else m.get("issue_scale")
        try:
            v = float(v) if v is not None else None
        except (ValueError, TypeError):
            v = None
        return v if (v is None or v > 0) else None
    return None


# ---------------------------------------------------------------- 基准

def _benchmark(start, end):
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT trade_date, index_value FROM equal_weight_index
        WHERE trade_date >= ? AND trade_date <= ? AND index_value > 0
        ORDER BY trade_date
    """, (start, end))
    rows = [(r[0], float(r[1])) for r in cur.fetchall()]
    conn.close()
    if len(rows) < 2:
        return {"dates": [], "values": []}
    base = rows[0][1]
    return {"dates": [x[0] for x in rows],
            "values": [round(x[1] / base, 4) for x in rows]}


# ---------------------------------------------------------------- 主流程

def run_backtest(factor="double_low", order=None, topn=20, rebalance=5,
                 start=DEFAULT_START, end=None, min_price=None, max_price=None,
                 min_rating=None, min_scale=None, max_scale=None,
                 min_years=None, exclude_st=True, exclude_bank=True,
                 sell_topn=None, fee=0.0):
    """执行回测，返回可直接交给模板的数据结构。

    参数
    ----
    factor      主排序因子（见 FACTORS）
    order       'asc' / 'desc'，None 则用因子默认方向
    topn        每期持仓只数
    rebalance   轮动周期（交易日数）
    start/end   回测起止日期
    sell_topn   缓冲区：已持仓债跌出该名次才卖出（默认 = topn，即无缓冲）
    fee         单边手续费（%），按换手率线性扣除
    其余为候选池硬门槛过滤。
    """
    warnings = []

    if factor not in FACTORS:
        factor = "double_low"
    meta_all = FACTORS[factor]
    if order not in ("asc", "desc"):
        order = meta_all["order"]
    reverse = (order == "desc")

    topn = max(1, min(int(topn or 20), 100))
    rebalance = max(1, min(int(rebalance or 5), 250))
    sell_topn = int(sell_topn) if sell_topn else topn
    sell_topn = max(topn, min(sell_topn, 200))
    fee = max(0.0, min(float(fee or 0.0), 2.0))

    start = str(start or DEFAULT_START)[:10]
    meta = bond_meta()
    code_px, all_dates = _panel(start, "9999-12-31")
    if end:
        end = str(end)[:10]
        all_dates = [d for d in all_dates if d <= end]
    if len(all_dates) < 2:
        return {"ok": False, "error": "所选区间没有可用的价格数据，请调整起始日期。"}

    # 调仓日序列
    rebal_dates = all_dates[::rebalance]

    min_px = float(min_price) if min_price not in (None, "") else None
    max_px = float(max_price) if max_price not in (None, "") else None
    min_sc = float(min_scale) if min_scale not in (None, "") else None
    max_sc = float(max_scale) if max_scale not in (None, "") else None
    min_yr = float(min_years) if min_years not in (None, "") else None
    min_rt = RATING_ORDER.get(str(min_rating).upper()) if min_rating else None

    def eligible(code, d, bond_px, met, m):
        if min_px is not None and bond_px < min_px:
            return False
        if max_px is not None and bond_px > max_px:
            return False
        if exclude_st and m.get("stock_is_st"):
            return False
        if exclude_bank and m.get("stock_is_bank"):
            return False
        if min_rt is not None:
            rt = RATING_ORDER.get(str(m.get("rating") or "").upper())
            if rt is None or rt < min_rt:
                return False
        if min_sc is not None or max_sc is not None:
            try:
                sc = float(m["remaining_scale"]) if m.get("remaining_scale") else None
            except (ValueError, TypeError):
                sc = None
            if sc is None:
                return False
            if min_sc is not None and sc < min_sc:
                return False
            if max_sc is not None and sc > max_sc:
                return False
        if min_yr is not None:
            if met["years"] is None or met["years"] < min_yr:
                return False
        return True

    def rank_on(d):
        """返回 d 日按因子排序后的候选列表。"""
        out = []
        for code, pxmap in code_px.items():
            p = pxmap.get(d)
            if not p:
                continue
            m = meta.get(code)
            if not m:
                continue
            bond_px, stock_px = p
            met = bond_metrics(m, d, bond_px, stock_px)
            fv = _factor_value(factor, met, m, bond_px)
            if fv is None or not math.isfinite(fv):
                continue
            if not eligible(code, d, bond_px, met, m):
                continue
            out.append({
                "bond_code": code,
                "bond_name": m.get("bond_name") or code,
                "stock_name": m.get("stock_name") or "",
                "price": round(bond_px, 3),
                "metric": round(fv, 3),
                "premium": round(met["premium"], 2) if met["premium"] is not None else None,
                "years": round(met["years"], 2) if met["years"] is not None else None,
                "ytm": round(met["ytm"], 2) if met["ytm"] is not None else None,
                "double_low": round(met["double_low"], 2) if met["double_low"] is not None else None,
                "rating": m.get("rating") or "",
                "scale": (round(float(m["remaining_scale"]), 2)
                          if m.get("remaining_scale") else None),
            })
        # 主键排序 + 代码兜底，保证同值稳定
        out.sort(key=lambda x: (x["metric"], x["bond_code"]), reverse=reverse)
        return out

    def price_on_or_before(code, d):
        """该债在 d 日或之前最近一个有效收盘价（处理停牌/退市）。"""
        pxmap = code_px.get(code)
        if not pxmap:
            return None, None
        if d in pxmap:
            return pxmap[d][0], d
        best = None
        for dd in pxmap:
            if dd <= d and (best is None or dd > best):
                best = dd
        if best is None:
            return None, None
        return pxmap[best][0], best

    periods = []
    nav = 1.0
    nav_series = [(rebal_dates[0], 1.0)] if rebal_dates else []
    returns = []
    prev_holds = []

    for i, d in enumerate(rebal_dates):
        ranked = rank_on(d)
        rank_of = {x["bond_code"]: k for k, x in enumerate(ranked)}

        # 缓冲区：已持仓且仍在 sell_topn 名次内 → 保留
        keep = [c for c in prev_holds if c in rank_of and rank_of[c] < sell_topn]
        picks = list(keep)
        for x in ranked:
            if len(picks) >= topn:
                break
            if x["bond_code"] not in picks:
                picks.append(x["bond_code"])

        if not picks:
            continue

        nxt = rebal_dates[i + 1] if i + 1 < len(rebal_dates) else None
        closed = nxt is not None

        holdings = []
        rets = []
        for code in picks:
            entry = None
            for x in ranked:
                if x["bond_code"] == code:
                    entry = x
                    break
            buy_px = entry["price"] if entry else None
            if buy_px is None:
                _, bd = price_on_or_before(code, d)
                if bd:
                    buy_px = code_px[code][bd][0]
            if not buy_px:
                continue
            if closed:
                end_px, _ed = price_on_or_before(code, nxt)
            else:
                end_px, _ed = price_on_or_before(code, all_dates[-1])
            if not end_px:
                end_px = buy_px
            r = end_px / buy_px - 1.0
            rets.append(r)
            row = dict(entry) if entry else {
                "bond_code": code, "bond_name": meta.get(code, {}).get("bond_name") or code,
                "stock_name": meta.get(code, {}).get("stock_name") or "",
                "price": round(buy_px, 3), "metric": None, "premium": None,
                "years": None, "ytm": None, "double_low": None,
                "rating": meta.get(code, {}).get("rating") or "", "scale": None}
            row.update({"buy_price": round(buy_px, 3), "end_price": round(end_px, 3),
                        "ret": round(r * 100, 2),
                        "rank": rank_of.get(code, 0) + 1,
                        "kept": code in keep})
            holdings.append(row)

        if not rets:
            continue
        gross = sum(rets) / len(rets)

        # 换手成本：新买入只数 × 2（买+卖）单边费率
        if i == 0:
            changed = len(picks)
        else:
            changed = len([c for c in picks if c not in prev_holds])
        cost = fee / 100.0 * (2.0 * changed / len(picks)) if fee else 0.0

        net = gross - cost
        nav *= (1.0 + net)
        returns.append(net)
        periods.append({
            "date": d,
            "next_date": nxt,
            "holding": not closed,
            "candidates": len(ranked),
            "gross": round(gross * 100, 2),
            "cost": round(cost * 100, 3),
            "ret": round(net * 100, 2),
            "nav": round(nav, 4),
            "n": len(holdings),
            "holdings": holdings,
        })
        nav_series.append((nxt or all_dates[-1], round(nav, 4)))
        prev_holds = picks

    if not periods:
        return {"ok": False, "error": "该参数下没有选出任何持仓，请放宽过滤条件或检查区间。"}

    # ---------------- 指标 ----------------
    def _std(xs):
        if len(xs) < 2:
            return 0.0
        mu = sum(xs) / len(xs)
        return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))

    total_ret = nav - 1.0
    d0 = datetime.strptime(rebal_dates[0], "%Y-%m-%d").date()
    d1 = datetime.strptime((periods[-1]["next_date"] or all_dates[-1]), "%Y-%m-%d").date()
    span_days = max(1, (d1 - d0).days)
    annual = (nav) ** (365.0 / span_days) - 1.0 if nav > 0 else -1.0
    vol = _std(returns) * math.sqrt(252.0 / rebalance)
    sharpe = ((annual - RF_RATE) / vol) if vol > 1e-9 else None

    peak = 1.0
    max_dd = 0.0
    for _d, v in nav_series:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1.0)

    win = len([r for r in returns if r > 0])
    bench = _benchmark(rebal_dates[0], periods[-1]["next_date"] or all_dates[-1])

    # ---------------- 提示 ----------------
    warns = []
    avg_cand = sum(p["candidates"] for p in periods) / len(periods)
    if avg_cand < 30:
        warns.append("每期候选池平均仅 %.0f 只，样本偏少，结果仅供参考（建议把起始日期调到 2024 年之后）。" % avg_cand)
    if rebal_dates[0] < "2024-01-01":
        warns.append("起始日期早于 2024-01-01：早期每日仅 100 余只转债有价格，覆盖率不足，指标波动大。")
    warns.append("⚠️ 存在幸存者偏差：历史库仅收录当前存续的 325 只转债价格，已退市的 740+ 只债无历史价，"
                 "因此本结果未包含「买入后违约/长期低迷退市」的样本，收益被系统性高估。")
    warns.append("剩余规模、评级、ST/银行标签均按**当前值**近似，未还原历史状态。")
    warns.append("到期收益率 YTM 为简化口径（仅算赎回价与现价的年化差，不含中间票息现金流）。")
    if not closed_now(periods):
        warns.append("最后一期持仓尚未卖出，按区间最后一个交易日收盘价估值（标记为「持有中」）。")

    return {
        "ok": True,
        "params": {
            "factor": factor, "factor_label": meta_all["label"],
            "order": order, "order_label": "升序（取最小）" if order == "asc" else "降序（取最大）",
            "topn": topn, "sell_topn": sell_topn, "rebalance": rebalance,
            "start": rebal_dates[0], "end": periods[-1]["next_date"] or all_dates[-1],
            "fee": fee,
            "filters": _filter_desc(min_px, max_px, min_rt, min_sc, max_sc, min_yr,
                                    exclude_st, exclude_bank),
        },
        "chart": {
            "dates": [x[0] for x in nav_series],
            "nav": [x[1] for x in nav_series],
            "bench_dates": bench["dates"],
            "bench": bench["values"],
        },
        "metrics": {
            "total_return": round(total_ret * 100, 2),
            "annual_return": round(annual * 100, 2),
            "max_drawdown": round(max_dd * 100, 2),
            "win_rate": round(100.0 * win / len(returns), 1),
            "volatility": round(vol * 100, 2),
            "sharpe": round(sharpe, 2) if sharpe is not None else None,
            "periods": len(periods),
            "avg_period_ret": round(100.0 * sum(returns) / len(returns), 2),
            "bench_return": round((bench["values"][-1] - 1) * 100, 2) if bench["values"] else None,
            "span_days": span_days,
            "final_nav": round(nav, 4),
        },
        "periods": periods,
        "warnings": warns,
        "coverage": {"dates": len(rebal_dates), "bonds": len(code_px),
                     "price_from": all_dates[0], "price_to": all_dates[-1]},
    }


def closed_now(periods):
    return not periods[-1]["holding"]


def _filter_desc(min_px, max_px, min_rt, min_sc, max_sc, min_yr, ex_st, ex_bank):
    parts = []
    if min_px is not None or max_px is not None:
        parts.append("价格 %s~%s 元" % (min_px if min_px is not None else "-",
                                     max_px if max_px is not None else "-"))
    if min_rt is not None:
        parts.append("评级 ≥ %s" % min_rt)
    if min_sc is not None or max_sc is not None:
        parts.append("剩余规模 %s~%s 亿" % (min_sc if min_sc is not None else "-",
                                        max_sc if max_sc is not None else "-"))
    if min_yr is not None:
        parts.append("剩余年限 ≥ %s 年" % min_yr)
    if ex_st:
        parts.append("排除 ST 正股")
    if ex_bank:
        parts.append("排除银行正股")
    if not parts:
        parts.append("无（全市场可交易转债）")
    return "、".join(parts)


if __name__ == "__main__":
    import sys
    r = run_backtest(factor="double_low", topn=20, rebalance=5,
                     start="2024-01-01", end="2026-09-23")
    if not r.get("ok"):
        print("FAIL:", r.get("error"))
        sys.exit(1)
    print("参数:", r["params"])
    print("指标:", r["metrics"])
    print("期数:", len(r["periods"]), "| 覆盖:", r["coverage"])
    for p in r["periods"][:3]:
        print("  %s -> %s  %.2f%%  nav=%.4f  候选%d只  持仓%d只" %
              (p["date"], p["next_date"], p["ret"], p["nav"], p["candidates"], p["n"]))
    print("  ...")
    for p in r["periods"][-2:]:
        print("  %s -> %s  %.2f%%  nav=%.4f" % (p["date"], p["next_date"], p["ret"], p["nav"]))
    print("末轮持仓:", [(h["bond_code"], h["bond_name"], h["ret"]) for h in r["periods"][-1]["holdings"][:5]])
