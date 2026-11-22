#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cnki-lit — 上财机构权限的知网检索/摘要/下载 CLI（供 DSH 插件调用）。

认证链路：门户 TGC → CARSI(SAML) → 知网机构会话
信任建立：首次滑块验证后保存浏览器 storage_state，之后纯 HTTP

用法:
  python cnki_lit.py auth --status
  python cnki_lit.py auth --login
  python cnki_lit.py auth --trust          # 弹浏览器，过一次滑块
  python cnki_lit.py search "数字化转型" --limit 20 --json
  python cnki_lit.py detail <url> --json
  python cnki_lit.py download <url> -o out.pdf
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.parse

if os.name == "nt":
    os.system("chcp 65001 > nul")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CONF = os.path.join(os.path.expanduser("~"), ".config", "cnki-lit")
STATE = os.path.join(CONF, "state.json")        # 浏览器 storage_state
PORTAL = os.path.join(os.path.expanduser("~"), ".config", "sufe-portal-cli", "cookies.json")
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", CONF), "cnki-lit", "profile")

IDP = "https://idpcarsi.shufe.edu.cn/idp/shibboleth"
KUA = ("YSTT4HG0,LSTPFY1C,EMRPGLPA,JUP3MUPD,MPMFIG1A,"
       "WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R")
FIELD = {"SU": "主题", "TI": "篇名", "KY": "关键词", "AB": "摘要", "AU": "作者", "LY": "来源"}


class Err(Exception):
    def __init__(self, code, msg):
        self.code, self.msg = code, msg
        super().__init__(f"[{code}] {msg}")


def _sess():
    s = requests.Session()
    s.trust_env = False                     # 关键：绕开 127.0.0.1:7897 代理
    s.headers["User-Agent"] = UA
    return s


def _load_state():
    if not os.path.isfile(STATE):
        raise Err("NO_SESSION", "未建立信任态，先运行 auth --login 再 auth --trust")
    return json.load(open(STATE, encoding="utf-8"))


def _portalcookies():
    if not os.path.isfile(PORTAL):
        raise Err("NO_PORTAL", "找不到门户登录态，先运行: sufe-portal-cli auth --qrcode")
    return json.load(open(PORTAL, encoding="utf-8"))["cookies"]


def _submit_form(resp):
    m = re.search(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', resp.text, re.S | re.I)
    if not m:
        return None, None
    data = {}
    for inp in re.finditer(r"<input[^>]*>", m.group(2), re.I):
        nm = re.search(r'name="([^"]+)"', inp.group(0))
        if not nm:
            continue
        vm = re.search(r'value="([^"]*)"', inp.group(0))
        data[nm.group(1)] = html.unescape(vm.group(1)) if vm else ""
    url = urllib.parse.urljoin(resp.url, html.unescape(m.group(1)))
    return url, data


# ---------------------------------------------------------------- auth
def carsi_login():
    """TGC → CARSI → 返回已认证的 requests.Session（仅 fsso 域）。"""
    s = _sess()
    for c in _portalcookies():
        s.cookies.set(c["name"], c["value"], domain="login.sufe.edu.cn", path="/")

    sp = ("https://fsso.cnki.net/Shibboleth.sso/Login?entityID="
          + urllib.parse.quote(IDP, safe="")
          + "&target=" + urllib.parse.quote("https://fsso.cnki.net/", safe=""))
    r = s.get(sp, timeout=30)
    for _ in range(8):
        url, data = _submit_form(r)
        if not url:
            break
        r = s.post(url, data=data, timeout=30, allow_redirects=True,
                   headers={"Referer": r.url})
    v = s.get("https://fsso.cnki.net/Shibboleth.sso/Session", timeout=20)
    if "Identity Provider" not in v.text:
        raise Err("CARSI_FAILED", "CARSI 未建立会话，门户登录态可能已过期")
    aff = re.search(r"affiliation[^:]*:\s*\d+\s*value", v.text)
    return s, bool(aff)


def auth_status():
    try:
        st = _load_state()
    except Err as e:
        return {"ok": False, "code": e.code, "message": e.msg}
    names = sorted({c["name"] for c in st["cookies"] if "cnki" in c["domain"]})
    out = {"ok": True, "cookies": len(names), "key": [n for n in names
           if n in ("Ecp_ClientId", "fingerprint", "Ecp_LoginStuts", "SID_kns_new")]}
    try:
        s = session()
        r = s.get("https://kns.cnki.net/kns8s/defaultresult/index?kw=%E6%B5%8B%E8%AF%95", timeout=30)
        t = re.search(r"<title>(.*?)</title>", r.text, re.S)
        title = t.group(1).strip() if t else ""
        out["probe"] = title
        out["alive"] = "安全验证" not in title and "检索" in title
    except Exception as e:
        out["alive"] = False
        out["probe"] = type(e).__name__
    return out


def auth_trust():
    """开浏览器过一次滑块，保存 storage_state。"""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE, exist_ok=True)
    os.makedirs(CONF, exist_ok=True)
    s0 = carsi_login()[0]
    ck = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/"}
          for c in s0.cookies if "cnki" in (c.domain or "") and not c.name.startswith("__")]

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=False, user_agent=UA,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled", "--start-maximized"])
        ctx.add_cookies(ck)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://kns.cnki.net/kns8s/defaultresult/index?kw=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD",
                  timeout=90000)
        ok = False
        for _ in range(100):
            time.sleep(2)
            try:
                if "安全验证" not in page.title() and "Loading" not in page.title():
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            ctx.close()
            raise Err("TRUST_TIMEOUT", "等待滑块验证超时")
        page.wait_for_timeout(2500)
        state = ctx.storage_state()
        json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)
        ctx.close()
    return {"ok": True, "state": STATE,
            "cookies": len([c for c in state["cookies"] if "cnki" in c["domain"]])}


