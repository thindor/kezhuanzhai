"""可转债十大持有人查询系统 —— Flask 应用。

功能：
  - 首页：输入转债代码 -> 命中缓存则直接返回；未命中则自动爬取并入库。
          同时服务端渲染「自然人持有人榜」入口（SEO 友好）。
  - /persons：自然人持有人完整榜单（服务端渲染，分页）。
  - /person/<name>：某自然人持有的全部可转债明细（服务端渲染，结构化数据）。
  - /robots.txt、/sitemap.xml：SEO 抓取与收录支持。
  - 管理后台(/admin)：登录后批量/单独更新、新增、删除转债数据。
  - 账号：admin / admin888

运行：
  pip install -r requirements.txt
  python app.py
  访问 http://localhost:5000
"""
from flask import (Flask, request, render_template, session, redirect,
                   url_for, jsonify, abort)
from markupsafe import Markup
import re
import os
import sys
import json
import time
import threading
import subprocess
import urllib.parse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from config import ADMIN_USER, ADMIN_PASS, SECRET_KEY, CACHE_TTL_DAYS
from db import (init_db, get_bond, get_periods, get_periods_info, get_holders, list_bonds,
                list_market_bonds,
                get_all_natural_persons, count_natural_persons, get_person_holdings,
                get_person_market_value, get_person_latest_holdings,
                get_natural_ranking, get_down_revise, save_down_revise,
                get_down_revise_count, record_bond_view, get_recent_bonds,
                set_delisted, search_bonds,                 get_all_institutions,
                get_institution_ranking, count_institutions,
                upsert_announcement, get_announcements,
                get_bond_announcements,
                get_announcement_type_counts, clear_announcements,                 get_daily_close,
                get_double_low_change,
                get_double_low_history,
                get_double_low_holds,
                compute_market_overview, get_price_trend, get_new_bonds,
                get_site_settings, save_site_settings,
                get_latest_data_date, get_redeemed_bond_codes,
                get_collect_runs, get_collect_steps, get_running_collect_run,
                recover_stale_runs)
import db
import crawler
import checkup
import mini_bond

app = Flask(__name__)
app.secret_key = SECRET_KEY

init_db()


@app.context_processor
def inject_site():
    """将站点设置（名称 / logo / 域名）注入所有模板，后台改完即时生效。"""
    s = get_site_settings()
    return {
        "site_name": s["site_name"],
        "site_logo": s["site_logo"],
        "site_domain": s["site_domain"],
    }
# 小盘债模块：幂等补齐 bonds 表所需列（赎回价 / 历史最高缓存）
try:
    mini_bond.ensure_columns()
except Exception:
    pass


# ---- 搜索索引：支持代码 / 名称 / 简拼 ----
try:
    from pypinyin import lazy_pinyin, Style
    _HAS_PINYIN = True
except Exception:
    _HAS_PINYIN = False

_INDEX = None
_INDEX_TS = 0
_INDEX_TTL = 24 * 3600
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_INDEX_FILE = os.path.join(_BASE_DIR, "bond_index.json")
_index_lock = threading.Lock()


def _make_pinyin(text):
    if not _HAS_PINYIN or not text:
        return ""
    try:
        return "".join(lazy_pinyin(text, style=Style.FIRST_LETTER)).lower()
    except Exception:
        return ""


def _db_bonds():
    """本地已入库转债（bond_name 非空），作为搜索索引的兜底来源。"""
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT bond_code, bond_name, stock_code, stock_name "
                    "FROM bonds WHERE bond_name IS NOT NULL")
        rows = [{"bond_code": r[0], "bond_name": r[1],
                 "stock_code": r[2], "stock_name": r[3]} for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _load_index_file(ignore_ttl=False):
    try:
        if os.path.exists(_INDEX_FILE):
            ts = os.path.getmtime(_INDEX_FILE)
            if ignore_ttl or (time.time() - ts) < _INDEX_TTL:
                with open(_INDEX_FILE, encoding="utf-8") as f:
                    return json.load(f)
    except Exception:
        pass
    return None


def _save_index_file(data):
    try:
        with open(_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def build_index(force=False):
    """构建可转债搜索索引（代码/名称/简拼），分层容错：

    1) 内存缓存（24h）；2) 本地文件缓存 bond_index.json；3) 实时全市场抓取
    （短超时、限页、尽力而为）；4) 本地已入库转债兜底；5) 过期文件兜底。
    任一来源成功即返回，保证名称/简拼搜索在任何网络环境下都可用（至少覆盖
    已入库转债）。结果写入内存与本地文件缓存。
    """
    global _INDEX, _INDEX_TS
    now = time.time()
    if _INDEX is not None and not force and (now - _INDEX_TS) < _INDEX_TTL:
        return _INDEX
    with _index_lock:
        # 双重检查，避免并发重复构建
        if _INDEX is not None and not force and (now - _INDEX_TS) < _INDEX_TTL:
            return _INDEX
        # 2) 以本地已入库转债为可靠核心（始终包含全市场已入库债券，优先于文件缓存）
        bonds = _db_bonds()
        # 3) 实时全市场作为去重增强（尽力、限页、去重）
        try:
            for b in crawler.fetch_all_bonds():
                if not any(x.get("bond_code") == b.get("bond_code") for x in bonds):
                    bonds.append(b)
        except Exception:
            pass
        # 4) 兜底：仅当 DB 与实时增强均无可用时，才回退到本地文件缓存
        if not bonds:
            bonds = _load_index_file(ignore_ttl=True) or []
        if not bonds:
            return []
        enriched = []
        for b in bonds:
            name = b.get("bond_name") or ""
            sname = b.get("stock_name") or ""
            enriched.append({
                "bond_code": b.get("bond_code"),
                "bond_name": name,
                "stock_code": b.get("stock_code") or "",
                "stock_name": sname,
                "py": _make_pinyin(name),
                "spy": _make_pinyin(sname),
            })
        _INDEX = enriched
        _INDEX_TS = now
        _save_index_file(enriched)
        return _INDEX


def _warmup_index():
    """后台预热搜索索引，避免首个用户请求被全市场抓取阻塞。"""
    try:
        build_index()
    except Exception:
        pass


# ---- 即时兜底索引：全量索引未就绪时，直接基于本地已入库转债秒回，绝不阻塞用户 ----
_FAST_INDEX = None
_FAST_TS = 0
_FAST_TTL = 300


def _fast_index():
    global _FAST_INDEX, _FAST_TS
    now = time.time()
    if _FAST_INDEX is not None and (now - _FAST_TS) < _FAST_TTL:
        return _FAST_INDEX
    bonds = _db_bonds()
    enriched = [{
        "bond_code": b.get("bond_code"),
        "bond_name": b.get("bond_name") or "",
        "stock_code": b.get("stock_code") or "",
        "stock_name": b.get("stock_name") or "",
        "py": _make_pinyin(b.get("bond_name") or ""),
        "spy": _make_pinyin(b.get("stock_name") or ""),
    } for b in bonds]
    _FAST_INDEX, _FAST_TS = enriched, now
    return enriched


def get_search_index():
    """返回当前可用搜索索引：全量索引就绪则用之，否则即时返回 DB 兜底索引。"""
    if _INDEX is not None:
        return _INDEX
    return _fast_index()


def _score_bond(b, q, ql):
    """单债与查询 q 的相关度评分（越小越优，99=不相关）。"""
    name = b.get("bond_name") or ""
    sname = b.get("stock_name") or ""
    py = b.get("py") or ""
    spy = b.get("spy") or ""
    if not name and not sname:
        return 99
    if name == q or sname == q:
        return 1
    if py and py == ql:
        return 2
    if spy and spy == ql:
        return 2
    if q in name or q in sname or name in q or sname in q:
        return 3
    if py and ql in py:
        return 4
    if spy and ql in spy:
        return 4
    return 99


def resolve_query(q):
    """将用户输入（代码 / 名称 / 简拼）解析为 6 位转债代码；无法解析返回 None。"""
    q = (q or "").strip()
    if not q:
        return None
    if re.fullmatch(r"\d{6}", q):
        return q
    idx = get_search_index()
    ql = q.lower()
    cands = [(s, b) for b in idx for s in [_score_bond(b, q, ql)] if s < 99]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1].get("bond_code")


def search_candidates(q, limit=8):
    """返回 q 的候选转债列表（按相关度升序），供前端歧义选择；可能多个同分。"""
    q = (q or "").strip()
    if not q:
        return []
    idx = get_search_index()
    if re.fullmatch(r"\d{6}", q):
        return [b for b in idx if b.get("bond_code") == q][:limit]
    ql = q.lower()
    scored = [(s, b) for b in idx for s in [_score_bond(b, q, ql)] if s < 99]
    scored.sort(key=lambda x: x[0])
    return [b for _, b in scored[:limit]]


def _days_since(ts):
    """返回 ts('YYYY-MM-DD HH:MM:SS') 距今天数；解析失败返回 None。"""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - dt).days
    except Exception:
        return None


