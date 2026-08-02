# -*- coding: utf-8 -*-
"""全市场可转债持有人全量抓取脚本（限流 / 断点续传 / 跳过冻结）。

用法：
    python crawl_all.py [--sleep 2.0] [--include-frozen] [--down-revise]

行为：
  - 遍历 bonds 表所有转债，逐只 crawl_bond(use_ths=False) 刷新/首抓十大持有人。
  - 跳过「已退市且已有持有人数据」的债（冻结规则，避免无效请求）；--include-frozen 可强制抓取。
  - 关闭同花顺 F10 性质校正（否则 1000+ 债必被封），性质用名称规则推断。
  - 每完成一只写入 _crawl_all_progress.txt（断点续传，进程中断后重跑自动跳过已完成）。
  - 进度/结果实时追加到 _crawl_all.log。
  - --down-revise：顺带抓取并写入历史下修记录（集思录，免费匿名；默认不抓，保持按需）。

说明：单线程串行 + 间隔 sleep，专门用于「慢慢抓、防限流」。直接前台或后台跑均可。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import crawler

REPO = os.path.dirname(os.path.abspath(__file__))
PROGRESS = os.path.join(REPO, "_crawl_all_progress.txt")
LOG = os.path.join(REPO, "_crawl_all.log")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleep", type=float, default=2.0, help="每只债抓取后的间隔秒数（限流）")
    ap.add_argument("--include-frozen", action="store_true", help="也抓取已退市且已有数据的债（默认跳过）")
    ap.add_argument("--down-revise", action="store_true", help="顺带抓取历史下修记录（集思录）")
    args = ap.parse_args()

    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bond_code, is_delisted FROM bonds")
    all_bonds = [(r[0], r[1]) for r in cur.fetchall()]
    cur.execute("SELECT DISTINCT bond_code FROM holders")
    held = set(r[0] for r in cur.fetchall())
    conn.close()

    delisted_set = set(c for c, d in all_bonds if int(d or 0) == 1)

    done = set()
    if os.path.exists(PROGRESS):
        with open(PROGRESS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    done.add(line)

    total = ok = skip = fail = 0
    logf = open(LOG, "a", encoding="utf-8")

    def logprint(s):
        print(s)
        logf.write(s + "\n")
        logf.flush()

    logprint("=== crawl_all start sleep=%.1f include_frozen=%s down_revise=%s bonds=%d done_before=%d ==="
             % (args.sleep, args.include_frozen, args.down_revise, len(all_bonds), len(done)))

    for code, d in all_bonds:
        if code in done:
            continue
        frozen = (code in delisted_set) and (code in held)
        if frozen and not args.include_frozen:
            skip += 1
            continue
        try:
            res = crawler.crawl_bond(code, use_ths=False)
            total += 1
            if res.get("ok"):
                ok += 1
                tag = "OK"
            else:
                fail += 1
                tag = "FAIL"
            msg = "[%s] %s %s" % (tag, code, res.get("message", ""))
            # 顺带抓下修（集思录，低风险）
            if args.down_revise and res.get("ok"):
                try:
                    cnt, recs = crawler.fetch_down_revise(code)
                    db.save_down_revise(code, cnt, recs)
                    msg += " | 下修=%d" % cnt
                except Exception as e:
                    msg += " | 下修ERR=%r" % e
        except Exception as e:
            fail += 1
            total += 1
            msg = "[ERR] %s %r" % (code, e)
        logprint(msg)
        with open(PROGRESS, "a", encoding="utf-8") as pf:
            pf.write(code + "\n")
        time.sleep(args.sleep)

    logprint("=== DONE total=%d ok=%d skip_frozen=%d fail=%d ===" % (total, ok, skip, fail))
    logf.close()


if __name__ == "__main__":
    main()