# ---------------------------------------------------------------- session
_cache = {}


def session():
    if "s" in _cache:
        return _cache["s"]
    st = _load_state()
    s = _sess()
    s.headers["Referer"] = "https://kns.cnki.net/"
    for c in st["cookies"]:
        if "cnki" in c["domain"]:
            s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    _cache["s"] = s
    return s


# ---------------------------------------------------------------- search
def search(kw, field="SU", page=1, size=20, start_year=None, end_year=None):
    # 知网只接受 20 / 50 两档 pageSize，其他值返回“参数校验失败”
    page_size = 50 if size > 20 else 20
    qj = {"Platform": "", "Resource": "CROSSDB", "Classid": "WD0FTY92", "Products": "",
          "QNode": {"QGroup": [{"Key": "Subject", "Title": "", "Logic": 0,
                    "Items": [{"Field": field, "Value": kw, "Operator": "TOPRANK",
                               "Logic": 0, "Vector": "", "Title": FIELD.get(field, "主题")}],
                    "ChildItems": []}]},
          "ExScope": 1, "SimpTrad": "0", "SearchType": 2, "Rlang": "CHINESE",
          "KuaKuCode": KUA, "Expands": {}, "View": "changeDBCh", "SearchFrom": 1}
    data = {"boolSearch": "true", "QueryJson": json.dumps(qj, ensure_ascii=False),
            "pageNum": str(page), "pageSize": str(page_size),
            "sortField": "", "sortType": "", "dstyle": "listmode", "productStr": "",
            "aside": f"({FIELD.get(field, '主题')}：{kw})", "searchFrom": "资源范围：总库",
            "subject": "", "language": "", "uniplatform": "", "CurPage": str(page)}
    r = session().post("https://kns.cnki.net/kns8s/brief/grid", data=data, timeout=60)
    if "vericode" in r.text:
        raise Err("CAPTCHA_REQUIRED", "翻页触发验证码，请换关键词或缩小范围")
    if "安全验证" in r.text:
        raise Err("TRUST_EXPIRED", "信任态失效，重新运行 auth --trust")
    if "校验失败" in r.text:
        raise Err("BAD_PARAM", _clean(re.search(r'value="([^"]*)"', r.text).group(1)))
    total = re.search(r"共找到\s*</span>\s*<em>\s*([\d,]+)\s*</em>", r.text)
    return {"total": total.group(1) if total else None,
            "items": _parse_rows(r.text)[:size]}


def _clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\u00a0", " ").strip()


def _parse_rows(t):
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.S):
        ti = re.search(r"""class=["']fz14["'][^>]*>(.*?)</a>""", row, re.S)
        if not ti:
            continue
        get = lambda cls: re.search(rf"""class=["']{cls}["'][^>]*>(.*?)</td>""", row, re.S)
        au, so, dt, db = get("author"), get("source"), get("date"), get("data")
        link = re.search(r"""class=["']fz14["'][^>]*href=["']([^"']+)["']""", row)
        out.append({"title": _clean(ti.group(1)),
                    "authors": _clean(au.group(1)).rstrip(";；") if au else "",
                    "source": _clean(so.group(1)) if so else "",
                    "date": _clean(dt.group(1)) if dt else "",
                    "db": _clean(db.group(1)) if db else "",
                    "url": html.unescape(link.group(1)) if link else ""})
    return out