def _is_stale(ts):
    """超过缓存 TTL 视为「较旧，建议更新」（但默认不自动抓取）。"""
    if not CACHE_TTL_DAYS or CACHE_TTL_DAYS <= 0:
        return False
    d = _days_since(ts)
    return d is not None and d > CACHE_TTL_DAYS


def is_admin():
    return session.get("admin") is True


@app.context_processor
def _inject_admin():
    """让所有模板（含 _nav.html）都能用 {{ admin }} 判断管理员登录态。"""
    return {"admin": is_admin()}


def _is_bot():
    """粗略判断搜索引擎爬虫，避免污染『最近检索』列表。"""
    ua = (request.user_agent.string or "").lower()
    return any(k in ua for k in
               ("bot", "spider", "crawl", "slurp", "mediapartners", "archive", "http"))


def _flt(v):
    """把请求参数解析为 float，空/非法返回 None（高级筛选区间用）。"""
    if v is None:
        return None
    v = str(v).strip()
    if v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


@app.context_processor
def inject_freshness():
    """全局注入『数据截至』日期，供所有模板顶部新鲜度条使用。"""
    return {"data_as_of": get_latest_data_date()}


@app.context_processor
def inject_redeemed():
    """全局注入已发布强赎公告的转债代码集合与打标函数，供所有列表页标记。"""
    redeemed = get_redeemed_bond_codes()

    def redeem_badge(code):
        if code and code in redeemed:
            return Markup('<span class="badge-redeem" title="已发布强赎公告，注意及时卖出或转股">强赎</span>')
        return Markup('')

    return {"redeemed_bond_codes": redeemed, "redeem_badge": redeem_badge}


# ---------------- 公开查询 ----------------
@app.route("/")
def index():
    code = request.args.get("code", "").strip()
    persons = get_all_natural_persons(limit=30, offset=0)
    ranking = get_natural_ranking(limit=10)
    inst_ranking = get_institution_ranking(limit=10)
    recent = get_recent_bonds(12)
    market = compute_market_overview()
    trend = get_price_trend(days=365, min_sample=50)
    from db import get_equal_weight_latest, get_equal_weight_trend
    ew = get_equal_weight_latest()
    ew_trend = get_equal_weight_trend(days=365)
    return render_template("index.html", code=code, persons=persons,
                           ranking=ranking, inst_ranking=inst_ranking, recent=recent,
                           market=market, trend=trend, ew=ew, ew_trend=ew_trend)


@app.route("/api/search")
def api_search():
    """按代码 / 名称 / 简拼返回候选列表，供前端歧义选择（单结果直跳、多结果展示候选）。"""
    q = (request.args.get("q") or "").strip()
    results = search_candidates(q)
    return jsonify(results=[{
        "bond_code": b.get("bond_code"),
        "bond_name": b.get("bond_name") or "",
        "stock_name": b.get("stock_name") or "",
    } for b in results])


@app.route("/api/bond/<code>")
def api_bond(code):
    """查看转债详情 —— 缓存优先策略（核心）：

      - 已采集且报告期数据完整  -> 直接返回本地 SQLite 缓存（source=cache），绝不抓取；
      - 仅在以下情况才抓取：
          1) 未采集过（本地无记录）；
          2) 缓存异常（有债券记录但无报告期数据，兜底修复）；
          3) 用户主动刷新（?refresh=1，来自详情页「更新数据」按钮）。
    返回附带 updated_at / cache_age_days / stale / cache_ttl_days，供前端透明展示。
    """
    force = request.args.get("refresh") == "1"
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False,
                        "message": "未找到匹配的可转债，请检查代码 / 名称 / 简拼（如 127061、美锦、mjzz）"}), 404
    code = real

    bond = get_bond(code)
    periods = get_periods(code) if bond else []

    # 记录「检索」行为（首页搜索预检触发，仅人类会调用此接口）
    if bond:
        try:
            record_bond_view(code, bond.get("bond_name"), bond.get("stock_name"))
        except Exception:
            pass

    # 退市债：已采集数据后不再重新抓取，仅返回历史数据（冻结）
    if force and bond and bond.get("is_delisted"):
        return jsonify({
            "ok": True,
            "bond": bond,
            "periods": _periods_payload(code, periods) if periods else [],
            "cached": True,
            "source": "frozen",
            "updated_at": bond.get("updated_at"),
            "locked": True,
            "message": "该转债已退市，历史数据不再更新",
        })

    # ---- 缓存命中：直接返回，不触网 ----
    if bond and periods and not force:
        ts = bond.get("updated_at")
        data = {
            "ok": True,
            "bond": bond,
            "periods": _periods_payload(code, periods),
            "cached": True,
            "source": "cache",
            "updated_at": ts,
            "cache_age_days": _days_since(ts),
            "stale": _is_stale(ts),
            "cache_ttl_days": CACHE_TTL_DAYS,
        }
        return jsonify(data)

    # ---- 需要抓取：未采集 / 缓存异常 / 用户强制刷新 ----
    try:
        res = crawler.crawl_bond(code)
    except Exception as e:  # 网络/解析异常兜底
        return jsonify({"ok": False, "message": "抓取失败：" + str(e), "bond_code": code}), 502
    if not res.get("ok"):
        return jsonify(res), 404
    bond = get_bond(code)
    periods = get_periods(code)
    data = {
        "ok": True,
        "bond": bond,
        "periods": _periods_payload(code, periods),
        "cached": False,
        "source": "fresh",
        "updated_at": bond.get("updated_at") if bond else None,
        "cache_age_days": _days_since(bond.get("updated_at")) if bond else None,
        "stale": False,
        "cache_ttl_days": CACHE_TTL_DAYS,
        "message": res.get("message"),
    }
    return jsonify(data)


