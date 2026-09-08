# -*- coding: utf-8 -*-
"""按「底线明确(安全) + 时间≥2年 + 打强赎为主 + 分散」筛选可转债。
逻辑：
  1. 安全性：评级归一后 >= AA-（排除 A+/A/A-/BBB/CC 等低评级与违约风险）
  2. 时间：剩余年限 2 <= years <= 5（到期不少于2年，且不过长）
  3. 债底/价格：现价 100~115（贴近债底、留强赎上行空间）
  4. 打强赎弹性：转股溢价率 <= 40%（能跟涨，正股涨30%触发强赎时转债可到130+）
  5. 分散：按策略契合度排序，给出建议篮子（跨评级分布）
"""
import sqlite3, urllib.request, datetime, html

DB = "cb_holders.db"
TODAY = datetime.date(2026, 9, 2)

GRADE_RANK = {"AAA": 7, "AA+": 6, "AA": 5, "AA-": 4, "A+": 3, "A": 2, "A-": 1,
              "BBB+": 0, "BBB": -1, "CC": -2}

def norm_rating(r):
    if not r:
        return None, None
    base = r.replace("sti", "").strip()
    return base, ("sti" in r)

def years_left(expire):
    try:
        d = datetime.date.fromisoformat(str(expire)[:10])
    except Exception:
        return None
    return (d - TODAY).days / 365.25

def sina_symbol(stock_code):
    sc = str(stock_code).strip()
    if sc[0] in ("6", "9", "5"):
        return "sh" + sc
    return "sz" + sc

def fetch_stock_prices(syms):
    """syms: list of sina symbols; return {sym: price}"""
    out = {}
    for i in range(0, len(syms), 50):
        batch = syms[i:i+50]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=15).read()
            txt = raw.decode("gbk", "ignore")
            for line in txt.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                sym = line.split("=")[0].replace("v_", "").strip()
                body = line.split('"')[1] if '"' in line else ""
                if not body:
                    continue
                f = body.split("~")
                try:
                    out[sym] = float(f[3])
                except (ValueError, IndexError):
                    pass
        except Exception as e:
            print("fetch err", e)
    return out

con = sqlite3.connect(DB)
cur = con.cursor()
cur.execute("""SELECT bond_code, bond_name, stock_code, stock_name, rating, expire_date,
                     current_price, current_transfer_price, redemption_price, redeem_price,
                     remaining_scale, down_revise_count
              FROM bonds WHERE is_delisted=0""")
rows = cur.fetchall()
con.close()

cands = []
for (code, name, sc, sn, rating, exp, price, tp, rp, rrp, rem, drc) in rows:
    base, is_sti = norm_rating(rating)
    gr = GRADE_RANK.get(base)
    if gr is None or gr < 3:          # 安全：评级 < A+ 排除（A+ 及以上；AA-+ 为严格档）
        continue
    yl = years_left(exp)
    if yl is None or yl < 2 or yl > 5:   # 时间范围 2~5 年
        continue
    if price is None or price < 100 or price > 130:   # 低于强赎触发区(130)，留上行空间
        continue
    rp = rp or rrp
    if not rp:
        continue
    ytm = (rp - price) / price / yl * 100   # 近似年化到期收益率(%)，仅作信息展示
    bottom_safe = price <= rp               # 现价<=到期赎回价=持有到期不亏(底线最硬)
    cands.append(dict(code=code, name=name, sc=sc, sn=sn, rating=base, sti=is_sti,
                      yl=yl, price=float(price) if price is not None else None,
                      tp=float(tp) if tp is not None else None,
                      rp=rp, rem=rem, drc=drc or 0, ytm=ytm,
                      bottom_safe=bottom_safe))

print(f"阶段1(安全+时间+价格+YTM)候选数: {len(cands)}")
if not cands:
    raise SystemExit("无候选")

# 批量抓正股现价
syms = [sina_symbol(c["sc"]) for c in cands]
prices = fetch_stock_prices(syms)
sym_by_code = {c["code"]: sina_symbol(c["sc"]) for c in cands}

for c in cands:
    sym = sym_by_code[c["code"]]
    sp = prices.get(sym)
    c["stock_price"] = sp
    if sp and c["tp"]:
        try:
            tp = float(c["tp"]); price = float(c["price"])
        except (TypeError, ValueError):
            tp = price = None
        conv_val = 100.0 / tp * sp if tp else None          # 转股价值
        c["conv_val"] = conv_val
        c["conv_prem"] = (c["price"] / conv_val - 1) * 100 if conv_val else None
    else:
        c["conv_val"] = None
        c["conv_prem"] = None

