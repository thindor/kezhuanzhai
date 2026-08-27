# -*- coding: utf-8 -*-
"""每日采集总入口（编排器）。

把原先散落在多处、各自独立调度的采集收口为单一入口，顺序：
  1) crawler.fetch_daily_all()              收盘后刷新行情（写 daily_close，并供 seed 回写 bonds.current_price）
  2) seed_bonds.main()                      刷新基础条款/转股价/退市判定，并用 daily_close 回写 bonds.current_price
  3) db.backfill_delist_status()            【退市检测】按已存到期日/摘牌日幂等回填 is_delisted（显式步骤，新标记数写入采集日志）；
                                            标退后本步起所有后续步骤只遍历未退市券，已退市券自此不再被任何定时任务更新
  4) mini_bond.ensure_columns()+refresh_all()  刷新小盘债候选（现价/赎回价/历史最高）写回 bonds
  5) checkup.refresh_remaining_scales()        滚动补全剩余规模（集思录前30活跃债写入 bonds.remaining_scale）
  6) checkup.refresh_redeem_prices()        补全到期赎回价（东财全量基础解析写入 bonds.redeem_price）
  7) checkup.refresh_transfer_prices()      回填当前转股价（akshare.bond_zh_cov_info 全市场遍历，修复东财 TRANSFER_PRICE=None/被初始价污染的债）

设计要点：
  - 顺序：先行情(daily_close)，再 seed（用 daily_close 回写现价/转股价），再小盘债，沿用现网 16:30→16:35 时序，
    保证 bonds.current_price 反映当日收盘。
  - 失败隔离：任一步异常仅记录，不中断后续步骤；行情与基础任一步失败则进程返回非零码，便于调度侧报警。
  - 已退市债：行情fetch_daily_all 仅取未退市(get_active_trading_bonds)；基础 seed_bonds 对已退市债冻结不回写；
    其余刷新步骤(remaining_scale/redeem_price/transfer_price/mini_bond)本身只遍历未退市券；第 3 步「退市检测」
    显式按到期日/摘牌日幂等回填 is_delisted，标退后上述步骤在【同一轮】起即跳过该券。即「已退市不再采集」。
  - 结构化日志：每次运行在 collect_runs / collect_steps 落库（管理后台 /admin/collect-logs 可查每步成败与错误原文），
    便于线上排错。同时保留 stdout 输出，供 cron 重定向日志做备份。
  - 交易日守卫：非交易日（周末 + config.TRADING_HOLIDAYS）自动跳过并记录 skipped 运行；--force 可强制。
  - 公告由 KZZ_Announcements_Daily 每日 08:30 单独跑（强赎/下修时效性强，保留独立节奏），不并入本批。

用法：
  python collect_daily.py                 # 供部署服务器 cron / WorkBuddy 自动化(KZZ_Daily_Close)调用
  python collect_daily.py --force         # 强制运行（忽略交易日守卫），管理后台「立即采集」用
  COLLECT_TRIGGER=admin python collect_daily.py --force --trigger=admin
"""
import os
import sys
import time
import traceback
from datetime import datetime

import db
import crawler
import seed_bonds
import mini_bond
import checkup


def _is_trading_day(dt=None):
    """返回今天是否为交易日（用于自动采集守卫）。

    - 周六(5)/周日(6) => 非交易日
    - config.TRADING_HOLIDAYS 中的日期 => 非交易日
    - 其余 => 交易日
    """
    from config import TRADING_HOLIDAYS
    d = dt or datetime.now()
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    if d.strftime("%Y-%m-%d") in TRADING_HOLIDAYS:
        return False
    return True


def _run_step(run_id, step_no, step_name, fn):
    """执行单步采集，异常隔离，并把成败/错误原文写入 collect_steps。返回 True/False。"""
    t0 = time.time()
    db.begin_collect_step(run_id, step_no, step_name)
    print("\n=== [%s] 开始 ===" % step_name)
    try:
        ret = fn()
        dur = time.time() - t0
        print("=== [%s] 完成，耗时 %.1fs ===" % (step_name, dur))
        msg = ret if isinstance(ret, str) else "成功"
        db.finish_collect_step(run_id, step_no, step_name, "success",
                               message=msg, duration=dur)
        return True
    except Exception as e:
        dur = time.time() - t0
        err = traceback.format_exc()
        print("!!! [%s] 失败：%s" % (step_name, e))
        traceback.print_exc()
        db.finish_collect_step(run_id, step_no, step_name, "failed",
                               message=str(e)[:500], error_text=err, duration=dur)
        return False


