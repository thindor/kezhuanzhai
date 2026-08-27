# -*- coding: utf-8 -*-
"""每日采集总入口（编排器）。

把原先散落在多处、各自独立调度的采集收口为单一入口，顺序：
  1) crawler.fetch_daily_all()              收盘后刷新行情（写 daily_close，并供 seed 回写 bonds.current_price）
  2) seed_bonds.main()                      刷新基础条款/转股价/退市判定，并用 daily_close 回写 bonds.current_price
  3) mini_bond.ensure_columns()+refresh_all()  刷新小盘债候选（现价/赎回价/历史最高）写回 bonds
  4) checkup.refresh_remaining_scales()        滚动补全剩余规模（集思录前30活跃债写入 bonds.remaining_scale）
  5) checkup.refresh_redeem_prices()        补全到期赎回价（东财全量基础解析写入 bonds.redeem_price）

设计要点：
  - 顺序：先行情(daily_close)，再 seed（用 daily_close 回写现价/转股价），再小盘债，沿用现网 16:30→16:35 时序，
    保证 bonds.current_price 反映当日收盘。
  - 失败隔离：任一步异常仅记录，不中断后续步骤；行情与基础任一步失败则进程返回非零码，便于调度侧报警。
  - 公告由 KZZ_Announcements_Daily 每日 08:30 单独跑（强赎/下修时效性强，保留独立节奏），不并入本批。
  - 好处：口径一致（所有页面读同一张 bonds/daily_close）、抗限流（各源自带重试退避）、一个日志好排错。

用法：
  python collect_daily.py       # 供 Windows 计划任务 / WorkBuddy 自动化(KZZ_Daily_Close)调用
"""
import sys
import time
import traceback

import db
import crawler
import seed_bonds
import mini_bond
import checkup


def _run(step_name, fn):
    """执行单步采集，异常隔离。返回 True/False。"""
    t0 = time.time()
    try:
        print("\n=== [%s] 开始 ===" % step_name)
        fn()
        print("=== [%s] 完成，耗时 %.1fs ===" % (step_name, time.time() - t0))
        return True
    except Exception as e:
        print("!!! [%s] 失败：%s" % (step_name, e))
        traceback.print_exc()
        return False


def main():
    db.init_db()  # 确保表结构存在（独立运行时不经过 app.init_db）
    t_all = time.time()
    print("[collect] 每日采集总入口启动 @ %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    # 顺序：行情 -> 基础数据 -> 小盘债 -> 剩余规模 -> 到期赎回价
    r1 = _run("行情 daily_close", lambda: crawler.fetch_daily_all())
    r2 = _run("基础数据 seed_bonds", lambda: seed_bonds.main())
    r3 = _run("小盘债 mini_bond",
              lambda: (mini_bond.ensure_columns(), mini_bond.refresh_all()))
    r4 = _run("剩余规模 remaining_scale", checkup.refresh_remaining_scales)
    r5 = _run("到期赎回价 redeem_price", checkup.refresh_redeem_prices)

    print("\n[collect] 全部步骤结束：行情=%s 基础=%s 小盘=%s 剩余规模=%s 赎回价=%s，总耗时 %.1fs"
          % (r1, r2, r3, r4, r5, time.time() - t_all))

    # 关键步骤（行情/基础）失败则非零退出，调度侧可据此报警
    if not (r1 and r2):
        sys.exit(2)


if __name__ == "__main__":
    main()
