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
from db import upsert_bond, delete_holders, insert_holders, compute_delist, \
    get_bonds_with_down_revise


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


def _is_delisted(d):
    dd = _parse_date(d.get("DELIST_DATE"))
    if dd and dd < datetime.now().date():
        return True
    return False


def _classify_quote(q, today):
    """根据行情板数据（现价 f2 + 转股溢价率 f24）推算公告类型。

    ratio = 正股 / 转股价 = 转股价值 / 100 = f2 / (100 + f24)。
    行情板不返回正股价 / 赎回公告细分，故「强赎」表示已满足强赎条件
    （无法区分公司后续是否公告不强赎）；「下修」由本地 bonds 表历史补充。
    返回 [dict, ...]。
    """
    code = q.get("code")
    name = q.get("name") or code
    price = _to_float(q.get("price"))
    premium = _to_float(q.get("premium"))  # 转股溢价率（%）
    url = _exchange_announce_url(code)
    if price is None or premium is None:
        return []
    denom = 100.0 + premium
    if denom <= 0:
        return []
    ratio = price / denom          # = 转股价值/100 = 正股/转股价
    cv = ratio * 100.0             # 转股价值
    ds = today.strftime("%Y-%m-%d")
    res = []

    def mk(atype, title):
        return {"bond_code": code, "bond_name": name, "announce_type": atype,
                "title": title, "announce_date": ds,
                "source": "东财行情", "official_url": url}

    if ratio >= 1.30:
        res.append(mk("强赎",
                      "%s 已满足强赎条件（转股价值约 %.0f，正股/转股价=%.2f），关注公司强赎/不强赎公告"
                      % (name, cv, ratio)))
    elif ratio >= 1.20:
        res.append(mk("临近强赎",
                      "%s 临近强赎（转股价值约 %.0f，正股/转股价=%.2f）"
                      % (name, cv, ratio)))
    elif ratio <= 0.82:
        res.append(mk("不下修",
                      "%s 已触发下修条件（正股/转股价=%.2f），关注公司是否下修"
                      % (name, ratio)))
    elif ratio <= 0.92:
        res.append(mk("提议下修",
                      "%s 价格已触发下修条件（正股/转股价=%.2f），关注董事会下修提议"
                      % (name, ratio)))
    elif ratio <= 0.98:
        res.append(mk("临近下修",
                      "%s 临近下修（正股/转股价=%.2f）" % (name, ratio)))
    return res


def _fmt2(v):
    try:
        return "%.2f" % float(v)
    except Exception:
        return str(v)


def fetch_cb_quotes():
    """分页拉取东财可转债板块（b:MK0354）全量行情：代码/名称/现价/转股溢价率。

    该行情接口分页（pn/pz）正常可用，覆盖全部活跃可转债，用作公告推算主数据。
    东财常对高频访问限速/封 IP，故单页失败重试 3 次（间隔 2s）以提升稳定性。
    """
    out = []
    pn = 1
    while pn <= 10:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {"pn": pn, "pz": 100, "po": "1", "np": "1", "fltt": "2",
                  "invt": "2", "fid": "f3", "fs": "b:MK0354",
                  "fields": "f12,f14,f2,f24", "_": int(time.time() * 1000)}
        j = None
        for _ in range(4):
            try:
                r = requests.get(url, params=params, headers=EM_HEADERS, timeout=15)
                j = r.json()
                break
            except Exception:
                time.sleep(5)
        if j is None:
            break
        data = (j.get("data") or {}).get("diff") or []
        if not data:
            break
        for d in data:
            code = d.get("f12")
            if not code:
                continue
            out.append({"code": str(code), "name": d.get("f14"),
                        "price": d.get("f2"), "premium": d.get("f24")})
        # 东财该接口单次最多返回 100 条：满 100 说明可能还有下一页
        if len(data) < 100:
            break
        pn += 1
        time.sleep(1.5)  # 页间延迟，降低连续请求被限流概率
    return out


def fetch_upcoming_bonds():
    """采集「即将发行」可转债：东财 RPT_BOND_CB_LIST 按申购日倒序取最新一批，
    筛选尚未上市（LISTING_DATE 为空）的债。单次调用，失败不影响其它类别。
    返回 [dict, ...]（announce_type=即将发行）。
    """
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
        url = _exchange_announce_url(code)
        title = "%s 即将发行（申购日 %s，尚未上市）" % (name, _fmt(pub) or "—")
        out.append({"bond_code": code, "bond_name": name, "announce_type": "即将发行",
                    "title": title,
                    "announce_date": _fmt(pub) or datetime.now().strftime("%Y-%m-%d"),
                    "source": "东财", "official_url": url})
    print("[ann] 即将发行可转债数量：%d" % len(out))
    return out


def fetch_announcements():
    """采集全市场可转债公告。

    数据源：
      - 东财行情板 b:MK0354（分页可用）：现价 + 转股溢价率 -> ratio，推算
        强赎 / 临近强赎 / 提议下修 / 临近下修 / 不下修。
      - 本地 bonds 表 down_revise_json（集思录历史）：已下修。
    按 (bond_code, announce_type) 维度产出（调用方 upsert 去重）。
    返回 (count, rows)。
    """
    import json as _json
    out = []
    seen = set()
    today = datetime.now().date()

    # 0) 即将发行（东财申购日历，单独调用，失败不影响其它类别）
    try:
        for a in fetch_upcoming_bonds():
            key = (a["bond_code"], a["announce_type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    except Exception as e:
        print("[ann] 即将发行采集失败：%s" % e)

    # 1) 行情板：强赎 / 临近强赎 / 提议下修 / 临近下修 / 不下修
    quotes = fetch_cb_quotes()
    print("[ann] 行情板可转债数量：%d" % len(quotes))
    for q in quotes:
        for a in _classify_quote(q, today):
            key = (a["bond_code"], a["announce_type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(a)

    # 2) 本地 bonds 表：已下修（集思录历史）
    try:
        rows = get_bonds_with_down_revise()
    except Exception as e:
        print("[ann] 读取本地下修历史失败：%s" % e)
        rows = []
    for b in rows:
        code = b["bond_code"]
        name = b["bond_name"] or code
        url = _exchange_announce_url(code)
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
                    "source": "集思录", "official_url": url})
    return len(out), out
