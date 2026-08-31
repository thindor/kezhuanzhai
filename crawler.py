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
import time
import requests
from collections import defaultdict
from datetime import datetime

from config import EM_BASE, EM_HEADERS, THS_BASE
from db import get_conn, upsert_bond, delete_holders, insert_holders, compute_delist, \
    get_bonds_with_down_revise, get_active_trading_bonds, upsert_daily_close, get_daily_close, \
    get_bond, get_latest_quotes, save_double_low_snapshot, get_latest_double_low, get_double_low_change, _now_str, \
    get_redeemed_bond_codes, get_periods_info, save_stock_finance

# akshare 作为可转债事件（强赎/不强赎/下修）的兜底信息源（集思录接口在本机可用）。
# 导入失败时降级为空实现，不影响其它爬虫。
try:
    import akshare as ak
    _HAS_AKSHARE = True
except Exception:
    ak = None
    _HAS_AKSHARE = False


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
            # 上市日期：东财 RPT_BOND_CB_LIST 字段名候选，规范化为 YYYY-MM-DD
            ld = row.get("LISTING_DATE") or row.get("ONLIST_DATE") or row.get("LIST_DATE")
            if ld:
                ld = str(ld).strip()
                if len(ld) >= 10 and ld[4] == '-' and ld[7] == '-':
                    ld = ld[:10]
                elif len(ld) >= 8 and ld[:8].isdigit():
                    ld = "%s-%s-%s" % (ld[:4], ld[4:6], ld[6:8])
                else:
                    ld = None
            out.append({
                "bond_code": code,
                "bond_name": row.get("SECURITY_NAME_ABBR"),
                "stock_code": row.get("CONVERT_STOCK_CODE"),
                "stock_name": row.get("SECURITY_SHORT_NAME"),
                "listing_date": ld,
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
    # 机构特例白名单：名称偏短但确为机构的持有人（如央企集团简称），
    # 否则会被「纯中文 2-5 字」规则误判为个人。
    known_orgs = ["中广核", "中广核集团", "中广核资本", "中广核风电"]
    if name in known_orgs:
        return "一般机构"
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


def crawl_bond(code, use_ths=True):
    """抓取并入库一只转债的十大持有人。返回结果摘要 dict。

    use_ths: 是否调用同花顺 F10 校正最新一期持有人性质（best-effort）。
             单只手动抓取时保持 True；批量全量抓取时为避免对每只债都打同花顺
             触发限流/封禁，传 False（性质改用名称规则 classify_nature 推断）。
    """
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

    # 同步刷新历史收盘价：此前单只更新只刷持有人、漏了行情，导致详情页图表不更新。
    # 已退市债不再更新行情（与详情页「冻结」口径一致）；存量不足 30 个交易日则补全历史。
    if not (basic and basic.get("is_delisted")):
        try:
            existing = get_daily_close(core, 250)
            fetch_daily_one(core, basic.get("stock_code"), history=(len(existing) < 30))
        except Exception as e:
            print("[daily] 单只行情同步失败 %s: %s" % (core, e))

    if not holders_raw:
        return {"ok": False,
                "message": "未获取到十大持有人数据，请确认代码 %s 是否为已上市可转债" % core,
                "bond_code": core}

    # 按报告期分组
    by_period = defaultdict(list)
    for h in holders_raw:
        by_period[h["report_period"]].append(h)

    # 同花顺性质校正（仅最新一期 best-effort）；批量抓取时关闭以免触发限流/封禁
    nat_map = _ths_nature_map(core) if use_ths else {}

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


def expected_report_period(dt=None):
    """返回当前应已披露的定期报告期（'YYYY-MM-DD'）。

    取离今天最近、且披露截止日（季末 + 约 30 天宽限）已过的报告期：
      - 中报 06-30  ：宽限至 08-01 起预期（截止 08-31，多数公司已披露）
      - 一季报 03-31：宽限至 05-01 起预期（截止 04-30）
      - 年报 12-31  ：宽限至次年 02-01 起预期（截止次年 04-30）
      - 三季报 09-30：宽限至 11-01 起预期（截止 10-31）
    options 已按时间倒序，第一个命中窗口的即「最新应披露期」。
    这样在中报季(8月)会预期 2026-06-30，触发滞后债增量重抓；数据未出者靠每日重试自愈。
    """
    d = dt or datetime.now()
    y, m, day = d.year, d.month, d.day
    options = [
        ((y, 9, 30), (11, 1)),     # 三季报
        ((y, 6, 30), (8, 1)),      # 中报
        ((y, 3, 31), (5, 1)),      # 一季报
        ((y - 1, 12, 31), (2, 1)),  # 年报（次年 2 月起）
    ]
    best = None
    for period, (em, ed) in options:
        if (m, day) >= (em, ed):
            best = period
            break
    if best is None:
        best = (y - 1, 9, 30)  # 年初未到 2/1，退而求其次看上年三季报
    return "%04d-%02d-%02d" % best


def refresh_holders_for_bond(code, use_ths=False):
    """仅刷新一只转债的十大持有人（不碰行情/基础，用于批量增量更新）。

    与 crawl_bond 的区别：不回写 bonds 现价/不补历史行情，只 delete+insert holders。
    若东方财富返回的最新报告期 <= 库内最新期，视为无变化直接跳过，避免无谓写库与请求。
    返回 summary dict（含 changed / latest 字段，供批量统计）。
    """
    core = re.sub(r"\.(SZ|SH)$", "", (code or "").strip().upper())
    if not re.fullmatch(r"\d{6}", core):
        return {"ok": False, "bond_code": code, "message": "转债代码格式不正确"}
    secucode = _secucode(core)
    holders_raw = fetch_holders(secucode)
    if not holders_raw:
        return {"ok": False, "bond_code": core, "message": "未获取到十大持有人数据", "skipped": True}
    by_period = defaultdict(list)
    for h in holders_raw:
        by_period[h["report_period"]].append(h)
    max_period = max(by_period.keys())
    # 库内当前最新期
    pinfo = get_periods_info(core)
    db_latest = pinfo[0]["period"] if pinfo else ""
    if max_period <= db_latest:
        return {"ok": True, "bond_code": core, "changed": False,
                "message": "已是最新(%s)，跳过" % db_latest}
    # 仅最新一期做同花顺性质校正（best-effort）；批量时关闭以免触发限流/封禁
    nat_map = _ths_nature_map(core) if use_ths else {}
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
    delete_holders(core)
    insert_holders(holder_rows)
    return {"ok": True, "bond_code": core, "changed": True, "latest": max_period,
            "message": "已更新至 %s（%d 期/%d 条）" % (max_period, len(by_period), len(holder_rows))}


def refresh_holders_stale(limit=None, sleep_sec=0.3):
    """增量刷新持有人：仅对「最新报告期 < 当前应披露期」的活跃（未退市）转债重抓。

    - 自动跳过已退市债（与『退市债不再更新』口径一致）。
    - limit：单次最多处理几只（用于每日管道限速，避免单轮爆破东方财富）；None=不限制（手动全量）。
    - 数据未出的债（如中报尚未在东方财富放出）本次 latest 仍 < expected，下一轮继续重试直至补齐。
    返回汇总 dict。
    """
    expected = expected_report_period()
    bonds = get_active_trading_bonds()  # 仅未退市
    target = []
    for b in bonds:
        code = b["bond_code"]
        pinfo = get_periods_info(code)
        latest = pinfo[0]["period"] if pinfo else ""
        if latest < expected:
            target.append((code, latest))
    if limit:
        target = target[:limit]
    done = changed = skipped = 0
    for code, latest in target:
        try:
            res = refresh_holders_for_bond(code, use_ths=False)
            if res.get("changed"):
                changed += 1
                print("[holders] %s -> %s" % (code, res.get("message")))
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            print("[holders] %s 失败: %s" % (code, e))
        done += 1
        if sleep_sec:
            time.sleep(sleep_sec)
    print("[holders] 完成：expected=%s 待处理=%d 实际=%d 更新=%d 跳过=%d"
          % (expected, len(target) if limit is None else limit, done, changed, skipped))
    return {"expected": expected, "target": len(target), "done": done,
            "changed": changed, "skipped": skipped}


# ---------------- 历史下修记录（集思录 adj_logs，免费匿名） ----------------
def fetch_down_revise(code):
    """采集某转债的历史下修记录。

    主源：集思录单只转债 HTML（adj_logs）；兜底：akshare 集思录接口
    （bond_cb_adj_logs_jsl）。主源为空/失败时自动回退兜底源。
    返回 (count, records)，records 字段：bond_name, meeting_date,
    price_before, price_after, effective_date, floor_price。
    """
    count, records = _fetch_down_revise_jsl_html(code)
    if count == 0:
        c2, recs2 = _fetch_down_revise_akshare(code)
        if c2:
            return c2, recs2
    return count, records


def _fetch_down_revise_jsl_html(code):
    """主源：集思录单只转债接口 https://www.jisilu.cn/data/cbnew/adj_logs/?bond_id=CODE
    该表列名即「下修前/后转股价、下修底价」，天然是下修语义（不含分红类调整）。
    返回 (count, records)；无记录或无效代码返回 (0, [])。
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


def _fetch_down_revise_akshare(code):
    """兜底：akshare 集思录单债转股价调整记录（bond_cb_adj_logs_jsl）。
    返回 (count, records)；字段与 _fetch_down_revise_jsl_html 对齐。
    """
    if not _HAS_AKSHARE:
        return 0, []
    try:
        df = ak.bond_cb_adj_logs_jsl(symbol=str(code))
    except Exception as e:
        print("[dr] akshare 下修兜底失败 %s: %s" % (code, e))
        return 0, []
    records = []
    for _, row in df.iterrows():
        try:
            records.append({
                "bond_name": str(row.get("转债名称") or ""),
                "meeting_date": str(row.get("股东大会日") or ""),
                "price_before": _to_float(row.get("下修前转股价")),
                "price_after": _to_float(row.get("下修后转股价")),
                "effective_date": str(row.get("新转股价生效日期") or ""),
                "floor_price": _to_float(row.get("下修底价")),
            })
        except Exception:
            continue
    return len(records), records


# ---------------- 可转债公告（东财全量驱动，覆盖 7 类事件/状态） ----------------
def _to_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_date(s):
    if not s:
        return None
    s = str(s).strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _fmt(d):
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def _market_of(code):
    code = str(code or "")
    if code.startswith("11"):
        return "SH"
    return "SZ"


def _exchange_announce_url(code):
    """跳转到对应交易所的可转债公告披露板块（官方地址）。"""
    if _market_of(code) == "SH":
        return "https://www.sse.com.cn/disclosure/bond/convertible/"
    return "http://www.szse.cn/disclosure/bond/convertible/index.html"


def _em_notice_center(stock_code):
    """东方财富个股公告中心：列出该正股全部公告，点进去即正文。"""
    return "https://data.eastmoney.com/notices/stock/%s.html" % str(stock_code)


def _official_url_for(bond_code, stock_code):
    """优先链到东方财富个股公告中心（能直接看正文）；无正股代码时退回交易所板块。"""
    if stock_code:
        return _em_notice_center(stock_code)
    return _exchange_announce_url(bond_code)


def _is_delisted(d):
    dd = _parse_date(d.get("DELIST_DATE"))
    if dd and dd < datetime.now().date():
        return True
    return False


def _fmt2(v):
    try:
        return "%.2f" % float(v)
    except Exception:
        return str(v)


def _safe_float_str(v):
    """把可能是 NaN 的数值安全格式化为字符串；NaN/空返回 None。"""
    try:
        f = float(v)
    except (ValueError, TypeError):
        return None
    if f != f:  # NaN
        return None
    return "%.2f" % f


def fetch_cb_redeem_akshare():
    """用 akshare 集思录强赎表取真实强赎事件（非价格推算）。

    集思录「强赎状态」字段给出权威事件状态：
      - 已公告强赎 -> 强赎
      - 公告不强赎 -> 不强赎
    其余（空 / 计数中）非事件，跳过。
    返回 [dict(announce_type in {'强赎','不强赎'}, ...)]。
    """
    out = []
    if not _HAS_AKSHARE:
        return out
    try:
        df = ak.bond_cb_redeem_jsl()
    except Exception as e:
        print("[ann] akshare 强赎采集失败：%s" % e)
        return out

    def mk(code, name, atype, row):
        sc = str(row.get("正股代码") or "")
        url = _official_url_for(code, sc)
        price_s = _safe_float_str(row.get("强赎价"))
        cnt = re.sub(r"<[^>]+>", "", str(row.get("强赎天计数") or "")).strip()
        if atype == "强赎":
            title = "%s 已公告强赎（强赎价 %s，强赎计数 %s）" % (
                name, price_s or "", cnt or "")
        else:
            title = "%s 公告不强赎（强赎计数 %s）" % (name, cnt or "")
        return {"bond_code": code, "bond_name": name, "announce_type": atype,
                "title": title,
                "announce_date": datetime.now().strftime("%Y-%m-%d"),
                "source": "集思录", "official_url": url}

    for _, row in df.iterrows():
        status = str(row.get("强赎状态") or "").strip()
        code = str(row.get("代码") or "").strip()
        name = str(row.get("名称") or "").strip()
        if not code or not name:
            continue
        if status == "已公告强赎":
            out.append(mk(code, name, "强赎", row))
        elif status == "公告不强赎":
            out.append(mk(code, name, "不强赎", row))
    print("[ann] akshare 强赎/不强赎：强赎 %d / 不强赎 %d" % (
        sum(1 for a in out if a["announce_type"] == "强赎"),
        sum(1 for a in out if a["announce_type"] == "不强赎")))
    return out


def fetch_upcoming_bonds(code2stock=None):
    """采集「即将发行」可转债：东财 RPT_BOND_CB_LIST 按申购日倒序取最新一批，
    筛选尚未上市（LISTING_DATE 为空）的债。单次调用，失败不影响其它类别。
    返回 [dict, ...]（announce_type=即将发行）。official_url 优先个股公告中心。
    """
    code2stock = code2stock or {}
    out = []
    params = {
        "reportName": "RPT_BOND_CB_LIST",
        "columns": "ALL",
        "pageSize": 100,
        "sortColumns": "PUBLIC_START_DATE",
        "sortTypes": "-1",
        "source": "WEB", "client": "WEB", "p": 1,
    }
    for _ in range(3):
        try:
            r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=15)
            j = r.json()
            batch = (j.get("result") or {}).get("data") or []
            break
        except Exception:
            batch = []
            time.sleep(2)
    for d in batch:
        code = d.get("SECURITY_CODE")
        if not code:
            continue
        name = d.get("SECURITY_NAME_ABBR") or code
        # 仅纳入尚未上市（LISTING_DATE 为空）的债
        listing = _parse_date(d.get("LISTING_DATE"))
        if listing is not None:
            continue
        pub = _parse_date(d.get("PUBLIC_START_DATE"))
        url = _official_url_for(code, code2stock.get(code))
        title = "%s 即将发行（申购日 %s，尚未上市）" % (name, _fmt(pub) or "—")
        out.append({"bond_code": code, "bond_name": name, "announce_type": "即将发行",
                    "title": title,
                    "announce_date": _fmt(pub) or datetime.now().strftime("%Y-%m-%d"),
                    "source": "东财", "official_url": url})
    print("[ann] 即将发行可转债数量：%d" % len(out))
    return out


def fetch_announcements():
    """采集全市场可转债公告（真实事件，非价格推算）。

    数据源：
      - 即将发行：东方财富 RPT_BOND_CB_LIST 申购日历（未上市债）。
      - 强赎 / 不强赎：akshare 集思录强赎表（强赎状态=已公告强赎 / 公告不强赎），真实事件。
      - 下修：本地 bonds 表 down_revise_json（集思录历史，采集时主源+akshare 兜底）。
    按 (bond_code, announce_type) 维度产出（调用方 upsert 去重）。
    返回 (count, rows)。
    """
    import json as _json
    out = []
    seen = set()
    today = datetime.now().date()

    # 交易信号标记：下修=买入信号，强赎=持仓离场信号；不强赎偏利好(买入)，
    # 即将发行=中性观察。可按需要调整。
    def _sig(atype):
        return {"下修": "buy", "强赎": "sell", "不强赎": "buy",
                "即将发行": "neutral"}.get(atype, "neutral")

    # 预载 转债代码 -> 正股代码 映射（用于把「查看原文」链到东财个股公告中心）
    code2stock = {}
    try:
        _rows = _load_bond_stock_map()
        code2stock = {r[0]: r[1] for r in _rows if r[1]}
    except Exception:
        code2stock = {}

    # 0) 即将发行（东财申购日历，单独调用，失败不影响其它类别）
    try:
        for a in fetch_upcoming_bonds(code2stock):
            a.setdefault("signal", _sig(a["announce_type"]))
            key = (a["bond_code"], a["announce_type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    except Exception as e:
        print("[ann] 即将发行采集失败：%s" % e)

    # 1) akshare 集思录强赎表：强赎（已公告强赎）/ 不强赎（公告不强赎），真实事件状态
    try:
        for a in fetch_cb_redeem_akshare():
            a.setdefault("signal", _sig(a["announce_type"]))
            key = (a["bond_code"], a["announce_type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    except Exception as e:
        print("[ann] 强赎/不强赎采集失败：%s" % e)

    # 2) 本地 bonds 表：已下修（集思录历史），best-effort 精确定位下修公告正文
    try:
        rows = get_bonds_with_down_revise()
    except Exception as e:
        print("[ann] 读取本地下修历史失败：%s" % e)
        rows = []
    for b in rows:
        code = b["bond_code"]
        name = b["bond_name"] or code
        stock_code = b.get("stock_code")
        url = _official_url_for(code, stock_code)
        dj = b.get("down_revise_json")
        try:
            hist = _json.loads(dj) if dj else []
        except Exception:
            hist = []
        last = hist[-1] if hist else None
        if last:
            md = last.get("meeting_date") or last.get("effective_date") or "—"
            title = "%s 已下修（最近一次：%s 转股价 %s → %s）" % (
                name, md, last.get("price_before"), last.get("price_after"))
        else:
            title = "%s 有下修历史" % name
        key = (code, "下修")
        if key in seen:
            continue
        seen.add(key)
        out.append({"bond_code": code, "bond_name": name, "announce_type": "下修",
                    "title": title, "announce_date": today.strftime("%Y-%m-%d"),
                    "source": "集思录", "official_url": url,
                    "signal": _sig("下修")})
    return len(out), out


def _load_bond_stock_map():
    """返回 [(bond_code, stock_code), ...]，供公告链接映射到东财个股公告中心。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bond_code, stock_code FROM bonds "
                "WHERE stock_code IS NOT NULL AND TRIM(stock_code) <> ''")
    rows = cur.fetchall()
    conn.close()
    return rows


# ============ 每日收盘价采集（新浪历史日K，直连稳定） ============

def _sina_symbol(code):
    """转债/正股代码加市场前缀（新浪日K格式：sh113050 / sz123111 / sh600745 / sz000723）。
    转债：11xxxx=沪、12xxxx=深；正股：6/9/5 开头=沪，0/2/3 开头=深。
    注意：不能用 _market_of（仅识别转债前缀），否则沪市正股会被误标为 sz 导致取不到数据。"""
    code = str(code or "").strip()
    if code.startswith("11"):
        m = "sh"
    elif code.startswith("12"):
        m = "sz"
    elif code[:1] in ("6", "9", "5"):
        m = "sh"
    else:
        m = "sz"
    return m + code


def _secid(code):
    """把债券/股票代码转成东方财富 kline 的 secid（市场.代码）。
    债券：11/13 开头→沪(1.)，12 开头→深(0.)；股票：6/9 开头→沪，其余→深。"""
    s = code[:2]
    if s in ("11", "13"):
        return "1." + code
    if s == "12":
        return "0." + code
    if code[0] in ("6", "9"):
        return "1." + code
    return "0." + code


_EM_REACHABLE = None  # None=尚未探测; True/False=东财 kline 接口是否可达（探测一次后缓存）


def _probe_eastmoney_reachable():
    """探测东财 kline 接口是否可达（仅首次调用时实际探测，之后复用缓存）。

    背景：部分运行环境（如带失效本地代理、或数据中心 IP 被东财 reset 的沙箱）
    东财主源根本连不通。原实现会对每只债空跑 3 次重试+退避再降级新浪，
    316 只债累计浪费约 20+ 分钟。此处首个调用时探测一次：不可达则后续
    直接走新浪兜底，省掉每只债的死循环。生产环境东财可达时返回 True，行为不变。
    """
    global _EM_REACHABLE
    if _EM_REACHABLE is not None:
        return _EM_REACHABLE
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "secid": "1.000001",  # 用上证指数探活，避免依赖个债数据
        "beg": 0, "end": "20500101", "lmt": 1,
        "ut": "fa5fd1943c7b386f172d6893dbfbaa15",
    }
    try:
        r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        d = r.json()
        reachable = bool((d.get("data") or {}).get("klines"))
    except Exception:
        reachable = False
    _EM_REACHABLE = reachable
    if not reachable:
        print("[daily] 东财 kline 接口不可达（代理/网络被拦），本轮行情采集直连新浪兜底")
    return reachable