def _periods_payload(code, periods):
    out = []
    for p in periods:
        out.append({"period": p, "holders": get_holders(code, p)})
    return out


# ---------------- 自然人持有人聚合（服务端渲染，SEO） ----------------
@app.route("/persons")
def persons():
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    page_size = 50
    rows = get_all_natural_persons(limit=page_size, offset=(page - 1) * page_size)
    total = count_natural_persons()
    total_pages = (total + page_size - 1) // page_size
    return render_template("persons.html", persons=rows, page=page,
                           total_pages=total_pages, total=total)


def holder_view(name):
    """统一持有人视图：支持自然人 / 机构，按数据自动区分。

    同一持有人名下的记录，若全部 is_natural=1 视为自然人，否则视为机构；
    两只均渲染 holder.html，仅品牌/结构化数据略有差异。
    """
    holdings = get_person_holdings(name)
    if not holdings:
        abort(404)
    # 该持有人是否自然人：全部记录 is_natural=1 才视为自然人，否则机构
    is_natural = all((h.get("is_natural") or 0) == 1 for h in holdings)
    # 去重市值：每只转债仅取最新报告期持仓，避免跨期重复累加
    mv_wan, bond_count, record_count = get_person_market_value(name)
    latest = get_person_latest_holdings(name)
    # 已退市可转债不计入持有市值与持有只数（自然人与机构口径一致：均仅算未退市持仓）
    _excl = [h for h in latest if not h.get("is_delisted")]
    mv_wan = round(sum(h["mv_wan"] for h in _excl), 2)
    bond_count = len(_excl)
    # 附加每只转债的历史下修次数（供持仓汇总表展示，未采集则 None）
    for h in latest:
        h["down_revise_count"] = get_down_revise_count(h["bond_code"])
    return render_template("holder.html", name=name, holdings=holdings,
                           is_natural=is_natural,
                           bond_count=bond_count, record_count=record_count,
                           mv_wan=mv_wan, latest=latest)


@app.route("/holder/<name>")
def holder(name):
    return holder_view(name)


@app.route("/person/<name>")
def person(name):
    # 兼容旧链接 / SEO：自然人持有人统一走 /holder/<name> 视图
    return holder_view(name)


