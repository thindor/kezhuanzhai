"""全市场可转债历史下修数据批量采集（带限速 + 断点续跑 + 限流保护）。

- 遍历 bonds 表全部转债（约 1042 只），对未采集过下修数据的逐一调用
  集思录 adj_logs 采集并写入 bonds 表（down_revise_count / down_revise_json）。
- 已采集过（down_revise_count 不为 None）的自动跳过，可反复运行做增量补全。
- 限速：每次请求间隔 1.5~3 秒随机；每 50 只额外休息 8 秒；降低集思录限流/验证码风险。
- 限流保护：连续失败达到 5 次即判定为被限流，自动停止，避免无意义轰炸。

用法：
  python seed_down_revise.py            # 全量
  python seed_down_revise.py 20         # 仅跑前 20 只（冒烟测试）
"""
import sqlite3
import time
import random
import sys

import crawler
from db import get_conn, get_down_revise_count, save_down_revise

DB_PATH = "cb_holders.db"


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bond_code FROM bonds ORDER BY bond_code")
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    if limit:
        codes = codes[:limit]
    total = len(codes)

    done = skip = fail = 0
    consecutive_fail = 0

    print(f"开始批量采集下修数据，共 {total} 只转债待处理", flush=True)
    for i, code in enumerate(codes, 1):
        # 断点续跑：已采集则跳过
        if get_down_revise_count(code) is not None:
            skip += 1
            continue
        try:
            c, recs = crawler.fetch_down_revise(code)
            save_down_revise(code, c, recs)
            done += 1
            consecutive_fail = 0
            print(f"[{i}/{total}] {code} -> {c} 次下修", flush=True)
        except Exception as e:
            fail += 1
            consecutive_fail += 1
            print(f"[{i}/{total}] {code} FAIL: {e}", flush=True)
            if consecutive_fail >= 5:
                print("连续失败达 5 次，疑似被集思录限流，停止采集。可稍后重跑（已采部分会跳过）。", flush=True)
                break
            time.sleep(random.uniform(5, 10))  # 失败后冷却更久
            continue

        # 分批节奏：每 50 只额外休息
        if i % 50 == 0:
            print(f"--- 已处理 {i}/{total}，小憩 8s ---", flush=True)
            time.sleep(8)
        time.sleep(random.uniform(1.5, 3.0))

    print(f"=== 采集完成：新采集 {done} 只 / 跳过已采 {skip} 只 / 失败 {fail} 只 ===", flush=True)


if __name__ == "__main__":
    main()