def fetch_sina_kline(code, datalen=320):
    """日K线收盘价，返回 list[(trade_date, close)] 升序。

    主源：东方财富 push2his kline（直连稳定、转债与正股均支持、数据新鲜，
    到当日）；兜底：新浪 CN_MarketData（偶发限流时回退）。
    注意：新浪行情源自 2026-08 起频繁限流返回空，故以东财为主。
    """
    secid = _secid(code)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": 1, "secid": secid,
        "beg": 0, "end": "20500101", "lmt": datalen,
        "ut": "fa5fd1943c7b386f172d6893dbfbaa15",
    }
    out = []
    # 东财偶发限流返回空，重试 2 次(退避)以提升采集可靠性；
    # 先经一次可达性探测：不可达环境(如沙箱)直接跳过，省去每只债的死循环重试
    if _probe_eastmoney_reachable():
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                d = r.json()
                kd = d.get("data") or {}
                klines = kd.get("klines") or []
                for kl in klines:
                    parts = kl.split(",")
                    if len(parts) >= 3:
                        try:
                            out.append((parts[0], float(parts[2])))
                        except (ValueError, TypeError):
                            continue
                if out:
                    return out
            except Exception:
                pass
            if attempt < 2:
                time.sleep(1.2)
    # 兜底：新浪
    try:
        sym = _sina_symbol(code)
        r = requests.get(
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
            params={"symbol": sym, "scale": 240, "ma": "no", "datalen": datalen},
            timeout=15, proxies={"http": None, "https": None})
        arr = r.json()
        return [(it["day"], float(it["close"])) for it in arr]
    except Exception:
        return []