@app.route("/bonds")
def bonds_list():
    """已采集可转债列表 + 检索（编号/名称、退市状态、下修次数、排序）。"""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    code = request.args.get("code", "").strip()
    delisted = request.args.get("delisted", "all")
    if delisted not in ("all", "delisted", "active"):
        delisted = "all"
    has_down = request.args.get("has_down", "all")
    if has_down not in ("all", "yes", "no"):
        has_down = "all"
    sort = request.args.get("sort", "code")
    SORT_KEYS = ("code", "price", "conv", "premium", "redeem", "scale",
                 "expire", "down", "updated", "holder")
    if sort not in SORT_KEYS:
        sort = "code"
    DEFAULT_ORDER = {
        "code": "asc", "expire": "asc", "updated": "desc",
        "price": "desc", "conv": "desc", "premium": "desc",
        "redeem": "desc", "scale": "desc", "down": "desc", "holder": "desc",
    }
    order = request.args.get("order", "")
    if order not in ("asc", "desc"):
        order = DEFAULT_ORDER.get(sort, "asc")
    try:
        down_min = int(request.args.get("down_min", "").strip())
    except (ValueError, TypeError):
        down_min = None
    # ---- 高级筛选参数 ----
    hist_low = request.args.get("hist_low", "") == "1"
    rating = (request.args.get("rating", "") or "").strip()
    price_min = _flt(request.args.get("price_min"))
    price_max = _flt(request.args.get("price_max"))
    remain_scale_max = _flt(request.args.get("remain_scale_max"))
    cb_ratio_max = _flt(request.args.get("cb_ratio_max"))
    debt_ratio_max = _flt(request.args.get("debt_ratio_max"))
    premium_max = _flt(request.args.get("premium_max"))
    year_min = _flt(request.args.get("year_min"))
    year_max = _flt(request.args.get("year_max"))
    interest_debt_max = _flt(request.args.get("interest_debt_max"))
    page_size = 50
    # 取【全部匹配】行（排序在 Python 层完成——转股价值/溢价率为衍生字段，无法下推 SQL）
    rows_all, total = search_bonds(code=code, delisted=delisted, has_down=has_down,
                                   down_min=down_min, sort=sort, page=1, page_size=0)
    # 批量补充衍生字段：正股最新收盘价 -> 转股价值/转股溢价率（一次性批量查询，避免 N+1）
    _codes = [b["bond_code"] for b in rows_all]
    _stock_close = db.get_latest_stock_closes(_codes)
    # 高级筛选所需的批量数据：正股财务（资产负债率/有息负债率/总股本）、历史最低收盘价
    _fin_map = db.get_stock_finance_map()
    _hist_low_map = db.get_hist_low_map()

    def _enrich_derived(b):
        _tp = db.effective_transfer_price(b)  # 与详情页共用单一权威有效转股价（下修后价优先）
        try:
            _tp = float(_tp) if _tp not in (None, "") else None
        except (TypeError, ValueError):
            _tp = None
        _sp = _stock_close.get(b["bond_code"])
        _bp = b.get("current_price")
        conv_value = None
        if _tp and _sp is not None:
            try:
                conv_value = round(100.0 / float(_tp) * float(_sp), 2)
            except (TypeError, ValueError):
                conv_value = None
        premium = None
        if _bp is not None and conv_value:
            try:
                premium = round((float(_bp) / conv_value - 1.0) * 100.0, 2)
            except (TypeError, ValueError):
                premium = None
        b["conv_value"] = conv_value
        b["premium"] = premium

    for b in rows_all:
        _enrich_derived(b)

    # ---- 高级筛选：补充衍生字段并过滤（在 Python 层完成，与衍生字段同口径）----
    for b in rows_all:
        _code = b["bond_code"]
        _fin = _fin_map.get(b.get("stock_code") or "")
        b["debt_ratio"] = _fin.get("debt_ratio") if _fin else None
        b["interest_debt_ratio"] = _fin.get("interest_debt_ratio") if _fin else None
        # 转债占比% = 剩余规模(亿) / 正股总市值(亿) × 100；总市值=总股本(股)×正股现价/1e8
        _ts = _fin.get("total_share") if _fin else None
        _rs = b.get("remaining_scale")
        _sp = _stock_close.get(_code)
        b["cb_ratio"] = None
        if _rs and _ts and _sp:
            try:
                _mcap_yi = float(_ts) * float(_sp) / 1e8
                if _mcap_yi > 0:
                    b["cb_ratio"] = round(float(_rs) / _mcap_yi * 100, 2)
            except (TypeError, ValueError):
                pass
        b["years_left"] = checkup.years_left(b.get("expire_date"))
        _hl = _hist_low_map.get(_code)
        b["is_hist_low"] = (b.get("current_price") is not None and _hl is not None
                            and float(b["current_price"]) <= float(_hl))

    def _norm_rating(r):
        if not r:
            return ""
        return r.replace("sti", "").replace("STI", "").split("/")[0].split("-")[0].strip()

    _adv = (hist_low or rating or price_min is not None or price_max is not None
            or remain_scale_max is not None or cb_ratio_max is not None
            or debt_ratio_max is not None or premium_max is not None
            or year_min is not None or year_max is not None or interest_debt_max is not None)
    if _adv:
        _kept = []
        for b in rows_all:
            if hist_low and not b.get("is_hist_low"):
                continue
            if rating and _norm_rating(b.get("rating")) != rating:
                continue
            _p = b.get("current_price")
            if price_min is not None and (_p is None or float(_p) < price_min):
                continue
            if price_max is not None and (_p is None or float(_p) > price_max):
                continue
            _rs = b.get("remaining_scale")
            if remain_scale_max is not None and (_rs is None or float(_rs) > remain_scale_max):
                continue
            if cb_ratio_max is not None and (b.get("cb_ratio") is None or float(b["cb_ratio"]) > cb_ratio_max):
                continue
            if debt_ratio_max is not None and (b.get("debt_ratio") is None or float(b["debt_ratio"]) > debt_ratio_max):
                continue
            if premium_max is not None and (b.get("premium") is None or float(b["premium"]) > premium_max):
                continue
            _yl = b.get("years_left")
            if year_min is not None and (_yl is None or float(_yl) < year_min):
                continue
            if year_max is not None and (_yl is None or float(_yl) > year_max):
                continue
            if interest_debt_max is not None and (b.get("interest_debt_ratio") is None
                                                  or float(b["interest_debt_ratio"]) > interest_debt_max):
                continue
            _kept.append(b)
        rows_all = _kept
        total = len(rows_all)

    # 按列排序：①已退市债永远置后（与 SQL COALESCE(is_delisted,0) ASC 一致，避免第一页被退市债占满）；
    # ②缺失值(None) 在各自分组内排最后，与排序方向无关
    def _scale_num(b):
        rs = b.get("remaining_scale")
        if rs is not None and rs > 0:
            return float(rs)
        iss = b.get("issue_scale")
        return float(iss) if iss is not None else None

    _key_fn = {
        "code": lambda b: b.get("bond_code"),
        "price": lambda b: float(b["current_price"]) if b.get("current_price") is not None else None,
        "conv": lambda b: b.get("conv_value"),
        "premium": lambda b: b.get("premium"),
        "redeem": lambda b: float(b["redeem_price"]) if b.get("redeem_price") else None,
        "scale": _scale_num,
        "expire": lambda b: b.get("expire_date"),
        "down": lambda b: b.get("down_revise_count") or 0,
        "updated": lambda b: b.get("updated_at"),
        "holder": lambda b: b.get("holder_count") or 0,
    }.get(sort)
    _act = [b for b in rows_all if not b.get("is_delisted")]
    _del = [b for b in rows_all if b.get("is_delisted")]
    rows_sorted = []
    for _grp in (_act, _del):
        _non_null = [b for b in _grp if _key_fn(b) is not None]
        _null = [b for b in _grp if _key_fn(b) is None]
        _non_null.sort(key=_key_fn, reverse=(order == "desc"))
        rows_sorted.extend(_non_null + _null)

    # 在排序后的完整序列上切片分页
    start = (page - 1) * page_size
    rows = rows_sorted[start:start + page_size]

    # 当前页：强赎预警 + 剩余规模展示字段
    for b in rows:
        b["redemption_warn"] = crawler.compute_redemption_warning(b["bond_code"])
        b["redeem_price"] = b.get("redeem_price") or None
        _rs = b.get("remaining_scale")
        if _rs is None or _rs <= 0:
            _rs = b.get("issue_scale")
            b["is_remaining"] = False
        else:
            b["is_remaining"] = True
        b["remaining_scale_disp"] = _rs
    total_pages = (total + page_size - 1) // page_size
    # 分页 URL：保留全部筛选/排序参数，仅覆盖 page
    args = dict(request.args)
    args["page"] = page - 1
    prev_url = ("/bonds?" + urllib.parse.urlencode(args)) if page > 1 else None
    args["page"] = page + 1
    next_url = ("/bonds?" + urllib.parse.urlencode(args)) if page < total_pages else None
    # 评级下拉选项（归一化后按等级大致排序）
    _rating_raw = [r[0] for r in db.get_conn().execute(
        "SELECT DISTINCT rating FROM bonds WHERE rating IS NOT NULL AND rating<>''").fetchall()]
    _rorder = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
               "BB", "B", "CCC", "CC", "C"]
    _rating_options = sorted({_norm_rating(x) for x in _rating_raw if _norm_rating(x)})
    _rating_options.sort(key=lambda x: _rorder.index(x) if x in _rorder else 99)
    return render_template("bonds.html", rows=rows, total=total, page=page,
                           total_pages=total_pages, code=code, delisted=delisted,
                           has_down=has_down, down_min=(down_min if down_min is not None else ""),
                           sort=sort, order=order, prev_url=prev_url, next_url=next_url,
                           # 高级筛选：回显当前值 + 选项
                           hist_low=hist_low, rating=rating, rating_options=_rating_options,
                           price_min=(price_min if price_min is not None else ""),
                           price_max=(price_max if price_max is not None else ""),
                           remain_scale_max=(remain_scale_max if remain_scale_max is not None else ""),
                           cb_ratio_max=(cb_ratio_max if cb_ratio_max is not None else ""),
                           debt_ratio_max=(debt_ratio_max if debt_ratio_max is not None else ""),
                           premium_max=(premium_max if premium_max is not None else ""),
                           year_min=(year_min if year_min is not None else ""),
                           year_max=(year_max if year_max is not None else ""),
                           interest_debt_max=(interest_debt_max if interest_debt_max is not None else ""),
                           adv_active=_adv,
                           fin_updated=db.get_stock_finance_updated_at(),
                           fin_count=db.get_conn().execute("SELECT COUNT(*) FROM stock_finance").fetchone()[0],
                           remain_count=db.get_conn().execute(
                               "SELECT COUNT(*) FROM bonds WHERE remaining_scale IS NOT NULL").fetchone()[0])


# 可转债公告类型展示顺序与配色
ANN_TYPE_ORDER = ["即将发行", "强赎", "不强赎", "下修"]
ANN_TYPE_COLOR = {
    "即将发行": ("#2f6fed", "#eaf1ff"),
    "强赎": ("#d4263a", "#ffeaea"),
    "不强赎": ("#0a7d3e", "#e7f6ee"),
    "下修": ("#1aa35a", "#e7f8ef"),
}
# 交易信号标记配色：buy=买入信号(红) / sell=持仓离场信号(橙) / neutral=中性观察(灰)
ANN_SIGNAL = {
    "buy":     ("买入", "#d4263a", "#ffeaea"),
    "sell":    ("离场", "#d97706", "#fff3e6"),
    "neutral": ("中性", "#6b7280", "#f0f1f3"),
}


