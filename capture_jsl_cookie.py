# -*- coding: utf-8 -*-
"""集思录 Cookie 抓取（管理后台按钮触发 / 命令行均可）。

打开【可见】Chromium 跳登录页，用户扫码或账号密码登录；以「打 cb_list_new 接口行数>=200」
判定真·登录态，自动抓取全站 cookie 写入 jsl_cookie.txt，并触发 refresh_remaining_scales()
补全剩余规模真值。运行状态写入 jsl_cookie_status.json，管理后台前端轮询展示。

设计要点：
  - 必须可见浏览器，因为需要人扫码/输密码登录；无图形界面的服务器环境会直接报错提示。
  - 登录态判定不再依赖页面「退出」字样（扫码后界面可能不跳转导致误判），改为直接拿 cookie
    打集思录 API 翻页验证，>=200 行即真·登录 -> 落盘。扫码会话一旦绑定，验证立即通过。
  - 落盘后自动刷新剩余规模，并把写入只数回写进状态文件，前端可即时看到成效。
  - jsl_cookie.txt 已被 .gitignore 忽略，切勿入库。
"""
import os
import time
import json
import urllib.parse
import urllib.request
from datetime import datetime
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE_DIR, "jsl_cookie.txt")
STATUS_FILE = os.path.join(BASE_DIR, "jsl_cookie_status.json")
LOGIN_URL = "https://www.jisilu.cn/login/"
DEADLINE_SEC = 300          # 等待登录的最长时长（秒）
POLL_SEC = 2                # 轮询间隔（秒）


def _write_status(d):
    d.setdefault("updated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def jsl_rows_with_cookie(cookie_str, rp=50, max_pages=40):
    """用 cookie 直接 POST 集思录 cb_list_new 翻页，返回抓到的行数（登录态~500，游客~30）。"""
    t = int(time.time() * 1000)
    url = "https://www.jisilu.cn/data/cbnew/cb_list_new/?___jsl=LST___t=%d" % t
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.jisilu.cn/data/cbnew/",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Cookie": cookie_str,
    }
    out = 0
    for page in range(1, max_pages + 1):
        payload = [
            ("fprice", ""), ("tprice", ""), ("curr_iss_amt", ""), ("volume", ""),
            ("svolume", ""), ("premium_rt", ""), ("ytm_rt", ""), ("market", ""),
            ("rating_cd", ""), ("is_search", "N"),
            ("market_cd[]", "shmb"), ("market_cd[]", "shkc"),
            ("market_cd[]", "szmb"), ("market_cd[]", "szcy"),
            ("btype", ""), ("listed", "Y"), ("qflag", "N"), ("sw_cd", ""),
            ("bond_ids", ""), ("rp", str(rp)), ("page", str(page)),
        ]
        body = urllib.parse.urlencode(payload, doseq=True).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print("[verify] 第%d页请求失败: %s" % (page, e), flush=True)
            break
        rows = (data.get("rows") or [])
        out += len(rows)
        if len(rows) < rp:
            break
    return out


def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = 0
    cookie_len = 0
    _write_status({
        "running": True, "stage": "opening", "started_at": started,
        "finished_at": None, "message": "正在打开浏览器，请稍候…",
        "rows": 0, "cookie_len": 0, "refreshed": 0,
    })
    print("[capture] 启动可见浏览器，准备打开集思录登录页 …", flush=True)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False, args=["--start-maximized"])
            except Exception as e:
                _write_status({
                    "running": False, "stage": "error",
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "无法启动浏览器（当前环境可能无图形界面，或 Playwright 浏览器未安装）：%s" % e,
                    "rows": 0, "cookie_len": 0, "refreshed": 0,
                })
                print("[capture] 浏览器启动失败：%s" % e, flush=True)
                return

            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            _write_status({
                "running": True, "stage": "waiting_login", "started_at": started,
                "message": "浏览器已打开，请扫码或账号密码登录集思录（无需页面跳转，登录态以数据接口验证为准）…",
                "rows": 0, "cookie_len": 0, "refreshed": 0,
            })
            print("[capture] 请登录集思录。登录态以 API 验证为准，无需页面跳转。", flush=True)
            print("[capture] 扫码绑定会话后 ~2 秒内自动抓取并落盘，然后关闭浏览器。", flush=True)

            deadline = time.time() + DEADLINE_SEC
            last_rows = -1
            done = False
            while time.time() < deadline:
                cookies = ctx.cookies()
                cookie_str = "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies)
                try:
                    rows = jsl_rows_with_cookie(cookie_str)
                except Exception:
                    rows = -1
                if rows != last_rows:
                    _write_status({
                        "running": True, "stage": "waiting_login", "started_at": started,
                        "message": "等待登录中… 当前 cookie 可拉取集思录 %d 行（登录态需 >=200 行）" % max(rows, 0),
                        "rows": max(rows, 0), "cookie_len": len(cookie_str), "refreshed": 0,
                    })
                    last_rows = rows
                if rows >= 200:
                    with open(OUT, "w", encoding="utf-8") as f:
                        f.write(cookie_str)
                    cookie_len = len(cookie_str)
                    _write_status({
                        "running": True, "stage": "saving", "started_at": started,
                        "message": "登录态验证通过（%d 行），已写入 cookie，正在补全剩余规模…" % rows,
                        "rows": rows, "cookie_len": cookie_len, "refreshed": 0,
                    })
                    print("[capture] 验证通过(>=200行)，已写入 %s（%d 字符）" % (OUT, cookie_len), flush=True)
                    done = True
                    break
                time.sleep(POLL_SEC)

            browser.close()

            if not done:
                _write_status({
                    "running": False, "stage": "timeout",
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "message": "超时：%d 秒内未检测到真·登录态（接口行数<200）。请重新点击按钮，扫码后务必在手机上点「确认登录」。"
                               % DEADLINE_SEC,
                    "rows": 0, "cookie_len": 0, "refreshed": 0,
                })
                print("[capture] 超时：未检测到真·登录态。", flush=True)
                return
    except Exception as e:
        _write_status({
            "running": False, "stage": "error",
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": "抓取过程异常：%s" % e,
            "rows": 0, "cookie_len": 0, "refreshed": 0,
        })
        print("[capture] 异常：%s" % e, flush=True)
        return

    # 已写入 cookie，自动补全剩余规模真值
    refreshed = 0
    try:
        import checkup
        import db
        try:
            db.init_db()
        except Exception:
            pass
        refreshed = checkup.refresh_remaining_scales()
    except Exception as e:
        _write_status({
            "running": False, "stage": "done_refresh_failed",
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": "Cookie 已写入（%d 字符），但自动补全剩余规模失败：%s" % (cookie_len, e),
            "rows": rows, "cookie_len": cookie_len, "refreshed": 0,
        })
        print("[capture] 刷新剩余规模失败：%s" % e, flush=True)
        return

    _write_status({
        "running": False, "stage": "done",
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": "完成：Cookie 已写入（%d 字符），本次补全剩余规模真值 %d 只。/bonds 列表与筛选即时生效。"
                   % (cookie_len, refreshed),
        "rows": rows, "cookie_len": cookie_len, "refreshed": refreshed,
    })
    print("[capture] EXIT done, refreshed=%s" % refreshed, flush=True)


if __name__ == "__main__":
    main()