# ---------------------------------------------------------------- detail
def detail(url):
    r = session().get(url, timeout=45)
    if r.status_code != 200:
        raise Err("DETAIL_FAILED", f"HTTP {r.status_code}")
    t = r.text
    def pick(pat):
        m = re.search(pat, t, re.S)
        return _clean(m.group(1)) if m else ""
    out = {
        "title": pick(r'<h1[^>]*id="[^"]*title"[^>]*>(.*?)</h1>') or pick(r"<title>(.*?)</title>"),
"abstract": _clean_abs(pick(r'class="abstract-text"[^>]*>(.*?)</div>')),
        "keywords": [k.strip() for k in re.split(r"[;；]", pick(r'class="keywords"[^>]*>(.*?)</p>')) if k.strip()],
        "authors": pick(r'class="author"[^>]*>(.*?)</h3>'),
        "source": pick(r'class="top-tip"[^>]*>(.*?)</a>'),
        "url": url,
    }
    out["pdf_links"] = [html.unescape(x) for x in
                        re.findall(r'href="([^"]*bar\.cnki\.net/bar/download/order[^"]*)"', t)]
    return out


def _clean_abs(s):
    s = re.sub(r"AbstractFilter\([^)]*\);?", "", s)
    s = re.split(r"\s*更多|\s*还原", s)[0]
    return re.sub(r"\s+", " ", s).strip()


def _safe_name(s, n=80):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:n] or "untitled"


def download(url, out_path, headless=True):
    """用浏览器点「PDF下载」，拦截 Download 事件拿文件。"""
    from playwright.sync_api import sync_playwright
    if not os.path.isdir(PROFILE):
        raise Err("NO_PROFILE", "缺少浏览器信任配置，先运行 auth --trust")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, channel="chrome", headless=headless, user_agent=UA, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=90000)
        page.wait_for_timeout(3000)
        try:
            with page.expect_download(timeout=60000) as di:
                page.get_by_text("PDF下载", exact=True).first.click()
            d = di.value
            if os.path.isdir(out_path) or out_path.endswith(("/", "\\")) or not os.path.splitext(out_path)[1]:
                os.makedirs(out_path, exist_ok=True)
                out_path = os.path.join(out_path, _safe_name(d.suggested_filename, 120))
            else:
                os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
            d.save_as(out_path)
        except Exception as e:
            ctx.close()
            raise Err("DOWNLOAD_FAILED", f"{type(e).__name__}: {str(e)[:120]}")
        ctx.close()
    head = open(out_path, "rb").read(5)
    if head != b"%PDF-":
        raise Err("NOT_PDF", f"下载得到 {head!r}，可能无下载权限或需登录")
    return {"ok": True, "path": out_path, "bytes": os.path.getsize(out_path)}


# ---------------------------------------------------------------- 批量采集 / Zotero
def collect(keyword, limit=20, field="SU", outdir=None, with_pdf=True):
    """检索 → 逐条取摘要 → 下载 PDF → 输出 RIS，可直接导入 Zotero。"""
    outdir = outdir or os.path.join(os.path.expanduser("~"), "cnki-import")
    os.makedirs(outdir, exist_ok=True)
    res = search(keyword, field, 1, limit)
    items = []
    for it in res["items"]:
        rec = dict(it)
        try:
            d = detail(it["url"])
            rec["abstract"] = d["abstract"]
            rec["keywords"] = d["keywords"]
            rec["authors"] = d["authors"] or rec["authors"]
        except Err as e:
            rec["abstract"], rec["keywords"], rec["error"] = "", [], e.code
        if with_pdf:
            try:
                pdf = download(it["url"], os.path.join(outdir, _safe_name(it["title"]) + ".pdf"))
                rec["pdf"] = pdf["path"]
            except Err as e:
                rec["pdf_error"] = e.code
        items.append(rec)
    ris_path = os.path.join(outdir, _safe_name(keyword, 40) + ".ris")
    with open(ris_path, "w", encoding="utf-8") as fh:
        for r in items:
            fh.write("TY  - JOUR\n")
            fh.write(f"TI  - {r['title']}\n")
            for au in re.split(r"[；;]|\s{2,}", r.get("authors", "")):
                if au.strip():
                    fh.write(f"AU  - {au.strip()}\n")
            fh.write(f"JO  - {r.get('source', '')}\n")
            fh.write(f"PY  - {(r.get('date') or '')[:4]}\n")
            fh.write(f"DA  - {r.get('date', '')}\n")
            if r.get("abstract"):
                fh.write(f"AB  - {r['abstract']}\n")
            for kw in r.get("keywords") or []:
                fh.write(f"KW  - {kw}\n")
            fh.write(f"UR  - {r.get('url', '')}\n")
            fh.write("ER  -\n\n")
    return {"ok": True, "count": len(items), "outdir": outdir, "ris": ris_path,
            "pdf": sum(1 for r in items if r.get("pdf")), "items": items}