def fetch_daily_all(history=False):
    """采集所有在交易转债及其正股的每日收盘价，写入 daily_close。
    history=True 强制全部补全历史(320交易日)；
    history=False (默认) 自动策略：本地存量<30交易日的债自动补全历史(320)，
    其余增量(最近10日)。这样新债首次进系统时无需手动 --history 也能自愈，
    且对存量债无额外开销。每只转债用同一连接、一个事务提交（而非逐行 commit），
    避免海量 fsync 与锁竞争。返回 (成功数, 总债数)。"""
    bonds = get_active_trading_bonds()
    total = len(bonds)
    ok = 0
    conn = get_conn()
    cur = conn.cursor()

    # 预判各债是否需要补全历史（history=True 时全部强制 320，无需判断）
    need_history = {}
    if not history:
        cur.execute("SELECT bond_code, COUNT(*) FROM daily_close GROUP BY bond_code")
        for code, cnt in cur.fetchall():
            if cnt < 30:
                need_history[code] = True

    for i, b in enumerate(bonds):
        code = b["bond_code"]
        sc = b.get("stock_code")
        now = _now_str()
        datalen = 320 if history else (320 if need_history.get(code) else 10)
        try:
            for d, c in fetch_sina_kline(code, datalen):
                # 跳过无收盘价的棒（如收盘前采集的当日 incomplete bar 返回空收盘价），
                # 否则会写入 NULL 毒化 backfill_current_price（详见 110097 等债）。
                if c is None or c <= 0:
                    continue
                cur.execute("INSERT OR IGNORE INTO daily_close(bond_code, trade_date, updated_at) VALUES(?,?,?)",
                            (code, d, now))
                cur.execute("UPDATE daily_close SET bond_close=?, updated_at=? WHERE bond_code=? AND trade_date=?",
                            (c, now, code, d))
            if sc:
                for d, c in fetch_sina_kline(sc, datalen):
                    if c is None or c <= 0:
                        continue
                    cur.execute("INSERT OR IGNORE INTO daily_close(bond_code, trade_date, updated_at) VALUES(?,?,?)",
                                (code, d, now))
                    cur.execute("UPDATE daily_close SET stock_close=?, updated_at=? WHERE bond_code=? AND trade_date=?",
                                (c, now, code, d))
            conn.commit()
            ok += 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            print("[daily] 采集失败 %s: %s" % (code, e))
        if (i + 1) % 50 == 0:
            print("[daily] 进度 %d/%d" % (i + 1, total))
        time.sleep(0.4)
    conn.close()
    return ok, total