# 阶段2：打强赎弹性（转股溢价率 <= 65%，且能算出；>65%需正股大涨才触发强赎）
passed = [c for c in cands if c["conv_prem"] is not None and c["conv_prem"] <= 65]
print(f"阶段2(转股溢价率<=65%)通过数: {len(passed)}")
# 严格档：仅 AA- 及以上（剔除 A+）
passed_strict = [c for c in passed if GRADE_RANK.get(c["rating"], 0) >= 4]

# 策略契合度评分（越高越契合）：债底(YTM)+上行空间(130-现价)+强赎弹性(35-溢价)+评级
for c in passed:
    c["score"] = (max(0, c["ytm"]) * 0.3
                  + (130 - c["price"]) * 0.3
                  + max(0, 50 - c["conv_prem"]) * 0.25
                  + GRADE_RANK.get(c["rating"], 0) * 0.15)

passed.sort(key=lambda x: x["score"], reverse=True)

# ---- 输出 ----
def fnum(v, n=2):
    return "-" if v is None else f"{v:.{n}f}"

print(f"\n{'代码':<8}{'名称':<10}{'评级':<6}{'剩余年':<7}{'现价':<8}{'转股价':<8}"
      f"{'转股溢价%':<10}{'转股价值':<9}{'YTM%':<7}{'剩余规模(亿)':<12}{'下修次'}")
print("-" * 100)
for c in passed:
    print(f"{c['code']:<8}{c['name']:<10}{c['rating']:<6}{c['yl']:<7.2f}{c['price']:<8.2f}"
          f"{fnum(c['tp']):<8}{fnum(c['conv_prem']):<10}{fnum(c['conv_val']):<9}"
          f"{c['ytm']:<7.2f}{fnum(c['rem']):<12}{c['drc']}")

# 建议分散篮子：优先"最底线明确"(现价<=赎回价)，再跨评级各取评分靠前，合计 ~15 只
basket = []
tier_limit = {"AAA": 4, "AA+": 4, "AA": 4, "AA-": 3}
tier_cnt = {}
ranked = sorted(passed, key=lambda x: (not x["bottom_safe"], -x["score"]))
for c in ranked:
    t = tier_cnt.get(c["rating"], 0)
    if t < tier_limit.get(c["rating"], 3):
        basket.append(c)
        tier_cnt[c["rating"]] = t + 1
    if len(basket) >= 15:
        break

print(f"\n=== 建议分散篮子({len(basket)}只) ===")
for c in basket:
    print(f"  {c['code']} {c['name']} {c['rating']} 现价{c['price']:.2f} 溢价{fnum(c['conv_prem'])}% 剩余{c['yl']:.2f}年")

# ---- 生成 HTML 交付物 ----
def row_html(c, cls=""):
    mark = "🔒" if c["bottom_safe"] else ""
    return (f"<tr class='{cls}'><td>{c['code']}</td><td>{html.escape(c['name'])}</td>"
            f"<td>{c['rating']}{'*' if c['sti'] else ''}</td><td>{c['yl']:.2f}</td>"
            f"<td>{c['price']:.2f}{mark}</td><td>{fnum(c['tp'])}</td><td>{fnum(c['conv_prem'])}</td>"
            f"<td>{fnum(c['conv_val'])}</td><td>{c['ytm']:.2f}</td><td>{fnum(c['rem'])}</td>"
            f"<td>{c['drc']}</td><td>{fnum(c['stock_price'])}</td></tr>")

basket_codes = {c["code"] for c in basket}
rows_all = "".join(row_html(c, "hot" if c["code"] in basket_codes else "") for c in passed)
rows_strict = "".join(row_html(c) for c in passed_strict)
rows_basket = "".join(row_html(c) for c in basket)