@app.route("/announcements")
def announcements():
    """可转债公告列表（真实事件：即将发行/强赎/不强赎/下修）。支持按类型筛选。"""
    atype = request.args.get("type", "").strip()
    if atype and atype not in ANN_TYPE_ORDER:
        atype = ""
    rows = get_announcements(atype=atype or None, limit=2000)
    counts = get_announcement_type_counts()
    total = sum(counts.values())
    # tabs：全部 + 各类型（按固定顺序，仅显示有数据的类型或当前选中类型）
    tabs = [{"key": "", "label": "全部", "n": total}]
    for t in ANN_TYPE_ORDER:
        n = counts.get(t, 0)
        if n or (atype == t):
            tabs.append({"key": t, "label": t, "n": n})
    # 给每行补上配色标签与交易信号标记
    for r in rows:
        fg, bg = ANN_TYPE_COLOR.get(r.get("announce_type"), ("#555", "#f0f0f0"))
        r["_fg"] = fg
        r["_bg"] = bg
        slabel, sfg, sbg = ANN_SIGNAL.get(r.get("signal") or "neutral",
                                          ANN_SIGNAL["neutral"])
        r["_signal_label"] = slabel
        r["_signal_fg"] = sfg
        r["_signal_bg"] = sbg
    return render_template("announcements.html", rows=rows, tabs=tabs,
                           active_type=atype, total=total)


@app.route("/redemption-warnings")
def redemption_warnings():
    """强赎预警单独页面：列出所有进入预警窗口（再 ≤5 个交易日触发强赎）的可转债。"""
    rows = crawler.get_redemption_warning_list()
    return render_template("redemption_warnings.html", rows=rows, total=len(rows))


@app.route("/down-revise-warnings")
def down_revise_warnings():
    """下修提醒单独页面：列出所有即将触发或已满足下修触发条件的可转债。"""
    rows = crawler.get_down_revise_warning_list()
    approaching = sum(1 for r in rows if r["status"] == "approaching")
    triggered = sum(1 for r in rows if r["status"] == "triggered")
    return render_template("down_revise_warnings.html", rows=rows,
                           total=len(rows), approaching=approaching, triggered=triggered)


@app.route("/double-low")
def double_low():
    """双低策略页面：当前持仓 20 只 + 累计收益 + 历史轮动记录（每期进入/调出与轮动收益）。"""
    data = get_double_low_change()
    if data is None:
        # 首次访问自动生成一次轮动快照
        try:
            crawler.rotate_double_low()
            data = get_double_low_change()
        except Exception:
            data = None
    holds = get_double_low_holds()
    history = get_double_low_history()
    return render_template("double_low.html", data=data, holds=holds, history=history)


@app.route("/new-bonds")
def new_bonds():
    rows = get_new_bonds(days=180)
    market = compute_market_overview()
    return render_template("new_bonds.html", rows=rows, market=market,
                           window_days=market.get("new_window_days", 180),
                           as_of=market.get("as_of"))


@app.route("/xiaopanzhai")
def xiaopanzhai():
    """小盘债（金陵式迷你弹性转债）筛选页：迷你盘 + 愿下修 + 非ST/未退市，含到期赎回价。"""
    rows, updated = mini_bond.get_rows()
    star = [r for r in rows if r["tag"] == "star"]
    low = [r for r in rows if r["tag"] == "low"]
    fired_n = sum(1 for r in rows if r["fired"])
    return render_template("xiaopanzhai.html", rows=rows, updated=updated,
                           total=len(rows), fired_n=fired_n,
                           star_n=len(star), low_n=len(low))


@app.route("/api/xiaopanzhai/refresh", methods=["POST"])
def api_xiaopanzhai_refresh():
    """实时刷新：重算实时价/赎回价/历史最高并写回 DB，返回 tbody 片段供前端无刷新替换。"""
    try:
        now = mini_bond.refresh_all()
    except Exception as e:
        return jsonify({"ok": False, "message": "刷新失败：" + str(e)}), 502
    rows, _ = mini_bond.get_rows(fill_missing=False)
    html = render_template("xiaopanzhai_rows.html", rows=rows)
    return jsonify({"ok": True, "html": html, "updated_at": now})


@app.route("/institutions")
def institutions():
    """机构持有人完整榜单（服务端渲染，分页）。"""
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    if page < 1:
        page = 1
    page_size = 50
    rows = get_all_institutions(limit=page_size, offset=(page - 1) * page_size)
    total = count_institutions()
    total_pages = (total + page_size - 1) // page_size
    return render_template("institutions.html", persons=rows, page=page,
                           total_pages=total_pages, total=total)