def fetch_daily_one(code, stock_code=None, history=False):
    """采集【单只】转债及其正股的每日收盘价并写入 daily_close。

    供详情页「更新数据」按钮（crawl_bond 单只刷新）复用：此前单只更新只刷持有人、
    漏刷了历史收盘价，导致详情页图表不更新。本函数与 fetch_daily_all 共用
    fetch_sina_kline + upsert_daily_close，保证口径与全量采集完全一致。

    参数：
      stock_code: 正股代码；可不传，缺省时从本地 bonds 表取（首次抓取尚未入库时为 None）。
      history:    True 补全历史(320交易日)，False 仅增量(最近10日)。
    返回：(写入笔数, 说明)。
    """
    if stock_code is None:
        b = get_bond(code)
        stock_code = b.get("stock_code") if b else None
    datalen = 320 if history else 10
    n = 0
    try:
        for d, c in fetch_sina_kline(code, datalen):
            upsert_daily_close(code, d, bond_close=c)
            n += 1
    except Exception as e:
        print("[daily] 单只转债行情失败 %s: %s" % (code, e))
    if stock_code:
        try:
            for d, c in fetch_sina_kline(stock_code, datalen):
                upsert_daily_close(code, d, stock_close=c)
                n += 1
        except Exception as e:
            print("[daily] 单只正股行情失败 %s: %s" % (stock_code, e))
    return n, "ok"


