"""定时采集每日收盘价（转债 + 正股），写入 daily_close 表。

用法：
  python fetch_daily.py           # 增量采集：最近 10 个交易日
  python fetch_daily.py --history # 补全历史：最近 320 个交易日

数据源：新浪财经日K线（直连稳定，转债与正股均支持）。
建议通过 Windows 计划任务在每日收盘后（如 16:30）运行一次。

注意：fetch_daily_all 默认采用「自动策略」——本地 daily_close 存量<30
交易日的债（典型为新债）会自动按 320 日补全历史，其余按 10 日增量，
无需再手动加 --history；--history 仍保留作为「全量强制补全」入口。
"""

import sys
import db
import crawler


if __name__ == "__main__":
    history = "--history" in sys.argv
    db.init_db()
    # 先同步全量转债列表，自动发现并录入新上市转债（支撑「新发转债数」滚动）
    try:
        lst = crawler.fetch_all_bonds()
        new_cnt = db.sync_bonds_list(lst)
        print("[daily] 全量列表同步完成：列表总计 %d 只，本次新录入 %d 只" % (len(lst), new_cnt))
    except Exception as e:
        print("[daily] 同步全量列表失败（不影响后续收盘价采集）：%r" % e)
    print("[daily] 开始采集（%s）..." % ("历史补全" if history else "增量"))
    ok, total = crawler.fetch_daily_all(history=history)
    print("[daily] 完成：成功 %d / 共 %d 只在交易转债" % (ok, total))
    # 等权指数：每日收盘后自动重算写库（幂等，全量重算）
    try:
        db.compute_equal_weight_index()
        print("[daily] 可转债等权指数已更新")
    except Exception as e:
        print("[daily] 等权指数计算失败：%r" % e)
