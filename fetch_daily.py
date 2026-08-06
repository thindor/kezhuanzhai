"""定时采集每日收盘价（转债 + 正股），写入 daily_close 表。

用法：
  python fetch_daily.py           # 增量采集：最近 10 个交易日
  python fetch_daily.py --history # 补全历史：最近 320 个交易日

数据源：新浪财经日K线（直连稳定，转债与正股均支持）。
建议通过 Windows 计划任务在每日收盘后（如 16:30）运行一次。
"""

import sys
from db import init_db
import crawler


if __name__ == "__main__":
    history = "--history" in sys.argv
    init_db()
    print("[daily] 开始采集（%s）..." % ("历史补全" if history else "增量"))
    ok, total = crawler.fetch_daily_all(history=history)
    print("[daily] 完成：成功 %d / 共 %d 只在交易转债" % (ok, total))