# ============ 强赎预警（提前 >=5 个交易日） ============

def compute_redemption_warning(bond_code):
    """返回单只转债强赎预警 dict 或 None（不满足预警条件）。
    口径：滚动30交易日窗口内，正股收盘/转股价 >= 1.30 的天数 = satisfy_cnt。
    预警：10 <= satisfy_cnt < 15（即再 <=5 天达标，提前 >=5 交易日预警）。
    已满足(>=15)归公告模块强赎类，此处不计。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT current_transfer_price FROM bonds WHERE bond_code=?", (bond_code,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    tp = row[0]
    try:
        tp = float(tp)
    except (TypeError, ValueError):
        return None
    if not tp or tp <= 0:
        return None
    rows = get_daily_close(bond_code, 30)
    if not rows:
        return None
    cnt = sum(1 for r in rows if r.get("stock_close") and r["stock_close"] / tp >= 1.30)
    if 10 <= cnt < 15:
        remaining = 15 - cnt
        level = "urgent" if remaining <= 2 else ("high" if remaining <= 3 else "normal")
        last = rows[-1].get("stock_close")
        return {"satisfy_cnt": cnt, "remaining": remaining, "warn": True,
                "level": level, "ratio_latest": (last / tp) if last else None}
    return None


def compute_redemption_warnings():
    """返回全市场强赎预警 dict：{bond_code: warn_dict}，遍历在交易转债。"""
    res = {}
    for b in get_active_trading_bonds():
        w = compute_redemption_warning(b["bond_code"])
        if w:
            res[b["bond_code"]] = w
    return res


def get_redemption_warning_list():
    """返回强赎预警列表（含转债基础信息），按紧急度排序。
    排序：先按剩余天数 remaining 升序（最紧急在前），再按最新 ratio 降序。"""
    warns = compute_redemption_warnings()
    if not warns:
        return []
    codes = list(warns.keys())
    conn = get_conn()
    cur = conn.cursor()
    ph = ",".join("?" * len(codes))
    cur.execute(
        "SELECT bond_code, bond_name, stock_code, stock_name, "
        "current_transfer_price, rating FROM bonds WHERE bond_code IN (%s)" % ph,
        codes)
    info = {r["bond_code"]: dict(r) for r in cur.fetchall()}
    conn.close()
    rows = []
    for code, w in warns.items():
        base = info.get(code, {})
        rows.append({
            "bond_code": code,
            "bond_name": base.get("bond_name") or "",
            "stock_code": base.get("stock_code") or "",
            "stock_name": base.get("stock_name") or "",
            "transfer_price": base.get("current_transfer_price"),
            "rating": base.get("rating") or "",
            "satisfy_cnt": w["satisfy_cnt"],
            "remaining": w["remaining"],
            "level": w["level"],
            "ratio_latest": w["ratio_latest"],
        })
    rows.sort(key=lambda x: (x["remaining"], -(x["ratio_latest"] or 0)))
    return rows


# ============ 下修提醒（提前 >=5 个交易日） ============
# 通用条款：转股期内，正股任意连续 30 个交易日中至少有 15 个交易日收盘价
# 低于当期转股价的 85%（即 正股/转股价 <= 0.85）即触发下修条款，董事会「有权」提议下修。
# 不同转债条款比例/窗口有差异（常见 80%/85%/90% × 5/10/15 日 in 10/20/30 日窗口），
# 此处按市场最常见的 85%/15-30 测算，页面已注明「以各转债公告条款为准」。
DOWN_REVISE_TRIGGER_RATIO = 0.85   # 下修触发比例：正股低于转股价此比例即计入达标
DOWN_REVISE_WINDOW = 30            # 滚动窗口（交易日）
DOWN_REVISE_THRESHOLD = 15         # 触发所需达标天数


def compute_down_revise_warning(bond_code):
    """返回单只转债下修提醒 dict 或 None（不满足预警条件）。
    口径：滚动30交易日窗口内，正股收盘/转股价 <= 0.85 的天数 = satisfy_cnt。
    预警：10 <= satisfy_cnt < 15（再 <=5 天触发，提前 >=5 交易日提示），status='approaching'；
    已满足(>=15)归「已触发下修条件」类（status='triggered'），同样提示。
    下修为「有权提议」（偏利好），提示语与强赎风险区分。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT current_transfer_price FROM bonds WHERE bond_code=?", (bond_code,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    tp = row[0]
    try:
        tp = float(tp)
    except (TypeError, ValueError):
        return None
    if not tp or tp <= 0:
        return None
    rows = get_daily_close(bond_code, DOWN_REVISE_WINDOW)
    if not rows:
        return None
    cnt = sum(1 for r in rows
              if r.get("stock_close") and r["stock_close"] / tp <= DOWN_REVISE_TRIGGER_RATIO)
    last = rows[-1].get("stock_close")
    if cnt >= DOWN_REVISE_THRESHOLD:
        return {"satisfy_cnt": cnt, "remaining": 0, "warn": True,
                "level": "urgent", "ratio_latest": (last / tp) if last else None,
                "status": "triggered"}
    if 10 <= cnt < DOWN_REVISE_THRESHOLD:
        remaining = DOWN_REVISE_THRESHOLD - cnt
        level = "urgent" if remaining <= 2 else ("high" if remaining <= 3 else "normal")
        return {"satisfy_cnt": cnt, "remaining": remaining, "warn": True,
                "level": level, "ratio_latest": (last / tp) if last else None,
                "status": "approaching"}
    return None


