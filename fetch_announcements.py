"""定时采集可转债公告（真实事件：即将发行/强赎/不强赎/下修）。

用法：
  python fetch_announcements.py          # 增量更新（按 (code,type) 覆盖式去重）
  python fetch_announcements.py --clear  # 清空公告表后全量重算

数据源：
  - 即将发行：东方财富 RPT_BOND_CB_LIST 申购日历
  - 强赎/不强赎：akshare 集思录强赎表（兜底信息源）
  - 下修：本地 bonds 表 down_revise_json（集思录采集，主源+akshare 兜底）
建议通过 Windows 任务计划程序每日定时执行一次。
"""
import sys
import time

import crawler
from db import init_db, upsert_announcement, clear_announcements


def main():
    # 确保 announcements 等表存在（独立运行脚本时不经过 app.py 的 init_db）
    init_db()
    clear = "--clear" in sys.argv
    if clear:
        print("[ann] 清空旧公告表 ...")
        clear_announcements()
    t0 = time.time()
    print("[ann] 开始采集可转债公告（东财全量驱动）...")
    try:
        count, rows = crawler.fetch_announcements()
    except Exception as e:
        print("[ann] 采集失败：%s" % e)
        return
    print("[ann] 采集到 %d 条候选公告，开始写入..." % count)
    n = 0
    for a in rows:
        try:
            upsert_announcement(a)
            n += 1
        except Exception as e:
            print("  ! 写入失败 %s/%s: %s" %
                  (a.get("bond_code"), a.get("announce_type"), e))
    print("[ann] 完成：成功写入 %d 条，耗时 %.1fs" % (n, time.time() - t0))


if __name__ == "__main__":
    main()
