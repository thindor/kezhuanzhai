"""可转债十大持有人爬虫。

数据源：
  1. 东方财富数据中心 RPT_BOND_CB_HOLDER  -> 各报告期十大持有人（权威、免费、含全历史）
  2. 东方财富数据中心 RPT_BOND_CB_LIST    -> 转债基础信息（名称/正股/评级/规模/到期）
  3. 同花顺 F10（best-effort）            -> 校正最新一期"持有人标识"性质

说明：
  - 东方财富接口返回 HOLD_NUM 单位为"张"，÷10000 转为"万张"。
  - 用 TYPE=2（合计行）过滤掉非持有人记录。
  - 性质（基金/一般机构/个人）东方财富不直接给，主要靠持有人名称规则推断；
    同花顺 F10 提供官方"持有人标识"，仅对最新一期做 best-effort 校正。
  - is_natural（自然人标记）：当 holder_nature == "个人" 时置 1，供后续筛选/统计使用。
"""
import re
import requests
from collections import defaultdict
from datetime import datetime

from config import EM_BASE, EM_HEADERS, THS_BASE
from db import upsert_bond, delete_holders, insert_holders, compute_delist


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _secucode(code):
    """6 位转债代码 -> 带交易所后缀的 SECUCODE（11x->.SH，12x->.SZ）。"""
    if code.endswith(".SZ") or code.endswith(".SH"):
        return code
    if code.startswith("11"):
        return code + ".SH"
    if code.startswith("12"):
        return code + ".SZ"
    # 兜底：深交所可转债多为 12 开头
    return code + ".SZ"


def fetch_bond_basic(code):
    params = {
        "reportName": "RPT_BOND_CB_LIST",
        "columns": "ALL",
        "filter": '(SECURITY_CODE="%s")' % code,
        "pageSize": 5,
        "source": "WEB",
        "client": "WEB",
        "p": 1,
    }
    try:
        r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=20)
        d = r.json()
    except Exception:
        return None
    if d.get("success") and d.get("result") and d["result"].get("data"):
        row = d["result"]["data"][0]
        is_delisted, delist_date = compute_delist(row)
        return {
            "bond_code": code,
            "bond_name": row.get("SECURITY_NAME_ABBR"),
            "stock_code": row.get("CONVERT_STOCK_CODE"),
            "stock_name": row.get("SECURITY_SHORT_NAME"),
            "rating": row.get("RATING"),
            "issue_scale": row.get("ACTUAL_ISSUE_SCALE"),
            "listing_date": (row.get("LISTING_DATE") or "")[:10],
            "expire_date": (row.get("EXPIRE_DATE") or "")[:10],
            "current_transfer_price": row.get("TRANSFER_PRICE") or row.get("TRANSFER_VALUE") or row.get("INITIAL_TRANSFER_PRICE"),
            "is_delisted": is_delisted,
            "delist_date": delist_date,
            "data_source": "东方财富数据中心",
        }
    return None


def fetch_all_transfer_prices():
    """批量取全市场转债的当前转股价。

    东方财富 RPT_BOND_CB_LIST 中 TRANSFER_PRICE 字段普遍为空，真正可用的转股价
    在 TRANSFER_VALUE（对未下修债即初始转股价）。此处优先 TRANSFER_VALUE，回退
    INITIAL_TRANSFER_PRICE，用于回填 bonds.current_transfer_price。

    返回 {bond_code: price(float|None)}。
    """
    out = {}
    page, page_size, max_pages = 1, 500, 8
    while page <= max_pages:
        params = {
            "reportName": "RPT_BOND_CB_LIST",
            "columns": "ALL",
            "pageSize": page_size,
            "source": "WEB",
            "client": "WEB",
            "p": page,
        }
        try:
            r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=15)
            d = r.json()
        except Exception:
            break
        if not (d.get("success") and d.get("result") and d["result"].get("data")):
            break
        batch = d["result"]["data"]
        if not batch:
            break
        for row in batch:
            code = row.get("SECURITY_CODE")
            if not code:
                continue
            tp = row.get("TRANSFER_PRICE") or row.get("TRANSFER_VALUE") or row.get("INITIAL_TRANSFER_PRICE")
            try:
                tp = float(tp) if tp is not None else None
            except (ValueError, TypeError):
                tp = None
            out[code] = tp
        if len(batch) < page_size:
            break
        page += 1
    return out


