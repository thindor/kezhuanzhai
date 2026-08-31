# -*- coding: utf-8 -*-
"""可转债系统关键数据一致性校验（独立脚本 + 可挂每日任务）。

回归防护目标：
  1) **双低快照**：当前最新一周（max(week_start)）的前 N 只快照，必须每只都有
     daily_close.bond_close 非 None 数据——防止未上市/缺数据债被错误调入（2026-08-31
     真实发生过：123284/123283/123282 以「调入价 100.00」进了轮动）。
  2) **等权指数算法**：最新一日的 chg% 必须与「各债 chg% 等权平均重算」一致，
     偏差 > 0.1% 则报警——防止 compute_equal_weight_index 在未来重写时被均价环比
     等「价格加权污染」错误实现回退（2026-08-31 真实发生过：旧版用均价环比，
     8/28→8/31 算出 +0.32%，集思录 -0.07%，正确口径 -0.073%）。

设计取舍：
  - **不**抓取集思录外部源做对比（避免登录/反爬脆弱依赖）；改用「内部自洽」：
    直接用同样的算法重算，与已写入 DB 的值对比。
  - 输出结构化报告 + 退出码非零（适合 cron 报警）；不抛异常，不修改 DB。
  - 双低校验从 bonds JOIN daily_close JOIN double_low_log 三表 join；
    等权指数从 daily_close JOIN bonds 重算最新一日 chg%。

用法：
  python verify_integrity.py                    # 默认校验 + 退出码
  python verify_integrity.py --quiet            # 只输出失败项
  python verify_integrity.py --json             # 输出 JSON 给程序消费
  python verify_integrity.py --tolerance 0.001  # 等权指数偏差阈值（默认 0.1%）
"""
import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

import db


def verify_double_low_snapshot(conn, limit=20):
    """校验最新一周双低快照：每只必须有 daily_close.bond_close 非 None。

    返回 (ok:bool, failures:list[dict], stats:dict)"""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    # 最新 week_start
    ws_row = cur.execute(
        "SELECT week_start FROM double_low_log GROUP BY week_start "
        "ORDER BY week_start DESC LIMIT 1").fetchone()
    if not ws_row:
        return True, [], {"reason": "无双低快照"}
    week_start = ws_row["week_start"]

    rows = [dict(r) for r in cur.execute("""
        SELECT dl.rank, dl.bond_code, dl.bond_name, dl.double_low, dl.bond_price,
               (SELECT MAX(d.trade_date) FROM daily_close d
                WHERE d.bond_code=dl.bond_code AND d.bond_close IS NOT NULL) last_bc_dt,
               (SELECT COUNT(*) FROM daily_close d
                WHERE d.bond_code=dl.bond_code AND d.bond_close IS NOT NULL) bc_rows,
               (SELECT listing_date FROM bonds WHERE bond_code=dl.bond_code) listing_date,
               (SELECT current_price FROM bonds WHERE bond_code=dl.bond_code) cur_price
        FROM double_low_log dl
        WHERE dl.week_start=?
        ORDER BY dl.rank
    """, (week_start,)).fetchall()]

    failures = []
    for r in rows:
        # 失败条件：bc_rows=0（daily_close 里完全没有非空 bond_close）→ 一定是未上市/缺数据
        if r["bc_rows"] == 0:
            failures.append({
                "bond_code": r["bond_code"],
                "bond_name": r["bond_name"],
                "rank": r["rank"],
                "bond_price": r["bond_price"],
                "current_price": r["cur_price"],
                "listing_date": r["listing_date"],
                "bc_rows": r["bc_rows"],
                "issue": "daily_close.bond_close 全空，疑似未上市/缺数据债进入双低",
            })
    return (len(failures) == 0), failures, {
        "week_start": week_start, "checked": len(rows), "failed": len(failures)
    }


