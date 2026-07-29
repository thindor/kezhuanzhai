# -*- coding: utf-8 -*-
"""批量补全 bonds 表 current_price（可转债现价）。

RPT_BOND_CB_LIST 基本面接口不含实时现价，这里用腾讯行情（qt.gtimg.cn）批量补全：
  - 沪市可转债代码 11xxxx -> sh 前缀；深市 12xxxx -> sz 前缀；
  - 返回串按 ~ 分割，索引 3 为当前价格；
  - 未上市/退市债价格异常（0 或空），跳过，保持 NULL（市值估算兜底按 100）。
"""
import re
import sqlite3
import time

import requests

from config import DB_PATH

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BATCH = 80


def prefix(code):
    return ("sh" + code) if code.startswith("11") else ("sz" + code)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    codes = [r[0] for r in conn.execute(
        "SELECT bond_code FROM bonds WHERE bond_name IS NOT NULL")]
    total = len(codes)
    print(f"待补现价转债数: {total}")

    updated = 0
    skipped = 0
    line_re = re.compile(r'v_(\w+)="([^"]*)"')

    for i in range(0, total, BATCH):
        batch = codes[i:i + BATCH]
        q = ",".join(prefix(c) for c in batch)
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + q, timeout=20,
                             headers=HEADERS)
            r.encoding = "gbk"
        except Exception as e:
            print(f"第 {i // BATCH + 1} 批请求失败: {e}")
            time.sleep(1)
            continue

        for m in line_re.finditer(r.text):
            key, val = m.group(1), m.group(2)
            parts = val.split("~")
            if len(parts) < 4:
                continue
            code = key[2:]  # 去掉 sh/sz
            try:
                price = float(parts[3])
            except (TypeError, ValueError):
                continue
            if price <= 0:
                skipped += 1
                continue
            conn.execute(
                "UPDATE bonds SET current_price=? WHERE bond_code=?",
                (price, code))
            updated += 1
        conn.commit()
        print(f"第 {i // BATCH + 1} 批完成（累计更新 {updated}）")
        time.sleep(0.5)

    conn.close()
    print(f"DONE 更新现价 {updated} 只，跳过（无价/退市）{skipped} 只")


if __name__ == "__main__":
    main()