def fetch_all_bonds():
    """拉取全市场可转债列表（代码/名称/正股），用于搜索索引。

    调用 RPT_BOND_CB_LIST（不加过滤），按东方财富实际每页上限分页拉全量。
    返回 [{'bond_code','bond_name','stock_code','stock_name'}, ...]。

    注意：东方财富该接口单页最多返回 500 条（即使 pageSize 传更大也只给 500），
    故用 pageSize=500 分页，并在某页不足 500 条时判定为末页。
    """
    out = []
    seen = set()
    page = 1
    page_size = 500
    max_pages = 8  # 安全上限，避免接口异常时无限翻页
    while page <= max_pages:
        params = {
            "reportName": "RPT_BOND_CB_LIST",
            "columns": "ALL",
            "pageSize": page_size,
            "source": "WEB",
            "client": "WEB",
            "p": page,
        }
        try:
            r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=10)
            d = r.json()
        except Exception:
            break
        if not (d.get("success") and d.get("result") and d["result"].get("data")):
            break
        batch = d["result"]["data"]
        if not batch:
            break
        new_in_page = 0
        for row in batch:
            code = row.get("SECURITY_CODE")
            if not code or code in seen:
                continue
            seen.add(code)
            new_in_page += 1
            out.append({
                "bond_code": code,
                "bond_name": row.get("SECURITY_NAME_ABBR"),
                "stock_code": row.get("CONVERT_STOCK_CODE"),
                "stock_name": row.get("SECURITY_SHORT_NAME"),
            })
        # 整页无新增（分页死循环/重复数据）则停止
        if new_in_page == 0:
            break
        if len(batch) < page_size:
            break
        page += 1
    return out


def fetch_holders(secucode):
    rows = []
    page = 1
    while True:
        params = {
            "reportName": "RPT_BOND_CB_HOLDER",
            "columns": "ALL",
            "filter": '(SECUCODE="%s")' % secucode,
            "sortColumns": "END_DATE",
            "sortTypes": "-1",
            "pageSize": 1000,
            "source": "WEB",
            "client": "WEB",
            "p": page,
        }
        try:
            r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=20)
            d = r.json()
        except Exception:
            break
        if not (d.get("success") and d.get("result") and d["result"].get("data")):
            break
        batch = d["result"]["data"]
        for x in batch:
            if x.get("TYPE") == 2:  # 合计行，跳过
                continue
            rows.append({
                "report_period": (x.get("END_DATE") or "")[:10],
                "holder_name": x.get("HOLDER_NAME"),
                "hold_amount": (x.get("HOLD_NUM") or 0) / 10000.0,  # 张 -> 万张
                "hold_ratio": x.get("HOLD_RATIO"),
            })
        if len(batch) < 1000:
            break
        page += 1
    return rows


def classify_nature(name):
    """按持有人名称推断性质：基金 / 一般机构 / 个人 / 未知。"""
    fund_kw = ["基金", "ETF", "证券投资基金", "养老金产品", "资产管理计划",
               "资管计划", "FOF", "LOF", "理财计划", "集合资产管理",
               "私募证券投资基金", "指数增强", "债券型", "混合型", "股票型", "指数型"]
    org_kw = ["银行", "证券", "保险", "信托", "有限公司", "合伙企业", "合伙",
              "投资", "资产管理", "财务公司", "集团", "香港", "境外", "QFII",
              "公司", "理财", "资管"]
    if any(k in (name or "") for k in fund_kw):
        return "基金"
    if any(k in (name or "") for k in org_kw):
        return "一般机构"
    if re.fullmatch(r"[\u4e00-\u9fa5]{2,5}", name or ""):
        return "个人"
    return "未知"


def _ths_nature_map(code):
    """同花顺 F10 债券十大持有人页的"持有人标识"性质映射（仅最新一期，best-effort）。"""
    nat_map = {}
    try:
        url = THS_BASE.format(code=code)
        r = requests.get(url, headers={"User-Agent": EM_HEADERS["User-Agent"]}, timeout=20)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for table in soup.find_all("table"):
            txt = table.get_text()
            if "持有人名称" in txt and "持有人标识" in txt:
                for tr in table.find_all("tr")[1:]:
                    tds = tr.find_all("td")
                    if len(tds) >= 4:
                        name = tds[0].get_text(strip=True)
                        nature = tds[1].get_text(strip=True)
                        if name and nature in ("基金", "一般机构", "个人"):
                            nat_map[name] = nature
                break
    except Exception:
        pass
    return nat_map


def _fetch_price(code):
    """抓取转债现价（元/张），用于估算自然人持仓市值。

    主源：腾讯行情 qt.gtimg.cn；兜底：东方财富 push2；均失败返回 None，
    由调用方按面值 100 估算（保证任何环境都不报错）。
    """
    prefix = "sh" if code.startswith("11") else "sz"
    # 1) 腾讯行情
    try:
        r = requests.get("https://qt.gtimg.cn/q=%s%s" % (prefix, code),
                         headers={"User-Agent": EM_HEADERS["User-Agent"]}, timeout=8)
        text = r.content.decode("gbk", "ignore")
        # 格式：v_sz127061="1~名称~代码~现价~昨收~..."
        seg = text.split('"')
        if len(seg) >= 2:
            fields = seg[1].split("~")
            if len(fields) > 3 and fields[3]:
                return float(fields[3])
    except Exception:
        pass
    # 2) 东方财富 push2 兜底
    try:
        secid = ("1." if code.startswith("11") else "0.") + code
        r = requests.get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={"secid": secid, "fields": "f43", "invt": 2, "fltt": 2},
            headers={"User-Agent": EM_HEADERS["User-Agent"]}, timeout=8)
        d = r.json()
        f43 = d.get("data", {}).get("f43")
        if f43:
            return float(f43)
    except Exception:
        pass
    return None