def _step_detect_delist():
    """每日退市检测：按 bonds 表已存的到期日/摘牌日幂等回填 is_delisted。

    返回可读的新标记数串（写入采集日志 message，便于管理后台一眼看到本次新退市几只）。
    这是「退市的转债不再定时更新数据」的总开关——本步标退后，后续行情/基础/小盘/规模/
    赎回价/转股价各步都只遍历未退市券（get_active_trading_bonds 与各处 is_delisted 过滤），
    已退市券自此被冻结、不再被任何定时任务碰。
    """
    n = db.backfill_delist_status()
    print("退市检测：本次新标记退市 %d 只（详见 bonds.is_delisted）" % n)
    return "新标记退市 %d 只" % n


def main():
    # 解析参数
    force = "--force" in sys.argv
    trigger = "admin" if "--trigger=admin" in sys.argv else os.environ.get("COLLECT_TRIGGER", "scheduler")

    # 确保表结构存在（独立运行时不经过 app.init_db），并让 collect_runs 表可用
    db.init_db()
    # 先回收可能因服务重启/沙箱重置而中断、永远停在 running 的遗留记录，保证日志如实、不锁「立即采集」
    db.recover_stale_runs()

    # 交易日守卫：非交易日跳过（--force 可绕过，用于管理后台手动补采 / 排错）
    if not force and not _is_trading_day():
        reason = "非交易日（周末/法定节假日），跳过自动采集"
        run_id = db.start_collect_run(trigger)
        db.finish_collect_run(run_id, "skipped", notes=reason)
        print("[collect] %s @ %s" % (reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        return

    t_all = time.time()
    run_id = db.start_collect_run(trigger)
    print("[collect] 每日采集总入口启动 @ %s  trigger=%s run_id=%s"
          % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trigger, run_id))

    # 顺序：行情 -> 基础数据 -> 退市检测 -> 小盘债 -> 剩余规模 -> 到期赎回价 -> 当前转股价
    steps = [
        ("行情 daily_close", lambda: crawler.fetch_daily_all()),
        ("基础数据 seed_bonds", seed_bonds.main),
        ("退市检测 backfill_delist_status", _step_detect_delist),
        ("小盘债 mini_bond", lambda: (mini_bond.ensure_columns(), mini_bond.refresh_all())),
        ("剩余规模 remaining_scale", checkup.refresh_remaining_scales),
        ("到期赎回价 redeem_price", checkup.refresh_redeem_prices),
        ("当前转股价 transfer_price", checkup.refresh_transfer_prices),
    ]
    results = []
    for i, (name, fn) in enumerate(steps, 1):
        results.append(_run_step(run_id, i, name, fn))
    r1, r2 = results[0], results[1]

    # 汇总判定
    ok_all = all(results)
    # 关键步骤（行情/基础）成功即视为「基本成功」；二者之一失败则整体失败
    if ok_all:
        final_status = "success"
    elif r1 and r2:
        final_status = "partial"   # 次要步骤失败，但核心数据可用
    else:
        final_status = "failed"
    notes = "行情=%s 基础=%s 退市检测=%s 小盘=%s 剩余规模=%s 赎回价=%s 转股价=%s，总耗时 %.1fs" % (
        results[0], results[1], results[2], results[3], results[4], results[5], results[6],
        time.time() - t_all)
    db.finish_collect_run(run_id, final_status, notes=notes)
    print("\n[collect] 运行结束 run_id=%s status=%s：%s" % (run_id, final_status, notes))

    # 关键步骤（行情/基础）失败则非零退出，调度侧可据此报警
    if not (r1 and r2):
        sys.exit(2)


if __name__ == "__main__":
    main()
