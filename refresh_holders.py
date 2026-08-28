# -*- coding: utf-8 -*-
"""手动触发：增量刷新十大持有人（管理后台「持有人信息采集」按钮后端）。

只对滞后债重抓（库内最新报告期 < 当前应披露期），不限额（手动全量补齐）。
运行前后各统计一次 pending，写入 holder_refresh_status.json 供前端轮询进度。
"""
import json
import os
from datetime import datetime

import crawler
import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(BASE_DIR, "holder_refresh_status.json")


def count_pending(expected):
    """活跃（未退市）转债中，库内最新报告期 < 应披露期的只数。"""
    bonds = db.get_active_trading_bonds()
    n = 0
    for b in bonds:
        pinfo = db.get_periods_info(b["bond_code"])
        latest = pinfo[0]["period"] if pinfo else ""
        if latest < expected:
            n += 1
    return n


def _write(status):
    with open(STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expected = crawler.expected_report_period()
    pending_before = count_pending(expected)
    _write({
        "running": True,
        "started_at": started,
        "finished_at": None,
        "expected": expected,
        "pending_before": pending_before,
        "updated": 0,
        "pending_after": pending_before,
        "message": "持有人采集中…（待补齐 %d 只，预计 3-6 分钟）" % pending_before,
    })

    ret = crawler.refresh_holders_stale(limit=None, sleep_sec=0.3)
    pending_after = count_pending(expected)
    status = {
        "running": False,
        "started_at": started,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "expected": expected,
        "pending_before": pending_before,
        "updated": ret.get("changed", 0),
        "pending_after": pending_after,
        "message": "已完成：本次更新 %d 只，仍 %d 只待数据源放出（随定期报告披露后手动再触发即可补齐）"
                   % (ret.get("changed", 0), pending_after),
    }
    _write(status)
    print(status["message"])


if __name__ == "__main__":
    try:
        db.init_db()
    except Exception:
        pass
    main()