@app.route("/bond/<code>")
def bond_detail(code):
    """债券详情页：基础信息 + 历史下修记录（核心展示位）+ 最新期十大持有人 + 持仓牛散。

    下修数据缓存优先：已采集则读 bonds 表；否则调集思录 adj_logs 采集并入库。
    """
    real = resolve_query(code)
    if not real:
        abort(404)
    code = real
    bond = get_bond(code)
    if not bond:
        abort(404)

    # 记录浏览（便于首页『最近检索』快捷入口；爬虫不计入）
    if not _is_bot():
        try:
            record_bond_view(code, bond.get("bond_name"), bond.get("stock_name"))
        except Exception:
            pass

    # ---- 历史下修数据：缓存优先 ----
    count, records, dr_updated = get_down_revise(code)
    if count is None:
        try:
            c, recs = crawler.fetch_down_revise(code)
            save_down_revise(code, c, recs)
            count, records, dr_updated = c, recs, None
        except Exception:
            count, records = 0, []

    # ---- 十大持有人（支持按报告期切换） ----
    periods_info = get_periods_info(code)
    periods = [p["period"] for p in periods_info]
    latest_period = periods[0] if periods else None
    req_period = request.args.get("period")
    current_period = req_period if req_period in periods else latest_period
    holders = get_holders(code, current_period) if current_period else []
    natural_holders = [h for h in holders if h.get("is_natural")]

    # 最近一次下修提议（股东大会审议）日：records 已按最新在前
    latest_down_revise = records[0] if records else None

    # 当前转股价：与列表页共用单一权威函数 effective_transfer_price（下修后最新价优先，
    # 无下修回退 bonds.current_transfer_price），确保详情页与列表页口径永远一致
    eff_tp = db.effective_transfer_price(bond)

    # ---- 已公告强赎：取该债 announce_type='强赎' 的最新一条公告 ----
    redeem_ann = None
    try:
        rans = get_bond_announcements(code, "强赎")
        if rans:
            redeem_ann = rans[0]
    except Exception:
        redeem_ann = None

    # ---- 每日收盘价历史（转债 + 正股）+ 强赎预警 ----
    daily = get_daily_close(code, 250)
    # 已公告强赎的转债：强赎预警/下修提醒均无意义，跳过计算
    if redeem_ann:
        redemption_warn = None
        down_revise_warn = None
    else:
        redemption_warn = crawler.compute_redemption_warning(code)
        down_revise_warn = crawler.compute_down_revise_warning(code)

    # ---- 丰富指标（复用体检卡引擎：纯债价值/YTM/触发价/PB 等） ----
    try:
        cu = checkup.get_checkup(code)
    except Exception:
        cu = {}
    cu = cu or {}

    # ---- 转股价值修复：取 daily 最新【非空】正股收盘价（与走势图一致），
    #      末日空 bar 或缺失时回退腾讯实时价；彻底解决算不出转股价值的问题 ----
    stock_price = None
    for row in reversed(daily):
        sc = row.get("stock_close")
        if sc:
            try:
                stock_price = float(sc)
                break
            except (TypeError, ValueError):
                pass
    if stock_price is None and cu.get("stock_price"):
        stock_price = cu.get("stock_price")

    conv_value = None
    if eff_tp is not None and stock_price is not None:
        try:
            conv_value = round(100.0 / float(eff_tp) * float(stock_price), 2)
        except (TypeError, ValueError):
            conv_value = None

    # 转股溢价率 = (现价/转股价值 - 1) × 100%
    premium = None
    bp = bond.get("current_price")
    if bp is not None and conv_value:
        try:
            premium = round((float(bp) / conv_value - 1.0) * 100.0, 2)
        except (TypeError, ValueError):
            premium = None

    # 双低值 = 现价 + 转股溢价率
    double_low = None
    if bp is not None and premium is not None:
        double_low = round(float(bp) + premium, 2)

    # 债性指标（体检卡引擎估算：东财票面利率 + 评级贴现）
    pure_value = cu.get("pure_value")
    pure_value_est = cu.get("pure_value_est")
    ytm = cu.get("ytm")
    year_left = cu.get("year_left")
    pb = cu.get("pb")

    # 纯债溢价率 = (现价/纯债价值 - 1) × 100%
    pure_premium = None
    if bp is not None and pure_value:
        try:
            pure_premium = round((float(bp) / float(pure_value) - 1.0) * 100.0, 2)
        except (TypeError, ValueError):
            pure_premium = None

    # 条款触发价（市场通用口径：强赎130%/回售70%/下修85%，以各债公告条款为准）
    force_price = round(eff_tp * 1.3, 2) if eff_tp is not None else None
    put_price = round(eff_tp * 0.7, 2) if eff_tp is not None else None
    down_trig = round(eff_tp * 0.85, 2) if eff_tp is not None else None

    # 剩余规模（优先集思录 curr_iss_amt 持久化值；取不到回退发行规模并标注）
    remaining_scale = bond.get("remaining_scale")
    is_remaining = remaining_scale is not None and remaining_scale > 0
    if not is_remaining:
        remaining_scale = bond.get("issue_scale")

    # ---- 到期赎回价（bonds 表 redemption_price 列） ----
    redemption_price = bond.get("redemption_price")

    return render_template("bond.html", bond=bond, code=code,
                           down_count=count, down_records=records,
                           dr_updated=dr_updated,
                           latest_period=latest_period,
                           periods=periods_info,
                           current_period=current_period,
                           holders=holders,
                           natural_holders=natural_holders,
                           latest_down_revise=latest_down_revise,
                           eff_tp=eff_tp,
                           daily=daily,
                           redemption_warn=redemption_warn,
                           down_revise_warn=down_revise_warn,
                           conv_value=conv_value,
                           redemption_price=redemption_price,
                           redeem_ann=redeem_ann,
                           stock_price=stock_price,
                           premium=premium,
                           double_low=double_low,
                           pure_value=pure_value,
                           pure_value_est=pure_value_est,
                           pure_premium=pure_premium,
                           ytm=ytm,
                           year_left=year_left,
                           pb=pb,
                           force_price=force_price,
                           put_price=put_price,
                           down_trig=down_trig,
                           remaining_scale=remaining_scale,
                           is_remaining=is_remaining,
                           watched=db.is_watched(code),
                           decision=db.latest_decision(code),
                           decisions=db.list_decisions(code),
                           in_compare=code in session.get("compare", []))


@app.route("/api/bond/<code>/holders")
def api_bond_holders(code):
    """按报告期返回十大持有人 JSON，供详情页无刷新切换期数。"""
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    code = real
    periods_info = get_periods_info(code)
    periods = [p["period"] for p in periods_info]
    if not periods:
        return jsonify({"ok": True, "period": None, "periods": [], "holders": []})
    req_period = request.args.get("period")
    current_period = req_period if req_period in periods else periods[0]
    holders = get_holders(code, current_period)
    return jsonify({"ok": True, "period": current_period,
                    "periods": periods_info, "holders": holders})


@app.route("/xiuxie/<code>")
def xiuxie_detail(code):
    """历史下修记录已合并展示在转债详情页 /bond/<code>（完整明细表 + FAQ），
    此处 301 重定向到详情页，保留旧链接与已收录入口的权重，避免重复内容。
    """
    return redirect(url_for("bond_detail", code=code), code=301)


