# -*- coding: utf-8 -*-
"""全市场可转债基础数据播种脚本。

数据源：东方财富数据中心 RPT_BOND_CB_LIST（即 convertible_comparison 列表页背后的接口）。
拉取全市场可转债的基础/行情字段（代码、名称、正股、评级、现价、转股价、上市/到期日等），
批量 upsert 进 bonds 表，作为系统的「基础数据」：
  - 让代码/名称/简拼搜索覆盖全市场；
  - 让自然人市值估算有真实现价（而非按面值 100 兜底）；
  - 用户点开某只债时，若尚未抓过十大持有人，api_bond 会自动触发抓取。

仅写入 bonds 基础信息，不抓十大持有人（持有人保持按需爬取）。
"""
import requests
import time
from datetime import datetime

from db import upsert_bond, compute_delist

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
BASE = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_BOND_CB_LIST&columns=ALL"
        "&pageSize=500&sortColumns=SECURITY_CODE&sortTypes=1"
        "&source=WEB&client=WEB")


def _to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _to_date(v):
    if not v:
        return None
    return str(v)[:10]


def fetch_page(page_number):
    url = f"{BASE}&pageNumber={page_number}"
    resp = requests.get(url, timeout=20, headers=HEADERS).json()
    result = resp.get("result") or {}
    return result.get("pages", 1), result.get("data") or []


def main():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = 0
    with_price = 0
    listed = 0

    pages, _ = fetch_page(1)
    print(f"总页数: {pages}")

    for pn in range(1, pages + 1):
        _, data = fetch_page(pn)
        if not data:
            print(f"第 {pn} 页无数据，停止")
            break
        for d in data:
            code = d.get("SECURITY_CODE")
            if not code:
                continue
            price = _to_float(d.get("CURRENT_BOND_PRICE"))
            bond = {
                "bond_code": code,
                "bond_name": d.get("SECURITY_NAME_ABBR"),
                "stock_code": d.get("CONVERT_STOCK_CODE"),
                "stock_name": d.get("SECURITY_SHORT_NAME"),
                "rating": d.get("RATING"),
                "issue_scale": _to_float(d.get("ACTUAL_ISSUE_SCALE")),
                "listing_date": _to_date(d.get("LISTING_DATE")),
                "expire_date": _to_date(d.get("EXPIRE_DATE")),
                "current_transfer_price": _to_float(d.get("TRANSFER_PRICE")),
                "current_price": price,
                "data_source": "eastmoney_RPT_BOND_CB_LIST",
                "created_at": now,
                "updated_at": now,
            }
            is_delisted, delist_date = compute_delist(d)
            bond["is_delisted"] = is_delisted
            bond["delist_date"] = delist_date
            upsert_bond(bond)
            total += 1
            if price is not None:
                with_price += 1
            if bond["listing_date"]:
                listed += 1
        print(f"第 {pn}/{pages} 页完成：本页 +{len(data)}，累计 {total}（含现价 {with_price}）")
        if pn < pages:
            time.sleep(1.5)  # 慢慢爬，降低限流风险

    print(f"DONE 入库 {total} 只，其中含现价 {with_price} 只，有上市日 {listed} 只")


if __name__ == "__main__":
    main()