def crawl_bond(code):
    """抓取并入库一只转债的十大持有人。返回结果摘要 dict。"""
    raw = (code or "").strip().upper()
    core = re.sub(r"\.(SZ|SH)$", "", raw)
    if not re.fullmatch(r"\d{6}", core):
        return {"ok": False,
                "message": "转债代码格式不正确，应为 6 位数字（可选带 .SZ/.SH）",
                "bond_code": raw}

    secucode = _secucode(core)
    basic = fetch_bond_basic(core)
    holders_raw = fetch_holders(secucode)
    price = _fetch_price(core)  # 现价（元/张），失败为 None -> 按面值估算

    if not holders_raw:
        return {"ok": False,
                "message": "未获取到十大持有人数据，请确认代码 %s 是否为已上市可转债" % core,
                "bond_code": core}

    # 按报告期分组
    by_period = defaultdict(list)
    for h in holders_raw:
        by_period[h["report_period"]].append(h)

    # 同花顺性质校正（仅最新一期 best-effort）
    nat_map = _ths_nature_map(core)

    now = _now()
    holder_rows = []
    for period, items in by_period.items():
        items_sorted = sorted(items, key=lambda x: x["hold_amount"], reverse=True)[:10]
        for i, it in enumerate(items_sorted, 1):
            name = it["holder_name"]
            nature = nat_map.get(name) or classify_nature(name)
            holder_rows.append({
                "bond_code": core,
                "report_period": period,
                "rank": i,
                "holder_name": name,
                "holder_nature": nature,
                "is_natural": 1 if nature == "个人" else 0,
                "hold_amount": round(it["hold_amount"], 2),
                "hold_ratio": it["hold_ratio"],
                "data_source": "东方财富数据中心",
                "fetched_at": now,
            })

    # 写库
    delete_holders(core)
    if basic:
        basic.update({"created_at": now, "updated_at": now, "current_price": price})
        upsert_bond(basic)
    else:
        upsert_bond({
            "bond_code": core, "bond_name": None, "stock_code": None, "stock_name": None,
            "rating": None, "issue_scale": None, "listing_date": None, "expire_date": None,
            "current_transfer_price": None, "current_price": price,
            "is_delisted": 0, "delist_date": None,
            "data_source": "东方财富数据中心",
            "created_at": now, "updated_at": now,
        })
    insert_holders(holder_rows)

    bond_name = basic["bond_name"] if basic else core
    return {
        "ok": True,
        "bond_code": core,
        "bond_name": bond_name,
        "periods": len(by_period),
        "holders": len(holder_rows),
        "message": "成功抓取 %d 个报告期、共 %d 条持有人记录" % (len(by_period), len(holder_rows)),
    }


# ---------------- 历史下修记录（集思录 adj_logs，免费匿名） ----------------
def fetch_down_revise(code):
    """采集某转债的历史下修记录（转股价格调整记录中的「下修」部分）。

    数据源：集思录单只转债接口 https://www.jisilu.cn/data/cbnew/adj_logs/?bond_id=CODE
    该表列名即「下修前/后转股价、下修底价」，天然是下修语义（不含分红类调整）。

    返回 (count, records)：
      count   : 下修次数（表格行数）
      records : list[dict]，每条含
                bond_name, meeting_date(股东大会日/下修提议审议日),
                price_before, price_after, effective_date(新转股价生效日期),
                floor_price(下修底价)
    无记录或无效代码返回 (0, [])。
    """
    import html as _html
    import re as _re
    url = "https://www.jisilu.cn/data/cbnew/adj_logs/?bond_id=%s" % code
    headers = {
        "User-Agent": EM_HEADERS.get("User-Agent",
                                     "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"),
        "Referer": "https://www.jisilu.cn/",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        data = r.text
    except Exception:
        return 0, []
    if "暂无数据" in data or "无效代码" in data:
        return 0, []
    rows = _re.findall(r"<tr[^>]*>(.*?)</tr>", data, _re.S)
    records = []
    for tr in rows:
        cells = _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, _re.S)
        cells = [_html.unescape(_re.sub(r"<[^>]+>", "", c).strip()) for c in cells]
        if not cells or cells[0] == "转债名称":
            continue
        if len(cells) < 6:
            continue
        try:
            records.append({
                "bond_name": cells[0],
                "meeting_date": cells[1],
                "price_before": float(cells[2]) if cells[2] else None,
                "price_after": float(cells[3]) if cells[3] else None,
                "effective_date": cells[4],
                "floor_price": float(cells[5]) if cells[5] else None,
            })
        except (ValueError, IndexError):
            continue
    return len(records), records