def compute_down_revise_warnings():
    """返回全市场下修提醒 dict：{bond_code: warn_dict}，遍历在交易转债。"""
    res = {}
    for b in get_active_trading_bonds():
        w = compute_down_revise_warning(b["bond_code"])
        if w:
            res[b["bond_code"]] = w
    return res


def get_down_revise_warning_list():
    """返回下修提醒列表（含转债基础信息），按紧急度排序。
    triggered（已满足）排最前；approaching 按 remaining 升序（最紧急在前），
    同档按 ratio_latest 升序（正股相对转股价越低越接近触发）。"""
    warns = compute_down_revise_warnings()
    if not warns:
        return []
    codes = list(warns.keys())
    conn = get_conn()
    cur = conn.cursor()
    ph = ",".join("?" * len(codes))
    cur.execute(
        "SELECT bond_code, bond_name, stock_code, stock_name, "
        "current_transfer_price, rating FROM bonds WHERE bond_code IN (%s)" % ph,
        codes)
    info = {r["bond_code"]: dict(r) for r in cur.fetchall()}
    conn.close()
    rows = []
    for code, w in warns.items():
        base = info.get(code, {})
        rows.append({
            "bond_code": code,
            "bond_name": base.get("bond_name") or "",
            "stock_code": base.get("stock_code") or "",
            "stock_name": base.get("stock_name") or "",
            "transfer_price": base.get("current_transfer_price"),
            "rating": base.get("rating") or "",
            "satisfy_cnt": w["satisfy_cnt"],
            "remaining": w["remaining"],
            "level": w["level"],
            "ratio_latest": w["ratio_latest"],
            "status": w.get("status", "approaching"),
        })
    rows.sort(key=lambda x: (0 if x["status"] == "triggered" else 1,
                             x["remaining"], x["ratio_latest"] or 0))
    return rows