@app.route("/robots.txt")
def robots():
    return ("User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n",
            200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/sitemap.xml")
def sitemap():
    base = request.host_url.rstrip("/")
    urls = [base + "/", base + "/bonds", base + "/persons", base + "/institutions"]
    for p in get_all_natural_persons(limit=5000, offset=0):
        urls.append(base + "/person/" + urllib.parse.quote(p["holder_name"]))
    for p in get_all_institutions(limit=5000, offset=0):
        urls.append(base + "/holder/" + urllib.parse.quote(p["holder_name"]))
    # 可转债详情页：搜索落地页，全部收录（SEO）
    for b in list_market_bonds():
        code = b.get("bond_code")
        if code:
            urls.append(base + "/bond/" + code)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
    for u in urls:
        xml += "  <url><loc>%s</loc></url>\n" % u
    xml += "</urlset>\n"
    return xml, 200, {"Content-Type": "application/xml"}


# ---------------- 管理后台 ----------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        u = request.form.get("username")
        p = request.form.get("password")
        if u == ADMIN_USER and p == ADMIN_PASS:
            session["admin"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="用户名或密码错误")
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin():
    if not is_admin():
        return redirect(url_for("admin_login"))
    # 首页只渲染轻量的设置面板；已收录/全市场两大列表改为点菜单才懒加载（见 /admin/api/bonds、/admin/api/market）
    return render_template("admin.html", site=get_site_settings())


@app.route("/admin/api/bonds")
def admin_api_bonds():
    """已收录转债（已抓取持有人）列表，按需加载，避免后台首页一次性拉全量拖慢。"""
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    return jsonify(list_bonds())


@app.route("/admin/api/market")
def admin_api_market():
    """市场全部可转债列表，按需加载。"""
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    return jsonify(list_market_bonds())


@app.route("/admin/collect-logs")
def admin_collect_logs():
    """采集日志：列出每次自动/手动采集的运行，并可展开查看每步成败与错误原文。"""
    if not is_admin():
        return redirect(url_for("admin_login"))
    run_id = request.args.get("run")
    runs = get_collect_runs(limit=50)
    steps = get_collect_steps(run_id) if run_id else []
    running = get_running_collect_run() is not None
    return render_template("admin_collect_logs.html",
                           runs=runs, steps=steps, selected_run=run_id,
                           running=running, site=get_site_settings())


@app.route("/admin/collect/run", methods=["POST"])
def admin_collect_run():
    """后台立即触发一次每日采集（--force，绕过交易日守卫），日志写入 collect_runs/steps。"""
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    # 先回收可能因服务重启而中断、永远停在 running 的遗留记录，避免「立即采集」被死锁
    recover_stale_runs()
    running_id = get_running_collect_run()
    if running_id:
        return jsonify({"ok": False, "message": "已有采集任务进行中（%s），请稍后重试" % running_id})
    py = sys.executable
    env = dict(os.environ)
    env["COLLECT_TRIGGER"] = "admin"
    log_path = os.path.join(BASE_DIR, "collect_cron.log")
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            subprocess.Popen(
                [py, "collect_daily.py", "--force", "--trigger=admin"],
                cwd=BASE_DIR, env=env, stdout=lf, stderr=lf,
                start_new_session=(os.name != "nt"))
        return jsonify({"ok": True, "message": "已启动后台采集，请稍后刷新本页查看日志"})
    except Exception as e:
        return jsonify({"ok": False, "message": "启动失败：" + str(e)}), 500


@app.route("/admin/collect-holders", methods=["POST"])
def admin_collect_holders():
    """后台手动触发持有人增量刷新（不进每日自动管道，低频手动点）。

    独立进程跑 refresh_holders.py，运行前后统计 pending 写入 holder_refresh_status.json，
    前端轮询 /admin/api/holder-refresh-status 展示进度。
    """
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    status_file = os.path.join(BASE_DIR, "holder_refresh_status.json")
    # 已在运行则拒绝（避免并发重复撞东方财富限流）
    if os.path.exists(status_file):
        try:
            with open(status_file, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("running"):
                return jsonify({"ok": False, "message": "持有人采集任务进行中，请稍后查看状态"})
        except Exception:
            pass
    py = sys.executable
    log_path = os.path.join(BASE_DIR, "holder_refresh.log")
    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            subprocess.Popen(
                [py, "refresh_holders.py"],
                cwd=BASE_DIR, stdout=lf, stderr=lf,
                start_new_session=(os.name != "nt"))
        return jsonify({"ok": True, "message": "已启动持有人采集，可在本页查看进度"})
    except Exception as e:
        return jsonify({"ok": False, "message": "启动失败：" + str(e)}), 500


@app.route("/admin/api/holder-refresh-status")
def admin_holder_refresh_status():
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    status_file = os.path.join(BASE_DIR, "holder_refresh_status.json")
    if not os.path.exists(status_file):
        return jsonify({"running": False, "updated": 0, "pending_after": 0,
                        "message": "尚未采集过（点上方按钮手动采集）"})
    try:
        with open(status_file, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("ok", True)
        return jsonify(st)
    except Exception as e:
        return jsonify({"running": False, "message": "状态读取失败：" + str(e)})


@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    if request.method == "POST":
        name = (request.form.get("site_name") or "").strip() or "可转债持有人信息"
        domain = (request.form.get("site_domain") or "").strip()
        logo = (request.form.get("site_logo") or "").strip()  # base64 data URI 或 URL，空=清除
        if logo.startswith("data:") and len(logo) > 2_000_000:
            return jsonify({"ok": False, "message": "Logo 图片过大（请压缩到 2MB 以内）"}), 400
        save_site_settings(name, domain, logo)
        return jsonify({"ok": True})
    return jsonify(get_site_settings())


def _is_frozen(code):
    """退市且已有持有人数据 -> 冻结，不再更新（符合『采集后不再更新退市债』规则）。

    退市但从未采集过的债，仍允许首次采集。
    """
    b = get_bond(code)
    if not (b and b.get("is_delisted")):
        return False
    return len(get_periods_info(code)) > 0


@app.route("/admin/update/<code>", methods=["POST"])
def admin_update(code):
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    if _is_frozen(code):
        return jsonify({"ok": False, "locked": True, "message": "该转债已退市且已有数据，不再更新"})
    try:
        res = crawler.crawl_bond(code)
    except Exception as e:
        return jsonify({"ok": False, "message": "更新失败：" + str(e)}), 502
    return jsonify(res)


@app.route("/admin/update_all", methods=["POST"])
def admin_update_all():
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    # 是否忽略已退市可转债（管理后台开关，默认忽略）
    ignore = request.form.get("ignore_delisted", "1") == "1"
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    sql = ("SELECT bond_code FROM bonds WHERE bond_code IN "
           "(SELECT DISTINCT bond_code FROM holders)")
    if ignore:
        sql += " AND COALESCE(is_delisted,0)=0"
    cur.execute(sql)
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    results = []
    for c in codes:
        # 不忽略时，退市且已有数据的债仍按冻结规则跳过（避免无效请求）
        if not ignore and _is_frozen(c):
            results.append({"code": c, "ok": False, "skipped": True,
                            "message": "已退市且已有数据，跳过"})
            continue
        try:
            res = crawler.crawl_bond(c)
            results.append({"code": c, "ok": res.get("ok"), "message": res.get("message")})
        except Exception as e:
            results.append({"code": c, "ok": False, "message": str(e)})
    return jsonify({"ok": True, "total": len(results), "results": results})


@app.route("/admin/add", methods=["POST"])
def admin_add():
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    code = (request.form.get("code") or "").strip()
    if not code:
        return jsonify({"ok": False, "message": "请输入转债代码"}), 400
    # 是否忽略已退市可转债（管理后台开关，默认忽略）
    ignore = request.form.get("ignore_delisted", "1") == "1"
    bond = get_bond(code)
    if ignore:
        delisted = bool(bond and bond.get("is_delisted"))
        # 库里没有记录的新代码，做一次轻量预检判断是否退市
        if not delisted and not bond:
            basic = crawler.fetch_bond_basic(code)
            delisted = bool(basic and basic.get("is_delisted"))
        if delisted:
            return jsonify({"ok": False, "skipped": True,
                            "message": "该转债已退市，已忽略采集"})
    if bond and bond.get("is_delisted") and len(get_periods_info(code)) > 0:
        return jsonify({"ok": False, "locked": True,
                        "message": "该转债已退市且已有数据，不再更新"})
    try:
        res = crawler.crawl_bond(code)
    except Exception as e:
        return jsonify({"ok": False, "message": "抓取失败：" + str(e)}), 502
    return jsonify(res)


@app.route("/admin/delete/<code>", methods=["POST"])
def admin_delete(code):
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM holders WHERE bond_code=?", (code,))
    cur.execute("DELETE FROM bonds WHERE bond_code=?", (code,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/admin/delisted/<code>", methods=["POST"])
def admin_delisted(code):
    """手动切换某转债的退市标记（管理后台修正用）。"""
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    raw = request.form.get("delisted")
    if raw is None:
        raw = request.args.get("delisted")
    val = 1 if str(raw) in ("1", "true", "on", "yes") else 0
    ddate = None
    if val == 1:
        b = get_bond(code) or {}
        ddate = b.get("expire_date") or b.get("delist_date")
    set_delisted(code, val == 1, ddate)
    return jsonify({"ok": True, "bond_code": code, "is_delisted": val})


@app.route("/bond/<code>/checkup")
def bond_checkup(code):
    """转债体检卡：实时数据分析 + 核心要素提炼。详情页入口跳转至此。"""
    real = resolve_query(code)
    if not real:
        abort(404)
    code = real
    bond = get_bond(code)
    if not bond:
        abort(404)
    data = checkup.get_checkup(code)
    if not data:
        abort(404)
    watched = db.is_watched(code)
    decision = db.latest_decision(code)
    decisions = db.list_decisions(code)
    in_compare = code in session.get("compare", [])
    return render_template("checkup.html", data=data, code=code,
                           watched=watched, decision=decision, decisions=decisions, in_compare=in_compare)


# ===================== 个人操作：关注 / 决策 / 对比 =====================
@app.route("/api/watch/<code>", methods=["POST"])
def api_watch(code):
    if not is_admin():
        return jsonify({"ok": False, "message": "请先登录管理员账号"}), 401
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    code = real
    bond = get_bond(code)
    if not bond:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    if db.is_watched(code):
        db.remove_watch(code)
        return jsonify({"ok": True, "watched": False})
    db.add_watch(code, bond.get("bond_name"))
    return jsonify({"ok": True, "watched": True})


@app.route("/api/decision/<code>", methods=["POST"])
def api_decision(code):
    if not is_admin():
        return jsonify({"ok": False, "message": "请先登录管理员账号"}), 401
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    code = real
    if not get_bond(code):
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        decision = payload.get("decision")
        note = payload.get("note")
    else:
        decision = request.form.get("decision")
        note = request.form.get("note")
    if decision not in ("买入", "观望", "规避", "关注"):
        return jsonify({"ok": False, "message": "无效决策"}), 400
    db.save_decision(code, decision, note)
    return jsonify({"ok": True, "decision": db.latest_decision(code)})


@app.route("/watchlist")
def watchlist_page():
    """我的关注：概览 + 一键看诊断。仅管理员可见。"""
    if not is_admin():
        return redirect(url_for("admin_login"))
    items = db.list_watch()
    rows = []
    for it in items:
        code = it["bond_code"]
        bond = get_bond(code) or {}
        eff_tp = db.effective_transfer_price(bond) if bond else None
        # 现价：优先 bonds.current_price，回退 daily_close 最新转债收盘
        price = bond.get("current_price")
        if price is None:
            d = db.get_daily_close(code, 1)
            price = d[0].get("bond_close") if d else None
        # 转股溢价率（本地算，避免逐个打实时接口）
        premium = None
        if eff_tp and price is not None:
            d = db.get_daily_close(code, 1)
            sc = d[0].get("stock_close") if d else None
            if sc:
                try:
                    conv = 100.0 / float(eff_tp) * float(sc)
                    premium = round((float(price) / conv - 1) * 100, 2)
                except (TypeError, ValueError):
                    premium = None
        # 双低值 = 现价 + 转股溢价率（实时计算，与详情页、/double-low 榜单同口径）
        # ⚠️ 不可用 double_low_log：那是每周一写入的「前20名」周快照，既非实时值
        #    （会与同行现价/溢价率对不上），且从未进过榜单的债查不到、显示 —
        dl = None
        if price is not None and premium is not None:
            try:
                dl = round(float(price) + premium, 2)
            except (TypeError, ValueError):
                dl = None
        # 剩余年限：由到期日实时算。bonds 表没有 remain_years 列——该键只在
        # db.get_new_bonds() 里现算，get_bond() 是 SELECT * 不会返回它，直接 get 恒为 None
        year_left = checkup.years_left(bond.get("expire_date")) if bond else None
        rows.append({
            "code": code,
            "name": it.get("bond_name") or bond.get("bond_name") or code,
            "price": price,
            "premium": premium,
            "rating": bond.get("rating"),
            "year_left": round(year_left, 2) if year_left is not None else None,
            "double_low": dl,
            "decision": db.latest_decision(code),
            "added_at": it.get("added_at"),
        })
    return render_template("watchlist.html", rows=rows, count=len(rows))


@app.route("/api/compare/add/<code>", methods=["POST"])
def api_compare_add(code):
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    code = real
    if not get_bond(code):
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    lst = list(session.get("compare", []))
    if code not in lst:
        lst.append(code)
    session["compare"] = lst[-4:]   # 最多对比 4 只
    return jsonify({"ok": True, "compare": session["compare"]})


@app.route("/api/compare/remove/<code>", methods=["POST"])
def api_compare_remove(code):
    lst = list(session.get("compare", []))
    if code in lst:
        lst.remove(code)
    session["compare"] = lst
    return jsonify({"ok": True, "compare": session["compare"]})


@app.route("/api/compare/clear", methods=["POST"])
def api_compare_clear():
    session["compare"] = []
    return jsonify({"ok": True, "compare": []})


@app.route("/api/compare/state")
def api_compare_state():
    return jsonify({"ok": True, "compare": list(session.get("compare", []))})


@app.route("/compare")
def compare_page():
    """横向对比：选中转债的关键指标并排。"""
    codes = list(dict.fromkeys(session.get("compare", [])))  # 保序去重
    cards = []
    for code in codes:
        bond = get_bond(code)
        if not bond:
            continue
        data = checkup.get_checkup(code) or {}
        cards.append({"code": code, "name": bond.get("bond_name") or code, "data": data})
    return render_template("compare.html", cards=cards)


@app.route("/api/bond/<code>/realtime")
def api_bond_realtime(code):
    """仅刷新体检卡的实时行情（腾讯秒级），供前端局部无刷新更新。"""
    real = resolve_query(code)
    if not real:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    code = real
    rt = checkup.get_realtime(code)
    if not rt:
        return jsonify({"ok": False, "message": "未找到该转债"}), 404
    return jsonify({"ok": True, **rt})


if __name__ == "__main__":
    # 仅供本地使用；如需外网部署请改由 gunicorn/waitress 托管并关闭 debug
    # 默认仅监听本机 127.0.0.1；如需局域网访问可改为 "0.0.0.0"
    # debug 模式开启「自动重载」：修改 py / 模板后 Flask 自动重启，无需手动重启进程。
    #   但 WorkBuddy 安全组件(tsbx)会把 reloader 子进程限制为只读，导致 SQLite 写入失败，
    #   因此本地默认关闭 debug（FLASK_DEBUG 缺省=0）。如确需热重载，可 set FLASK_DEBUG=1，但会触发只读问题。
    # 后台预热搜索索引（全市场列表），首个用户请求不会被抓取阻塞
    threading.Thread(target=_warmup_index, daemon=True).start()
    import os
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug_mode, threaded=True)