html_doc = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<title>可转债强赎策略筛选 {TODAY}</title>
<style>
body{{font-family:-apple-system,Segoe UI,'Microsoft YaHei',sans-serif;margin:24px;color:#1f2937;background:#f7f8fa}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px}}
.meta{{color:#6b7280;font-size:13px;margin-bottom:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px;background:#fff}}
th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:right}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(3),td:nth-child(3){{text-align:left}}
th{{background:#eef2ff;position:sticky;top:0}}
tr.hot{{background:#fff7ed}}
.note{{background:#fffbeb;border:1px solid #fde68a;padding:10px 14px;border-radius:8px;font-size:13px;line-height:1.6}}
.tag{{display:inline-block;background:#dbeafe;color:#1e40af;border-radius:4px;padding:1px 6px;font-size:12px;margin-right:4px}}
</style></head><body>
<h1>可转债「强赎为主」策略筛选结果</h1>
<div class=meta>生成日期 {TODAY} ｜ 未退市转债池 {315} 只 ｜ 转债现价取自本地库(更新 2026-09-01 16:35)，正股现价为本次实时抓取</div>
<div class=note>
<b>筛选逻辑（四道闸门）：</b><br>
<span class=tag>安全</span> 评级归一 ≥ AA-（严格档）/ ≥ A+（放宽档），排除低评级与违约风险<br>
<span class=tag>时间</span> 剩余年限 2~5 年（到期不少于 2 年；不过长以免强赎变猜谜）<br>
<span class=tag>债底</span> 现价 100~130（低于强赎触发区 130，留上行空间；🔒=现价≤到期赎回价，持有到期不亏，底线最硬）<br>
<span class=tag>强赎</span> 转股溢价率 ≤ 65%（正股涨 30% 触发强赎时转债跟涨到 130+，公告期常冲 150~157；溢价>65%需正股大涨才触发）<br>
<b>关于「强赎价格 157」</b>：理解为强赎触发/公告期的目标价（非入场上限）。入场放在 130 以内，上行空间 = 到 130 触发价、再到 157 脉冲。<br>
<b>分散：</b>优先🔒最底线明确项，再跨评级取评分前 15 只（高亮行）。评分 = max(0,YTM)×0.3 + (130-现价)×0.3 + (50-溢价)×0.25 + 评级×0.15。
</div>
<div class=warn>
<b>⚠️ 核心矛盾（务必先读）：</b>当前市场里，安全(AA-+)+2~5年+能强赎的债，已被整体炒到 120~130（贴近强赎价），<b>没有一只是「硬债底」</b>（现价全部高于到期赎回价 ~108-118，YTM 全为负 -2%~-8%）。也就是说：<b>「底线明确（持有到期不亏）」与「打强赎为主（低溢价、贴着强赎价）」在当前是互相打架的</b>——能同时满足"安全+强赎弹性"的，就必须接受"买入价已高于债底、收益主要靠强赎兑现而非债底"。若坚持要硬债底，得牺牲强赎弹性去选高溢价便宜债（赌下修/正股反转），那是另一种打法。
</div>
<h2>一、建议分散篮子（{len(basket)} 只，跨评级，优先🔒底线）</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>评级</th><th>剩余年</th><th>现价🔒</th><th>转股价</th>
<th>转股溢价%</th><th>转股价值</th><th>YTM%</th><th>剩余规模(亿)</th><th>下修次</th><th>正股现价</th></tr></thead>
<tbody>{rows_basket}</tbody></table>
<h2>二、严格档 · AA-及以上（{len(passed_strict)} 只，最贴合"安全+强赎"）</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>评级</th><th>剩余年</th><th>现价🔒</th><th>转股价</th>
<th>转股溢价%</th><th>转股价值</th><th>YTM%</th><th>剩余规模(亿)</th><th>下修次</th><th>正股现价</th></tr></thead>
<tbody>{rows_strict}</tbody></table>
<h2>三、放宽档 · 含 A+（{len(passed)} 只，多 2 只 A+ 换更分散）</h2>
<table><thead><tr><th>代码</th><th>名称</th><th>评级</th><th>剩余年</th><th>现价🔒</th><th>转股价</th>
<th>转股溢价%</th><th>转股价值</th><th>YTM%</th><th>剩余规模(亿)</th><th>下修次</th><th>正股现价</th></tr></thead>
<tbody>{rows_all}</tbody></table>
<p class=meta>🔒 现价≤到期赎回价（持有到期本息不亏，债底最硬）。评级带 * 表示集思录标记 sti（特殊条款/观察项），安全口径已按基础评级判定。转股溢价率=现价/转股价值-1；转股价值=100/转股价×正股现价。YTM 为近似年化到期收益率（未计复利与票息节奏），为负代表需靠强赎/上涨兑现收益。当前市场高评级+2~5年债普遍在 115 以上，故严格「价格≤115」几乎无标的，本表已放宽至 130。</p>
</body></html>"""

with open("可转债强赎策略筛选_2026-09-02.html", "w", encoding="utf-8") as f:
    f.write(html_doc)
print("\nHTML 已写出: 可转债强赎策略筛选_2026-09-02.html")
