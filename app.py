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
import re
import os
import json
import time
import threading
import urllib.parse
from datetime import datetime

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
                get_announcement_type_counts, clear_announcements, get_daily_close)
import crawler

app = Flask(__name__)
app.secret_key = SECRET_KEY

init_db()


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


def resolve_query(q):
    """将用户输入（代码 / 名称 / 简拼）解析为 6 位转债代码；无法解析返回 None。"""
    q = (q or "").strip()
    if not q:
        return None
    if re.fullmatch(r"\d{6}", q):
        return q
    idx = get_search_index()
    ql = q.lower()

    def score(b):
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

    cands = [(score(b), b) for b in idx]
    cands = [c for c in cands if c[0] < 99]
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1].get("bond_code")


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


def _is_bot():
    """粗略判断搜索引擎爬虫，避免污染『最近检索』列表。"""
    ua = (request.user_agent.string or "").lower()
    return any(k in ua for k in
               ("bot", "spider", "crawl", "slurp", "mediapartners", "archive", "http"))


# ---------------- 公开查询 ----------------
@app.route("/")
def index():
    code = request.args.get("code", "").strip()
    persons = get_all_natural_persons(limit=30, offset=0)
    ranking = get_natural_ranking(limit=30)
    inst_ranking = get_institution_ranking(limit=30)
    recent = get_recent_bonds(12)
    return render_template("index.html", code=code, persons=persons,
                           ranking=ranking, inst_ranking=inst_ranking, recent=recent)


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
    if sort not in ("code", "down", "updated", "holder", "expire"):
        sort = "code"
    try:
        down_min = int(request.args.get("down_min", "").strip())
    except (ValueError, TypeError):
        down_min = None
    page_size = 50
    rows, total = search_bonds(code=code, delisted=delisted, has_down=has_down,
                                down_min=down_min, sort=sort, page=page, page_size=page_size)
    # 为当前页每只转债计算强赎预警（提前 >=5 交易日）
    for b in rows:
        b["redemption_warn"] = crawler.compute_redemption_warning(b["bond_code"])
    total_pages = (total + page_size - 1) // page_size
    # 分页 URL：保留全部筛选参数，仅覆盖 page
    args = dict(request.args)
    args["page"] = page - 1
    prev_url = ("/bonds?" + urllib.parse.urlencode(args)) if page > 1 else None
    args["page"] = page + 1
    next_url = ("/bonds?" + urllib.parse.urlencode(args)) if page < total_pages else None
    return render_template("bonds.html", rows=rows, total=total, page=page,
                           total_pages=total_pages, code=code, delisted=delisted,
                           has_down=has_down, down_min=(down_min if down_min is not None else ""),
                           sort=sort, prev_url=prev_url, next_url=next_url)


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

    # 当前转股价：优先用集思录最新下修后转股价（已下修债才是真正的当前价），
    # 无下修记录时回退东方财富 current_transfer_price
    eff_tp = None
    if latest_down_revise and latest_down_revise.get("price_after") is not None:
        eff_tp = latest_down_revise.get("price_after")
    if eff_tp is None:
        eff_tp = bond.get("current_transfer_price")
    # 兜底强转浮点（DB 该列为 TEXT 亲和，取出可能是字符串）
    try:
        eff_tp = float(eff_tp)
    except (TypeError, ValueError):
        eff_tp = None

    # ---- 每日收盘价历史（转债 + 正股）+ 强赎预警 ----
    daily = get_daily_close(code, 250)
    redemption_warn = crawler.compute_redemption_warning(code)

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
                           redemption_warn=redemption_warn)


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
    bonds = list_bonds()
    market = list_market_bonds()
    return render_template("admin.html", bonds=bonds, market=market)


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