# ============ 双低策略 ============

def compute_double_low_list(topn=20):
    """计算双低值并排序取前 topn。

    双低值 = 转债价格 + 转股溢价率(%)   （经典双低公式，数值越低越优）
    转股价值 = 100 / 转股价 × 正股价
    转股溢价率(%) = (转债价格 / 转股价值 - 1) × 100

    价格取 daily_close 最新收盘价（兜底 bonds.current_price）；正股价取 daily_close 最新收盘价。
    仅纳入在交易转债；转股价/价格/正股价任一缺失或非法则跳过。
    已公告强赎的转债（announcements.announce_type='强赎'）视为离场信号，直接剔除——
    这样它们在轮动时会被调出（卖出），并由排名下一位的转债补入，保持 20 只。
    """
    res = []
    redeemed = get_redeemed_bond_codes()
    for b in get_active_trading_bonds():
        code = b["bond_code"]
        if code in redeemed:
            continue
        try:
            tp = float(b.get("current_transfer_price") or 0)
        except (TypeError, ValueError):
            continue
        if tp <= 0:
            continue
        g = get_bond(code)
        if not g:
            continue
        q = get_latest_quotes(code)
        # 转债价：仅采用 daily_close 最新收盘价。
        # ⚠️ 严格禁止从 bonds.current_price 兜底——新债/未上市债的 current_price 恒为 100.0
        # （占位面值），用它兜底会让未上市债以 100 元 + 任意溢价率被算进双低，
        # 进而被轮动误调入；正确做法是直接 skip（无成交价 = 不能买卖 = 不能进轮动）。
        bp = None
        try:
            if q.get("bond_close") is not None:
                bp = float(q["bond_close"])
        except (TypeError, ValueError):
            bp = None
        if not bp or bp <= 0:
            continue
        # 正股价
        sc = None
        try:
            if q.get("stock_close") is not None:
                sc = float(q["stock_close"])
        except (TypeError, ValueError):
            sc = None
        if not sc or sc <= 0:
            continue
        try:
            conv_value = 100.0 / tp * sc
            premium = (bp / conv_value - 1.0) * 100.0
            dl = bp + premium
        except (ZeroDivisionError, ValueError):
            continue
        if dl != dl or dl in (float("inf"), float("-inf")):
            continue
        res.append({
            "bond_code": code,
            "bond_name": g.get("bond_name"),
            "stock_name": g.get("stock_name"),
            "rating": g.get("rating"),
            "double_low": round(dl, 2),
            "bond_price": round(bp, 2),
            "premium_rate": round(premium, 2),
        })
    res.sort(key=lambda x: x["double_low"])
    return res[:topn]