def review_bundle(keyword, limit=15, field="SU"):
    """检索 + 摘要打包，供上层模型写综述。"""
    res = search(keyword, field, 1, limit)
    out = []
    for it in res["items"]:
        rec = {"title": it["title"], "authors": it["authors"], "source": it["source"],
               "date": it["date"], "url": it["url"]}
        try:
            d = detail(it["url"])
            rec["abstract"] = d["abstract"]
            rec["keywords"] = d["keywords"]
        except Err as e:
            rec["abstract"], rec["keywords"], rec["error"] = "", [], e.code
        out.append(rec)
    return {"ok": True, "keyword": keyword, "total": res["total"], "items": out}


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(prog="cnki-lit")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth")
    a.add_argument("--status", action="store_true")
    a.add_argument("--login", action="store_true")
    a.add_argument("--trust", action="store_true")
    a.add_argument("--json", action="store_true")

    se = sub.add_parser("search")
    se.add_argument("keyword")
    se.add_argument("--field", default="SU", choices=list(FIELD))
    se.add_argument("--page", type=int, default=1)
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--json", action="store_true")

    d = sub.add_parser("detail")
    d.add_argument("url")
    d.add_argument("--json", action="store_true")

    dl = sub.add_parser("download")
    dl.add_argument("url")
    dl.add_argument("-o", "--out", default=".")
    dl.add_argument("--headed", action="store_true", help="显示浏览器窗口")

    co = sub.add_parser("collect")
    co.add_argument("keyword")
    co.add_argument("--limit", type=int, default=10)
    co.add_argument("--field", default="SU", choices=list(FIELD))
    co.add_argument("--outdir", default=None)
    co.add_argument("--no-pdf", action="store_true")

    rv = sub.add_parser("review")
    rv.add_argument("keyword")
    rv.add_argument("--limit", type=int, default=15)
    rv.add_argument("--field", default="SU", choices=list(FIELD))
    rv.add_argument("--out", default=None, help="输出 JSON 路径")

    args = ap.parse_args()
    try:
        if args.cmd == "auth":
            if args.trust:
                out = auth_trust()
            elif args.login:
                s, aff = carsi_login()
                out = {"ok": True, "affiliation": aff,
                       "cookies": len({c.name for c in s.cookies})}
            else:
                out = auth_status()
        elif args.cmd == "search":
            out = search(args.keyword, args.field, args.page, args.limit)
        elif args.cmd == "detail":
            out = detail(args.url)
        elif args.cmd == "download":
            out = download(args.url, args.out, headless=not args.headed)
        elif args.cmd == "collect":
            out = collect(args.keyword, args.limit, args.field, args.outdir, not args.no_pdf)
        elif args.cmd == "review":
            out = review_bundle(args.keyword, args.limit, args.field)
            if args.out:
                json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        else:
            out = download(args.url, args.out)
    except Err as e:
        print(json.dumps({"ok": False, "code": e.code, "message": e.msg},
                         ensure_ascii=False) if getattr(args, "json", False) else f"{e.code}: {e.msg}")
        return 2
    except Exception as e:
        print(json.dumps({"ok": False, "code": "UNEXPECTED", "message": str(e)[:200]},
                         ensure_ascii=False) if getattr(args, "json", False) else f"UNEXPECTED: {e}")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if args.cmd == "search":
            print(f"共 {out['total']} 条，返回 {len(out['items'])} 条")
            for i, it in enumerate(out["items"], 1):
                print(f"{i:2d}. {it['title']}\n     {it['authors']} ｜ {it['source']} ｜ {it['date']}")
        elif args.cmd == "detail":
            print(out["title"]); print(); print(out["abstract"][:500])
        else:
            print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
