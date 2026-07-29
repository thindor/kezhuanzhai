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
from db import (init_db, get_bond, get_periods, get_holders, list_bonds,
                list_market_bonds,
                get_all_natural_persons, count_natural_persons, get_person_holdings,
                get_person_market_value, get_person_latest_holdings,
                get_natural_ranking, get_down_revise, save_down_revise,
                get_down_revise_count, record_bond_view, get_recent_bonds)
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
    recent = get_recent_bonds(12)
    return render_template("index.html", code=code, persons=persons,
                           ranking=ranking, recent=recent)


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


@app.route("/person/<name>")
def person(name):
    holdings = get_person_holdings(name)
    if not holdings:
        abort(404)
    # 去重市值：每只转债仅取最新报告期持仓，避免跨期重复累加
    mv_wan, bond_count, record_count = get_person_market_value(name)
    latest = get_person_latest_holdings(name)
    # 附加每只转债的历史下修次数（供持仓汇总表展示，未采集则 None）
    for h in latest:
        h["down_revise_count"] = get_down_revise_count(h["bond_code"])
    return render_template("person.html", name=name, holdings=holdings,
                           bond_count=bond_count, record_count=record_count,
                           mv_wan=mv_wan, latest=latest)


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

    # ---- 十大持有人（最新一期） ----
    periods = get_periods(code)
    latest_period = periods[0] if periods else None
    holders = get_holders(code, latest_period) if latest_period else []
    natural_holders = [h for h in holders if h.get("is_natural")]

    # 最近一次下修提议（股东大会审议）日：records 已按最新在前
    latest_down_revise = records[0] if records else None

    return render_template("bond.html", bond=bond, code=code,
                           down_count=count, down_records=records,
                           dr_updated=dr_updated,
                           latest_period=latest_period,
                           holders=holders,
                           natural_holders=natural_holders,
                           latest_down_revise=latest_down_revise)


@app.route("/xiuxie/<code>")
def xiuxie_detail(code):
    """历史下修记录独立专题页（SEO 优化）：独立 URL、结构化数据、FAQ、语义化标签。

    转债详情页 /bond/<code> 仅保留下修统计卡 + 「查看完整历史下修记录」链接，
    完整明细表放在本页，避免两页重复内容导致收录分散。
    """
    real = resolve_query(code)
    if not real:
        abort(404)
    code = real
    bond = get_bond(code)
    if not bond:
        abort(404)
    # ---- 历史下修数据：缓存优先 ----
    count, records, dr_updated = get_down_revise(code)
    if count is None:
        try:
            c, recs = crawler.fetch_down_revise(code)
            save_down_revise(code, c, recs)
            count, records, dr_updated = c, recs, None
        except Exception:
            count, records = 0, []
    latest = records[0] if records else None
    return render_template("xiuxie.html", bond=bond, code=code,
                           down_count=count, down_records=records,
                           dr_updated=dr_updated,
                           latest_down_revise=latest)


@app.route("/robots.txt")
def robots():
    return ("User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n",
            200, {"Content-Type": "text/plain; charset=utf-8"})


@app.route("/sitemap.xml")
def sitemap():
    base = request.host_url.rstrip("/")
    urls = [base + "/", base + "/persons"]
    for p in get_all_natural_persons(limit=5000, offset=0):
        urls.append(base + "/person/" + urllib.parse.quote(p["holder_name"]))
    # 可转债详情页：搜索落地页，全部收录（SEO）
    for b in list_market_bonds():
        code = b.get("bond_code")
        if code:
            urls.append(base + "/bond/" + code)
    # 有下修记录的转债：独立专题页纳入收录
    for b in list_market_bonds():
        cnt = b.get("down_revise_count")
        try:
            if cnt and int(cnt) > 0:
                urls.append(base + "/xiuxie/" + b["bond_code"])
        except (ValueError, TypeError):
            pass
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


@app.route("/admin/update/<code>", methods=["POST"])
def admin_update(code):
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    try:
        res = crawler.crawl_bond(code)
    except Exception as e:
        return jsonify({"ok": False, "message": "更新失败：" + str(e)}), 502
    return jsonify(res)


@app.route("/admin/update_all", methods=["POST"])
def admin_update_all():
    if not is_admin():
        return jsonify({"ok": False, "message": "未登录"}), 401
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT bond_code FROM bonds WHERE bond_code IN (SELECT DISTINCT bond_code FROM holders)")
    codes = [r[0] for r in cur.fetchall()]
    conn.close()
    results = []
    for c in codes:
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


if __name__ == "__main__":
    # 仅供本地使用；如需外网部署请改由 gunicorn/waitress 托管并关闭 debug
    # 默认仅监听本机 127.0.0.1；如需局域网访问可改为 "0.0.0.0"
    # debug 模式开启「自动重载」：修改 py / 模板后 Flask 自动重启，无需手动重启进程
    #   —— 本地开发默认开（FLASK_DEBUG 缺省=1）；部署到 kzz.bukui.fun 时请 set FLASK_DEBUG=0 或用生产服务器托管
    # 后台预热搜索索引（全市场列表），首个用户请求不会被抓取阻塞
    threading.Thread(target=_warmup_index, daemon=True).start()
    import os
    debug_mode = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="127.0.0.1", port=5000, debug=debug_mode, threaded=True)
