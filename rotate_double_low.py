"""双低策略每周轮动：计算当前前 20 只双低转债，对比上周得出进入/调出并写入 DB。

用法：
  python rotate_double_low.py        # 执行一次轮动（幂等：同一周重复运行只更新当前快照）

数据源：daily_close（转债价/正股价）+ bonds（转股价/评级/正股名）。
建议通过 Windows 计划任务在每周一收盘后（如 16:40）运行一次。
"""

from db import init_db
import crawler


if __name__ == "__main__":
    init_db()
    res = crawler.rotate_double_low()
    print("[double-low] 轮动完成：周次=%s 前20只=%d 进入=%d 调出=%d" % (
        res["week_start"], len(res["current"]), len(res["entered"]), len(res["exited"])))
    print("[double-low] 进入:", ", ".join("%s(%s)" % (r["bond_code"], r["double_low"]) for r in res["entered"]) or "无")
    print("[double-low] 调出:", ", ".join("%s(%s)" % (r["bond_code"], r["double_low"]) for r in res["exited"]) or "无")