def verify_equal_weight_index(conn, tolerance=0.001):
    """校验等权指数最新一日 chg% 与「各债 chg% 等权平均重算」一致性。

    重算口径：
      取最新一日与前一日 bond_close 非空的交集；
      return = mean(c_today / c_prev - 1)；
      chg% = return * 100。

    返回 (ok:bool, mismatch:dict|None, stats:dict)"""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    latest_row = cur.execute(
        "SELECT trade_date, index_value, avg_price, median_price, sample_n "
        "FROM equal_weight_index ORDER BY trade_date DESC LIMIT 1").fetchone()
    if not latest_row:
        return True, None, {"reason": "无等权指数数据"}

    latest_td = latest_row["trade_date"]
    # 前一交易日
    prev_row = cur.execute(
        "SELECT trade_date, index_value FROM equal_weight_index "
        "WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1", (latest_td,)).fetchone()
    if not prev_row:
        return True, None, {"reason": "只有一条等权指数，无法算环比"}

    prev_td = prev_row["trade_date"]

    # 重算 chg%：用 daily_close JOIN bonds 直接算各债 chg% 等权平均
    eb_sql = db._eb_filter_sql("b")
    rows = cur.execute(f"""
        SELECT d1.bond_code AS bond_code,
               d1.bond_close AS prev_close, d2.bond_close AS today_close
        FROM daily_close d1
        JOIN daily_close d2 ON d1.bond_code = d2.bond_code
        JOIN bonds b ON b.bond_code = d1.bond_code
        WHERE d1.trade_date = ?
          AND d2.trade_date = ?
          AND d1.bond_close IS NOT NULL
          AND d2.bond_close IS NOT NULL
          AND COALESCE(b.is_delisted,0) = 0
          AND {eb_sql}
    """, (prev_td, latest_td)).fetchall()

    chgs = []
    for r in rows:
        if r["prev_close"] and r["prev_close"] > 0:
            chgs.append(r["today_close"] / r["prev_close"] - 1.0)
    if not chgs:
        return True, None, {"reason": "重算样本为空"}

    recomputed_pct = (sum(chgs) / len(chgs)) * 100.0
    # DB 中已存的 chg_pct：从 index_value 环比推
    stored_idx = latest_row["index_value"]
    prev_idx = prev_row["index_value"]
    stored_pct = ((stored_idx / prev_idx) - 1.0) * 100.0 if prev_idx else 0.0

    diff_pct = abs(recomputed_pct - stored_pct)
    ok = diff_pct < tolerance * 100.0  # tolerance 是 0.001=0.1%，对应差值 < 0.1 个百分点
    return ok, {
        "latest_td": latest_td,
        "prev_td": prev_td,
        "stored_chg_pct": round(stored_pct, 4),
        "recomputed_chg_pct": round(recomputed_pct, 4),
        "diff_pct_pts": round(diff_pct, 4),
        "tolerance_pts": tolerance * 100.0,
        "sample_n": len(chgs),
        "issue": "等权指数 chg% 与各债 chg% 等权平均重算偏差超阈值" if not ok else "",
    }, {
        "latest_td": latest_td,
        "prev_td": prev_td,
        "stored_pct": round(stored_pct, 4),
        "recomputed_pct": round(recomputed_pct, 4),
        "diff_pts": round(diff_pct, 4),
        "sample_n": len(chgs),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="只输出失败项")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--tolerance", type=float, default=0.001,
                    help="等权指数 chg% 偏差阈值（默认 0.001 = 0.1 个百分点）")
    ap.add_argument("--double-low-limit", type=int, default=20)
    args = ap.parse_args()

    db.init_db()
    conn = db.get_conn()

    # 校验 1：双低快照
    dl_ok, dl_failures, dl_stats = verify_double_low_snapshot(conn, limit=args.double_low_limit)
    # 校验 2：等权指数
    ew_ok, ew_mismatch, ew_stats = verify_equal_weight_index(conn, tolerance=args.tolerance)

    overall_ok = dl_ok and ew_ok
    report = {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": "PASS" if overall_ok else "FAIL",
        "double_low": {
            "ok": dl_ok,
            "stats": dl_stats,
            "failures": dl_failures,
        },
        "equal_weight_index": {
            "ok": ew_ok,
            "stats": ew_stats,
            "mismatch": ew_mismatch,
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if not args.quiet:
            print("=" * 60)
            print("可转债系统数据一致性校验 @ %s" % report["as_of"])
            print("=" * 60)
            print("\n[1] 双低快照校验（最新一周）")
            print(f"    week_start={dl_stats.get('week_start')}, checked={dl_stats.get('checked', 0)}")
            if dl_ok:
                print(f"    ✓ PASS — {dl_stats.get('checked', 0)} 只快照全部有 daily_close.bond_close 数据")
            else:
                print(f"    ✗ FAIL — {dl_stats.get('failed', 0)} 只债 daily_close.bond_close 全空，疑似未上市债误入:")
                for f in dl_failures:
                    print(f"      - rank {f['rank']}: {f['bond_code']} {f['bond_name']} "
                          f"(bond_price={f['bond_price']}, current_price={f['current_price']}, "
                          f"listing_date={f['listing_date']}, bc_rows={f['bc_rows']})")

            print("\n[2] 等权指数 chg% 校验（最新一日 vs 前一交易日）")
            if "reason" in ew_stats:
                print(f"    - {ew_stats['reason']}")
            elif ew_ok:
                print(f"    ✓ PASS — latest={ew_stats['latest_td']}, stored_chg%={ew_stats['stored_pct']}, "
                      f"recomputed={ew_stats['recomputed_pct']}, diff={ew_stats['diff_pts']} 个百分点 "
                      f"(样本 {ew_stats['sample_n']})")
            else:
                print(f"    ✗ FAIL — 等权指数 chg% 与重算偏差超阈值:")
                print(f"      最新日={ew_mismatch['latest_td']}, 前一日={ew_mismatch['prev_td']}")
                print(f"      已存 chg%={ew_mismatch['stored_chg_pct']}, 重算={ew_mismatch['recomputed_chg_pct']}")
                print(f"      偏差={ew_mismatch['diff_pct_pts']} 个百分点 (阈值 {ew_mismatch['tolerance_pts']} 个百分点)")
                print(f"      样本数={ew_mismatch['sample_n']}")

            print("\n" + ("=" * 60))
            print(f"结果: {report['overall']}")

    conn.close()
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()