def rotate_double_low():
    """每周轮动：计算当前前 20 双低，对比上次快照得出进入/调出，并写入 DB。

    返回 dict(week_start, prev_week_start, current, entered, exited)。
    week_start 取本周一日期（避免同一周内重复运行时产生多份快照）。
    """
    import datetime as _dt
    today = _dt.date.today()
    monday = today - _dt.timedelta(days=today.weekday())
    week_start = monday.strftime("%Y-%m-%d")
    current = compute_double_low_list(20)
    for i, r in enumerate(current, 1):
        r["rank"] = i
    prev = get_latest_double_low()
    prev_codes = {r["bond_code"] for r in prev["rows"]} if prev else set()
    cur_codes = {r["bond_code"] for r in current}
    entered = [r for r in current if r["bond_code"] not in prev_codes]
    exited = [r for r in prev["rows"] if r["bond_code"] not in cur_codes] if prev else []
    save_double_low_snapshot(week_start, current)
    return {
        "week_start": week_start,
        "prev_week_start": prev["week_start"] if prev else None,
        "current": current,
        "entered": entered,
        "exited": exited,
    }


# ============ 正股财务指标采集（高级筛选数据源） ============

def _secucode_stock(code):
    """正股 6 位代码 -> 带交易所后缀的 SECUCODE（6/9/5->.SH，其余->.SZ）。"""
    code = str(code or "").strip()
    if code.endswith((".SH", ".SZ", ".BJ")):
        return code
    if code[:1] in ("6", "9", "5"):
        return code + ".SH"
    return code + ".SZ"


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (ValueError, TypeError):
        return None


def collect_stock_finance():
    """东方财富 F10 主要财务指标 -> stock_finance 表（全量未退市债对应正股）。

    采集：总资产 / 总负债 / 有息负债率 / 总股本（每只是一只正股取最新一期报告）。
    资产负债率在本地由 总负债/总资产 现算（东财不直接给百分比，避免口径漂移）；
    有息负债率东财已为百分比（如 22.22 即 22.22%）。
    覆盖约 300 只正股，单次批量请求完成。返回写入条数；异常静默返回 0。"""
    try:
        conn = get_conn(); cur = conn.cursor()
        rows = cur.execute(
            "SELECT DISTINCT stock_code FROM bonds "
            "WHERE stock_code IS NOT NULL AND TRIM(stock_code)<>'' "
            "AND COALESCE(is_delisted,0)=0").fetchall()
        conn.close()
        plain = [r[0] for r in rows]
        if not plain:
            return 0
        # 正股代码 -> SECUCODE 映射（去重）
        sc2plain = {}
        for c in plain:
            sc2plain[_secucode_stock(c)] = c
        secucodes = list(sc2plain.keys())

        out = []
        for i in range(0, len(secucodes), 200):
            batch = secucodes[i:i + 200]
            f = ('(SECUCODE in (%s)) AND (REPORT_DATE >= \'2024-01-01\')'
                 % ",".join('"%s"' % s for s in batch))
            params = {
                "reportName": "RPT_F10_FINANCE_MAINFINADATA",
                "columns": "SECUCODE,SECURITY_CODE,REPORT_DATE,REPORT_DATE_NAME,"
                           "TOTAL_ASSETS_PK,LIABILITY,INTEREST_DEBT_RATIO,TOTAL_SHARE",
                "filter": f,
                "pageNumber": "1", "pageSize": "3000",
                "sortTypes": "-1", "sortColumns": "REPORT_DATE",
                "source": "HSF10", "client": "PC",
            }
            r = requests.get(EM_BASE, params=params, headers=EM_HEADERS, timeout=30)
            d = r.json()
            data = (d.get("result") or {}).get("data") or []
            seen = set()
            for row in data:
                sc = row.get("SECUCODE")
                if not sc or sc in seen:
                    continue
                seen.add(sc)  # sortTypes=-1 已按报告期倒序，首见即最新一期
                ta = _to_float(row.get("TOTAL_ASSETS_PK"))
                li = _to_float(row.get("LIABILITY"))
                idr = _to_float(row.get("INTEREST_DEBT_RATIO"))
                ts = _to_float(row.get("TOTAL_SHARE"))
                dr = round(li / ta * 100, 2) if (ta and li is not None) else None
                out.append({
                    "stock_code": sc2plain.get(sc, sc),
                    "report_date": row.get("REPORT_DATE"),
                    "report_label": row.get("REPORT_DATE_NAME"),
                    "total_assets": ta,
                    "total_liability": li,
                    "debt_ratio": dr,
                    "interest_debt_ratio": idr,
                    "total_share": ts,
                })
        return save_stock_finance(out)
    except Exception as e:
        print("[collect_stock_finance] ERR", type(e).__name__, str(e)[:200])
        return 0

