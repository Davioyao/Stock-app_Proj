"""台股月營收 + EPS 比較工具（tkinter 桌面版）

免資料庫：前端直連 TWSE OpenAPI / FinMind，快取存本機 json。
執行：python3 stock_gui.py（圖表需 matplotlib）

功能：多公司搜尋（代號/名稱）、篩選列表、YoY 比較圖、12 個月趨勢、近 8 季 EPS。
"""
import calendar
import http.cookiejar
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(BASE_DIR, ".tw_cache.json")
TOKEN_FILE = os.path.join(BASE_DIR, ".tw_token")  # 舊版，相容用
ENV_FILE = os.path.join(BASE_DIR, ".env")  # FINMIND_TOKEN=xxx（已進 .gitignore）
CACHE_TTL = 12 * 3600
CACHE_SCHEMA = 3  # v3 起快照含上櫃＋市場欄，舊快取自動失效
MAX_SEL = 8

TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

BUILTIN = [
    ("2330", "台積電"), ("2317", "鴻海"), ("2454", "聯發科"),
    ("2303", "聯電"), ("2881", "富邦金"), ("2882", "國泰金"),
    ("1301", "台塑"), ("2002", "中鋼"), ("2412", "中華電"),
    ("2382", "廣達"), ("2308", "台達電"), ("2891", "中信金"),
]
if HAS_MPL:
    matplotlib.rcParams["font.sans-serif"] = [
        "PingFang HK", "Heiti TC", "Microsoft JhengHei",
        "Noto Sans TC", "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False


# ---------- 資料層（純函式，可單獨測試） ----------

def http_get_json(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def to_num(v):
    if v is None or v == "" or v == "--":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


def pick_key(obj, *cands):
    names = list(obj.keys())
    for c in cands:
        if c in obj:
            return c
    for c in cands:
        for n in names:
            if c in n:
                return n
    return None


def norm_row(r, market=""):
    """把 TWSE/TPEx 回傳的一列轉成統一格式（鍵名微調也能相容）。"""
    k_code = pick_key(r, "公司代號", "代號") or "公司代號"
    k_name = pick_key(r, "公司名稱", "名稱") or "公司名稱"
    k_ind = pick_key(r, "產業別") or "產業別"
    k_ym = pick_key(r, "資料年月") or "資料年月"
    k_rev = pick_key(r, "當月營收") or "當月營收"
    k_mom = pick_key(r, "上月比較增減") or "上月比較增減"
    k_yoy = pick_key(r, "去年同月增減") or "去年同月增減"
    k_acc = pick_key(r, "前期比較增減", "累計") or "累計營業收入-前期比較增減(%)"
    return {
        "code": str(r.get(k_code, "") or "").strip(),
        "name": str(r.get(k_name, "") or "").strip(),
        "industry": str(r.get(k_ind, "") or "").strip(),
        "ym": str(r.get(k_ym, "") or "").strip(),
        "rev": to_num(r.get(k_rev)),
        "mom": to_num(r.get(k_mom)),
        "yoy": to_num(r.get(k_yoy)),
        "accYoy": to_num(r.get(k_acc)),
        "market": market,
    }


def finmind_fetch(token, dataset, data_id, start_date):
    qs = urllib.parse.urlencode({
        "dataset": dataset, "data_id": data_id, "start_date": start_date,
    })
    return http_get_json(
        f"{FINMIND_URL}?{qs}",
        headers={"Authorization": f"Bearer {token}"},
    )


def _fin_month_ym(x):
    """FinMind 的 date 是下個月 1 號（公布日），真正的營收月份看 revenue_year/month。"""
    try:
        y = int(x.get("revenue_year"))
        m = int(x.get("revenue_month"))
        if 1 <= m <= 12:
            return f"{y}-{m:02d}"
    except (TypeError, ValueError):
        pass
    return ""


def parse_finmind_revenue(payload):
    rows = payload.get("data") or []
    if not rows:
        return []
    cols = list(rows[0].keys())
    rev_key = next((k for k in cols if "revenue" in k.lower()
                    and "year" not in k.lower() and "month" not in k.lower()
                    and "country" not in k.lower()), "revenue")
    date_key = next((k for k in cols if "date" in k.lower()), "date")
    out = []
    for x in rows[-24:]:
        v = to_num(x.get(rev_key))
        # FinMind 單位是元，MOPS 是千元：統一轉千元才不會差 1000 倍
        out.append({"ym": _fin_month_ym(x) or str(x.get(date_key, ""))[:7],
                    "revenue": round(v / 1000) if v is not None else None})
    return out


def _fin_q(date_str):
    """'2026-06-30' → '2026Q2'（與 MOPS 季標籤一致，混用才不會變兩套 x 軸）。"""
    m = re.match(r"(\d{4})-(\d{2})", str(date_str or ""))
    if not m:
        return ""
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return ""
    return f"{y}Q{(mo - 1) // 3 + 1}"


def parse_finmind_eps(payload):
    data = payload.get("data") or []
    if not data:
        return []
    if "EPS" in data[0] or "eps" in data[0]:
        rows = [{"date": x.get("date"), "value": x.get("EPS", x.get("eps"))}
                for x in data]
    else:
        rows = [x for x in data
                if "EPS" in str(x.get("type", "") or x.get("account", "")).upper()]
    out = []
    for x in rows[-8:]:
        q = _fin_q(x.get("date"))
        v = to_num(x.get("value"))
        if q and v is not None:
            out.append({"q": q, "eps": v})
    return out


# ---------- MOPS 真實歷史（免 key，舊版站 mopsov） ----------
MOPS_BASE = "https://mopsov.twse.com.tw/mops/web"
MOPS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/120.0.0.0 Safari/537.36")
_mops_opener = None


def mops_session(reset=False):
    """建 session（先 GET 查詢頁拿 cookie，否則會被擋）。"""
    global _mops_opener
    if _mops_opener is None or reset:
        cj = http.cookiejar.CookieJar()
        _mops_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        for page in ("t163sb04", "t05st10_ifrs"):
            try:
                req = urllib.request.Request(
                    f"{MOPS_BASE}/{page}", headers={"User-Agent": MOPS_UA})
                _mops_opener.open(req, timeout=30).read()
            except Exception as e:  # noqa: BLE001
                print("mops session fail", page, e)
    return _mops_opener


def mops_post(endpoint, data, refer_page, timeout=40, tries=4):
    """POST MOPS ajax；被節流（擋人頁 / HTTP 307）會換 session＋拉長冷卻重試。"""
    body = urllib.parse.urlencode(data).encode()
    last_err = None
    for attempt in range(tries):
        try:
            if attempt:
                mops_session(reset=True)
                threading.Event().wait(10 * attempt)
            req = urllib.request.Request(
                f"{MOPS_BASE}/{endpoint}", data=body,
                headers={"User-Agent": MOPS_UA,
                         "Referer": f"{MOPS_BASE}/{refer_page}",
                         "X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded"})
            with mops_session().open(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
            if "無法呈現" in text:
                last_err = RuntimeError("MOPS 暫時拒絕（節流），重試中…")
                print(last_err, endpoint)
                continue
            return text
        except urllib.error.HTTPError as e:
            last_err = e
            print(f"mops_post HTTP {e.code}（{endpoint}），冷卻重試…")
        except Exception as e:  # noqa: BLE001
            last_err = e
            print("mops_post fail", endpoint, e)
    raise RuntimeError(f"MOPS 請求失敗（已重試 {tries} 次）：{last_err}")


class _Tables(HTMLParser):
    """stdlib 表格抽取器（取代 pandas.read_html，免依賴）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._stack = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ",
                          "".join(self._cell).replace(" ", " ")).strip()
            self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            done = self._stack.pop()
            if done:
                self.tables.append(done)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def extract_tables(html):
    p = _Tables()
    p.feed(html)
    return p.tables


REV_FORM = {"encodeURIComponent": 1, "step": 1, "firstin": "ture", "off": 1,
            "keyword4": "", "code1": "", "TYPEK2": "", "checkbtn": "",
            "queryName": "co_id", "inpuType": "co_id", "TYPEK": "all",
            "isnew": "false"}


def parse_mops_revenue(html):
    """解析單家單月營收；回 dict(name, rev, prev, yoy)，查無資料回 None。"""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    if "請輸入公司代號" in text or "查無資料" in text:
        return None
    m = re.search(r"本資料由\s*[(（][^)）]+[)）]\s*(\S+?)\s*公司提供", text)
    name = m.group(1) if m else ""
    data = None
    for tb in extract_tables(html):
        flat = [c for row in tb for c in row]
        norm = [c.replace(" ", "") for c in flat]
        if "本月" in norm and "去年同期" in norm:
            data = flat
            break
    if not data:
        return None
    norm = [c.replace(" ", "") for c in data]

    def after(key):
        try:
            i = norm.index(key)
        except ValueError:
            return None
        for c in data[i + 1:]:
            v = to_num(c)
            if v is not None:
                return v
        return None

    rev = after("本月")
    if rev is None:
        return None
    prev = after("去年同期")
    acc = after("本年累計")
    if acc is not None and rev > acc:
        # 單月永遠不會大於當年累計；成立代表抓錯欄，丟棄避免累計數污染趨勢圖
        print("rev>acc, drop", name, rev, acc)
        return None
    yoy = None
    try:
        i = norm.index("增減百分比")
        for c in data[i + 1:]:
            v = to_num(c)
            if v is not None:
                yoy = v
                break
    except ValueError:
        pass
    return {"name": name, "rev": rev, "prev": prev, "yoy": yoy}


def parse_mops_eps(html):
    """解析綜合損益彙總表；回 {代號: (名稱, EPS)}（含各業別表）。"""
    out = {}
    for tb in extract_tables(html):
        hdr = next((i for i, r in enumerate(tb)
                    if any("公司代號" in c.replace(" ", "") for c in r)), None)
        if hdr is None:
            continue
        hrow = [c.replace(" ", "") for c in tb[hdr]]
        ci = next((j for j, c in enumerate(hrow) if "公司代號" in c), 0)
        ni = next((j for j, c in enumerate(hrow) if "公司名稱" in c), 1)
        ei = next((j for j, c in enumerate(hrow) if "每股盈餘" in c), None)
        if ei is None:
            continue
        for r in tb[hdr + 1:]:
            if len(r) <= max(ci, ni, ei):
                continue
            code = re.sub(r"\D", "", r[ci])[:4]
            if len(code) != 4:
                continue
            out[code] = (r[ni].strip(), to_num(r[ei]))
    return out


def quarterly_from_cumulative(pairs):
    """MOPS 綜合損益為累計制（Q2=上半年、Q3=前三季、Q4=全年），轉回單季 EPS。

    pairs: [(label, value)]，label 如 '2026Q2'，需按時間排序。
    回 {label: 單季值}。
    """
    by_year = {}
    for label, v in pairs:
        by_year.setdefault(int(label[:4]), {})[int(label[5:])] = v
    out = {}
    for y in sorted(by_year):
        prev = 0.0
        for q in sorted(by_year[y]):
            v = by_year[y][q]
            if v is None:
                out[f"{y}Q{q}"] = None
            else:
                out[f"{y}Q{q}"] = round(v - prev, 2)
                prev = v
    return out


def _ym_key(ym):
    """年月轉 (年, 月)：通吃 '11508'（民國）、'2026-08'（西元）。"""
    s = re.sub(r"\D", "", str(ym or ""))
    if len(s) == 6:
        return (int(s[:4]), int(s[4:]))
    if len(s) == 5:
        return (int(s[:3]) + 1911, int(s[3:]))
    return (0, 0)


def latest_published_quarter(ref=None):
    """最新已公布季：季結束後約 45 天才公布完，不夠久就退回上一季。"""
    now = ref or datetime.now()
    y, q = now.year, (now.month - 1) // 3 + 1
    for _ in range(3):
        last_day = calendar.monthrange(y, q * 3)[1]
        if (now - datetime(y, q * 3, last_day)).days >= 45:
            return (y, q)
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return (y, q)


def eps_window_labels(n=8, ref=None):
    """固定 8 季日曆窗口（錨定最新已公布季），缺的公司留白不斷軸。"""
    ly, lq = latest_published_quarter(ref)
    out = []
    y, q = ly, lq
    for _ in range(n):
        out.append(f"{y}Q{q}")
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return sorted(out)


def rev_window_labels(now=None):
    """固定 24 個月日曆窗口（近 12 個月＋去年同期）。"""
    cur = [f"{y}-{mo:02d}" for y, mo in last_12_months(now)]
    prev = [f"{y - 1}-{mo:02d}" for y, mo in last_12_months(now)]
    return sorted(set(cur + prev))


def candidate_quarters(n_try=12):
    """從本季往回列 [(年, 季)]，抓到 8 季有數字的為止。"""
    now = datetime.now()
    q = (now.month - 1) // 3 + 1
    y = now.year
    out = []
    for _ in range(n_try):
        out.append((y, q))
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return out


def last_12_months(now=None):
    """回最近 12 個「已公布」的 (年, 月)，由新到舊。

    月營收次月 10 日前公布：當月一定還沒出；每月 10 日前連上個月都還沒出齊。
    （之前從當月開始抓，導致當月＋去年同月兩個點一起落空。）
    """
    now = now or datetime.now()
    y, m = now.year, now.month - (2 if now.day < 10 else 1)
    if m <= 0:
        m += 12
        y -= 1
    out = []
    for _ in range(12):
        out.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


def read_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            c = json.load(f)
        if datetime.now().timestamp() - c.get("ts", 0) < CACHE_TTL \
                and c.get("v", 1) == CACHE_SCHEMA:
            return c
    except (OSError, ValueError):
        pass
    return {}


def write_cache(rows, rev_hist=None, eps_hist=None):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"v": CACHE_SCHEMA, "ts": datetime.now().timestamp(),
                       "rows": rows,
                       "rev_hist": rev_hist or {},
                       "eps_hist": eps_hist or {}}, f)
    except OSError:
        pass


def load_token():
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == "FINMIND_TOKEN":
                    v = v.strip().strip('"').strip("'")
                    if v:
                        return v
    except OSError:
        pass
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            old = f.read().strip()
        if old:
            save_token(old)  # 舊檔自動搬到 .env
            return old
    except OSError:
        pass
    return ""


def save_token(token):
    token = token.strip()
    lines = []
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    out, hit = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s \
                and s.split("=", 1)[0].strip() == "FINMIND_TOKEN":
            if not hit:
                out.append(f"FINMIND_TOKEN={token}")
                hit = True
            # 重複行直接丟掉
        else:
            out.append(line)
    if not hit:
        out.append(f"FINMIND_TOKEN={token}")
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        return True
    except OSError:
        return False


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("台股月營收 + EPS 比較")
        self.geometry("1200x920")
        self.rows = []
        self.trend = {}
        self.eps = {}
        self.rev_hist = {}
        self.eps_hist = {}
        self._hist_lock = threading.Lock()
        self._refresh_pending = False
        self._stop_flag = False
        self.selected = []
        self.name_map = {c: n for c, n in BUILTIN}
        self._build_widgets()
        for c in ["2330", "2317"]:
            self.add_code(c, silent=True)
        self.refresh_selected()
        self.set_status("按「載入 TWSE+MOPS」抓最新月營收＋近8季EPS＋24個月營收（真實資料，約需3-5分鐘）。")

    # ----- 版面 -----
    def _build_widgets(self):
        # 1. 資料來源
        f1 = ttk.LabelFrame(self, text="1. 資料來源")
        f1.pack(fill="x", padx=8, pady=4)
        ttk.Button(f1, text="TWSE+MOPS(no key)",
                   command=self.on_load_twse).pack(side="left", padx=4, pady=6)
        ttk.Button(f1, text="FinMind(key)",
                   command=self.on_load_finmind).pack(side="left", padx=4)
        ttk.Button(f1, text="Stop Action",
                   command=self.on_stop).pack(side="left", padx=4)
        ttk.Label(f1, text="token:").pack(side="left", padx=(12, 2))
        self.token_var = tk.StringVar(value=load_token())
        ttk.Entry(f1, textvariable=self.token_var, show="•",
                  width=28).pack(side="left")
        ttk.Button(f1, text="儲存",
                   command=self.on_save_token).pack(side="left", padx=4)
        ttk.Button(f1, text="清除token/中繼站",
                   command=self.on_clear_token).pack(side="left", padx=4)
        self.status_var = tk.StringVar()
        ttk.Label(self, textvariable=self.status_var, foreground="#555",
                  wraplength=1000, justify="left").pack(fill="x", padx=10)

        # 2. 選股
        f2 = ttk.LabelFrame(self, text="2. 選擇比較公司（最多 8 家，可輸入代號或名稱）")
        f2.pack(fill="x", padx=8, pady=4)
        top = ttk.Frame(f2)
        top.pack(fill="x", padx=4, pady=4)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var,
                                      width=30)
        self.search_entry.pack(side="left", padx=2)
        self.search_entry.bind("<Return>", lambda e: self.on_add())
        ttk.Button(top, text="加入比較",
                   command=self.on_add).pack(side="left", padx=4)
        mid = ttk.Frame(f2)
        mid.pack(fill="x", padx=4, pady=4)
        self.chip_frame = ttk.Frame(mid)
        self.chip_frame.pack(side="left", fill="x", expand=True)
        self.marked = set()
        ttk.Button(mid, text="移除所選",
                   command=self.on_remove).pack(side="left", padx=6)
        ttk.Label(f2, text="已選公司（點一下變紅標記，再按「移除所選」）。",
                  foreground="#888").pack(anchor="w", padx=6, pady=(0, 4))

        # 3. 篩選 + 列表
        f3 = ttk.LabelFrame(self, text="3. 篩選列表（雙擊加入比較）")
        f3.pack(fill="both", padx=8, pady=4, expand=True)
        flt = ttk.Frame(f3)
        flt.pack(fill="x", padx=4, pady=4)
        ttk.Label(flt, text="關鍵字").pack(side="left")
        self.kw_var = tk.StringVar()
        ttk.Entry(flt, textvariable=self.kw_var, width=16).pack(side="left", padx=2)
        self.kw_var.trace_add("write", lambda *a: self.render_table())
        ttk.Label(flt, text="產業").pack(side="left", padx=(8, 0))
        self.ind_var = tk.StringVar()
        self.ind_combo = ttk.Combobox(flt, textvariable=self.ind_var,
                                      width=14, state="readonly")
        self.ind_combo.pack(side="left", padx=2)
        self.ind_combo.bind("<<ComboboxSelected>>",
                            lambda e: self.render_table())
        ttk.Label(flt, text="市場").pack(side="left", padx=(8, 0))
        self.mkt_var = tk.StringVar(value="全部")
        mkt_combo = ttk.Combobox(flt, textvariable=self.mkt_var, width=8,
                                 state="readonly",
                                 values=["全部", "上市", "上櫃"])
        mkt_combo.pack(side="left", padx=2)
        mkt_combo.bind("<<ComboboxSelected>>",
                       lambda e: self.render_table())
        ttk.Label(flt, text="YoY").pack(side="left", padx=(8, 0))
        self.yoy_var = tk.StringVar(value="不限")
        yoy_combo = ttk.Combobox(flt, textvariable=self.yoy_var, width=10,
                                 state="readonly",
                                 values=["不限", "> 0%", "> 10%", "> 20%"])
        yoy_combo.pack(side="left", padx=2)
        yoy_combo.bind("<<ComboboxSelected>>",
                       lambda e: self.render_table())
        cols = ("code", "name", "market", "industry", "ym", "rev",
                "mom", "yoy", "acc")
        self.tree = ttk.Treeview(f3, columns=cols, show="headings", height=9)
        heads = ["代號", "名稱", "市場", "產業", "資料年月", "當月營收(千元)",
                 "MoM%", "YoY%", "累計YoY%"]
        widths = [70, 90, 60, 100, 90, 140, 80, 80, 90]
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="e" if c not in
                             ("code", "name", "market", "industry") else "w")
        self.tree.tag_configure("pos", foreground="#c53030")
        self.tree.tag_configure("neg", foreground="#2f855a")
        sb = ttk.Scrollbar(f3, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self.on_tree_double)

        # 4. 比較圖 + 明細
        f4 = ttk.LabelFrame(self, text="4. 比較")
        f4.pack(fill="both", padx=8, pady=4, expand=True)
        top4 = ttk.Frame(f4)
        top4.pack(fill="x", padx=4, pady=(0, 4))
        ttk.Button(top4, text="Updata",
                   command=self.on_refresh_compare).pack(side="left")
        ttk.Label(top4, text="增減公司後按此補齊該公司的趨勢＋EPS。",
                  foreground="#888").pack(side="left", padx=8)
        if HAS_MPL:
            nb = ttk.Notebook(f4)
            nb.pack(fill="both", expand=True)
            self.figs, self.axes, self.canvases = {}, {}, {}
            for key, title in [("yoy", "當月 YoY%"), ("trend", "近24個月營收趨勢"),
                               ("eps", "近8季單季 EPS")]:
                frame = ttk.Frame(nb)
                nb.add(frame, text=title)
                fig = Figure(figsize=(10.5, 4.2), dpi=100)
                ax = fig.add_subplot(111)
                canvas = FigureCanvasTkAgg(fig, master=frame)
                canvas.get_tk_widget().pack(fill="both", expand=True)
                self.figs[key] = fig
                self.axes[key] = ax
                self.canvases[key] = canvas
        else:
            ttk.Label(f4, text="未安裝 matplotlib，圖表停用。請 pip install matplotlib 後重開。",
                      foreground="red").pack(padx=8, pady=8)
        dcols = ("code", "market", "ym", "rev", "mom", "yoy",
                 "acc", "eps1", "ttm")
        self.cmp = ttk.Treeview(f4, columns=dcols, show="headings", height=4)
        for c, h in zip(dcols, ["公司", "市場", "資料年月", "當月營收(千元)",
                                "MoM%", "YoY%", "累計YoY%", "近一季EPS",
                                "近4季合計"]):
            self.cmp.heading(c, text=h)
            self.cmp.column(c, width=60 if c == "market" else 110,
                            anchor="e" if c != "code" else "w")
        self.cmp.pack(fill="x", padx=4, pady=4)

    # ----- 狀態/選股 -----
    def set_status(self, msg):
        self.status_var.set(msg)
        print(msg)

    def resolve_name(self, text):
        """公司名稱→代號：完全符合優先，其次唯一包含；無法唯一對應回空字串。"""
        if not text:
            return ""
        lut = {}
        for c, n in self.name_map.items():
            if n:
                lut.setdefault(n, []).append(c)
        for r in self.rows:
            if r["name"] and r["code"] not in lut.setdefault(r["name"], []):
                lut[r["name"]].append(r["code"])
        if text in lut and len(lut[text]) == 1:
            return lut[text][0]
        cand = {}
        for n, cs in lut.items():
            if text in n and len(cs) == 1:
                cand[cs[0]] = n
        if len(cand) == 1:
            return next(iter(cand))
        return ""

    def add_code(self, raw, silent=False):
        token = str(raw).strip().split()[0] if str(raw).strip() else ""
        if re.fullmatch(r"\d{4}", token):
            code = token
        else:
            code = self.resolve_name(str(raw).strip())
            if not code:
                if not silent:
                    self.set_status(
                        "請輸入股票代號（4 碼，如 2330）或公司名稱"
                        "（須唯一對應，如 緯穎；要先載入一次才有名錄）。")
                return
        if code in self.selected:
            return
        if len(self.selected) >= MAX_SEL:
            self.set_status(f"最多比較 {MAX_SEL} 家，請先移除。")
            return
        self.selected.append(code)
        self.refresh_selected()
        self.render_compare()
        if not silent:
            self.ensure_history(code)

    def ensure_history(self, code):
        """缺資料自動補齊；已齊全只同步顯示不重抓（併入整批背景任務）。"""
        self._apply_cached_history(code)
        if self._history_complete(code):
            self.render_compare()
            return
        self.set_status(f"{code} 缺資料，背景補抓中…")
        self.run_bg(self._refresh_all_background)

    def _lookup_name(self, code):
        if self.name_map.get(code):
            return self.name_map[code]
        r = next((x for x in self.rows if x["code"] == code), None)
        if r and r["name"]:
            self.name_map[code] = r["name"]
            return r["name"]
        return ""

    def disp_name(self, code):
        """圖表用標籤：2330 台積電（無名稱時只顯示代號）。"""
        name = self._lookup_name(code)
        return f"{code} {name}".strip() if name else code

    def refresh_selected(self):
        for w in self.chip_frame.winfo_children():
            w.destroy()
        if not self.selected:
            ttk.Label(self.chip_frame, text="尚未選擇，請搜尋或雙擊下方表格加入。",
                      foreground="#999").pack(side="left")
            return
        for c in self.selected:
            txt = f"{c} {self._lookup_name(c)}".strip()
            lab = tk.Label(self.chip_frame, text=txt, relief="solid",
                           borderwidth=1, padx=8, pady=3,
                           bg="#ffd6d6" if c in self.marked else "#ebf4ff")
            lab.pack(side="left", padx=3)
            lab.bind("<Button-1>", lambda e, c=c: self.toggle_mark(c))

    def toggle_mark(self, code):
        if code in self.marked:
            self.marked.discard(code)
        else:
            self.marked.add(code)
        self.refresh_selected()

    def on_add(self):
        self.add_code(self.search_var.get())
        self.search_var.set("")

    def on_remove(self):
        if not self.marked:
            self.set_status("請先點一下要移除的公司（會變紅），再按「移除所選」。")
            return
        self.selected = [c for c in self.selected if c not in self.marked]
        self.marked.clear()
        self.refresh_selected()
        self.render_compare()

    def on_tree_double(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.add_code(self.tree.item(item)["values"][0])

    def on_save_token(self):
        ok = save_token(self.token_var.get())
        self.set_status("token 已存本機。" if ok else "token 儲存失敗。")

    def on_clear_token(self):
        # 只清輸入框：本機 .env 保留（不上傳），下次啟動照樣讀得到
        self.set_status("已清除輸入框，本機 .env 保留。")
        self.token_var.set("")

    # ----- 載入資料（背景執行緒 + after 回主執行緒） -----
    def run_bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_load_twse(self):
        self.set_status("TWSE+MOPS 載入中…（真實歷史約需3-5分鐘，可先看快照）")
        self.run_bg(self._do_load_twse)

    def on_stop(self):
        self._stop_flag = True
        self.set_status("已要求停止，將在目前請求完成後停下（已抓到的會保留）。")

    def _fetch_snapshot_rows(self):
        twse, tpex = [], []
        try:
            raw = http_get_json(TWSE_URL)
            twse = [r for r in (norm_row(x, "上市") for x in raw)
                    if r["code"]]
        except Exception as e:  # noqa: BLE001
            print("twse fail", e)
        try:
            raw_o = http_get_json(TPEX_URL)
            tpex = [r for r in (norm_row(x, "上櫃") for x in raw_o)
                    if r["code"]]
        except Exception as e:  # noqa: BLE001
            print("tpex fail", e)
        rows = twse + tpex
        if not rows:
            raise RuntimeError("TWSE 與 TPEx 都抓不到")
        return rows

    def _do_load_twse(self):
        try:
            cached = read_cache()
            if cached.get("rows"):
                self.rows = cached["rows"]
                self.rev_hist = cached.get("rev_hist", {})
                self.eps_hist = cached.get("eps_hist", {})
                self.after(0, lambda: self._after_rows("（快取，12h 內）"))
            else:
                self.rows = self._fetch_snapshot_rows()
                write_cache(self.rows, self.rev_hist, self.eps_hist)
                self.after(0, lambda: self._after_rows("（即時）"))
            self._refresh_all_background(force="mops")
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda: self.set_status(f"TWSE 快照載入失敗：{e}"))

    def _apply_history(self):
        for c in self.selected:
            if c in self.rev_hist:
                self.trend[c] = self.rev_hist[c][-24:]
            if c in self.eps_hist:
                self.eps[c] = self.eps_hist[c][-8:]

    def _apply_cached_history(self, code):
        ok = False
        if code in self.rev_hist:
            self.trend[code] = self.rev_hist[code][-24:]
            ok = True
        if code in self.eps_hist:
            self.eps[code] = self.eps_hist[code][-8:]
            ok = True
        return ok

    def _history_complete(self, code):
        """12 個月＋最新已公布季是否齊全（只看快取有無，不看顯示層）。

        只數滿 8 季不夠——舊資料滿 8 季也會被判齊全，新季度永遠補不進來。
        """
        need = {f"{y}-{mo:02d}" for y, mo in last_12_months()}
        have = {p["ym"] for p in self.rev_hist.get(code, [])}
        ly, lq = latest_published_quarter()
        have_q = {p["q"] for p in self.eps_hist.get(code, [])}
        return (need <= have and f"{ly}Q{lq}" in have_q
                and len(self.eps_hist.get(code, [])) >= 8)

    def _refresh_all_background(self, force=None):
        """背景抓取統一入口：單飛鎖＋待辦合併，一次處理全部已選公司。

        force='mops' 強制走 MOPS；'finmind' 強制走 FinMind；
        None 自動：有 token 優先 FinMind（快），FinMind 兜不全的才用 MOPS 備援。
        跑途中又有人按（或新加公司），只記待辦，等本輪結束自動再跑一輪，
        不會開第二條線去加重節流。
        """
        if not self.selected:
            return
        if not self._hist_lock.acquire(blocking=False):
            self._refresh_pending = True
            self.after(0, lambda: self.set_status(
                "歷史抓取進行中，跑完會自動補抓新增的公司。"))
            return
        prog = lambda msg: self.after(0, lambda: self.set_status(msg))  # noqa: E731
        try:
            self._stop_flag = False
            first = True
            while True:
                self._refresh_pending = False
                codes = list(self.selected)
                if not codes:
                    break
                if not first and all(self._history_complete(c)
                                     for c in codes):
                    break
                first = False
                token = self.token_var.get().strip()
                how = force or ("finmind" if token else "mops")
                if how == "finmind" and not token:
                    self.after(0, lambda: self.set_status(
                        "FinMind 需要 token，請先填寫儲存。"))
                    break
                if how == "finmind":
                    self._do_finmind(token, codes)
                    still = [c for c in codes
                             if not self._history_complete(c)]
                    if still and not self._stop_flag:
                        prog("FinMind 缺的部分改用 MOPS 備援抓取…")
                        self._do_history(still)
                else:
                    self._do_history(codes)
                if not self._refresh_pending:
                    break
        finally:
            self._hist_lock.release()

    def _do_history(self, codes):
        """抓 MOPS 真實歷史（由 _refresh_all_background 加鎖呼叫）。"""
        prog = lambda msg: self.after(0, lambda: self.set_status(msg))  # noqa: E731
        fail_streak = 0
        aborted = False
        try:
            ly, lq = latest_published_quarter()
            need_q = f"{ly}Q{lq}"
            need_eps = [c for c in codes
                        if len(self.eps_hist.get(c, [])) < 8
                        or need_q not in {p["q"]
                                          for p in self.eps_hist.get(c, [])}]
            if need_eps:
                kept = []  # [(label, {code: (name, eps)})]
                for y, q in candidate_quarters(12):
                    if self._stop_flag or aborted:
                        break
                    label = f"{y}Q{q}"
                    prog(f"MOPS 綜合損益 {label} 抓取中…")
                    try:
                        html = mops_post("ajax_t163sb04", {
                            "encodeURIComponent": 1, "step": 1,
                            "firstin": 1, "off": 1, "TYPEK": "sii",
                            "year": str(y - 1911),
                            "season": f"{q:02d}"}, "t163sb04")
                        m = parse_mops_eps(html)
                        fail_streak = 0
                    except Exception as e:  # noqa: BLE001
                        # 被節流：等 30 秒重試一次
                        print("eps fail", label, e)
                        prog(f"{label} 被 MOPS 節流，30 秒後重試…")
                        threading.Event().wait(30)
                        try:
                            html = mops_post("ajax_t163sb04", {
                                "encodeURIComponent": 1, "step": 1,
                                "firstin": 1, "off": 1, "TYPEK": "sii",
                                "year": str(y - 1911),
                                "season": f"{q:02d}"}, "t163sb04")
                            m = parse_mops_eps(html)
                            fail_streak = 0
                        except Exception as e2:  # noqa: BLE001
                            print("eps retry fail", label, e2)
                            m = {}
                            fail_streak += 1
                            if fail_streak >= 3:
                                aborted = True
                                prog("MOPS 節流嚴重，本輪先暫停；已抓到的會保留。")
                                break
                    if len(m) > 50:
                        kept.append((label, m))
                        if len(kept) == 8:
                            break
                    threading.Event().wait(4.0)
                kept = kept[-8:]
                if not kept and need_eps:
                    prog("EPS 抓取被 MOPS 節流，請稍後再按「更新趨勢＋EPS」補抓。")
                got = set()
                for _, m in kept:
                    got.update(m)
                missing = [c for c in need_eps if c not in got]
                if missing and kept and not aborted and not self._stop_flag:
                    for label, m in kept:
                        yy, qq = int(label[:4]), int(label[5:])
                        prog(f"MOPS 綜合損益 {label}（上櫃）抓取中…")
                        try:
                            html = mops_post("ajax_t163sb04", {
                                "encodeURIComponent": 1, "step": 1,
                                "firstin": 1, "off": 1, "TYPEK": "otc",
                                "year": str(yy - 1911),
                                "season": f"{qq:02d}"}, "t163sb04")
                            m.update(parse_mops_eps(html))
                        except Exception as e:  # noqa: BLE001
                            print("eps otc fail", label, e)
                        threading.Event().wait(4.0)
                self._merge_eps_kept(kept, need_eps)
            if need_eps:
                # EPS 先存先顯示，不用等月營收跑完
                write_cache(self.rows, self.rev_hist, self.eps_hist)
                self.after(0, self._apply_history)
                self.after(0, self.render_compare)
                self.after(0, lambda: self.set_status("EPS 已先載入顯示，繼續抓月營收…"))
            run_rev = not (aborted or self._stop_flag)
            for j, code in enumerate(codes if run_rev else []):
                if self._stop_flag or aborted:
                    break
                # 缺哪個月補哪個月（之前是湊滿 12 點就跳過，破洞永遠補不上）
                have = {p["ym"] for p in self.rev_hist.get(code, [])}
                months = [(y, mo) for (y, mo) in last_12_months()
                          if f"{y}-{mo:02d}" not in have]
                if not months and code in self.rev_hist:
                    continue
                by_ym = {p["ym"]: p for p in self.rev_hist.get(code, [])}
                for y, mo in months:
                    if self._stop_flag or aborted:
                        break
                    prog(f"{code} {y}-{mo:02d} 月營收…({j + 1}/{len(codes)})")
                    try:
                        html = mops_post("ajax_t05st10_ifrs", {
                            **REV_FORM, "co_id": code,
                            "year": str(y - 1911),
                            "month": f"{mo:02d}"}, "t05st10_ifrs")
                        d = parse_mops_revenue(html)
                    except Exception as e:  # noqa: BLE001
                        # 被節流：等 10 秒重試一次，再不行才跳過該月
                        print("rev fail", code, y, mo, e)
                        prog(f"{code} {y}-{mo:02d} 被節流，30 秒後重試…")
                        threading.Event().wait(30)
                        try:
                            html = mops_post("ajax_t05st10_ifrs", {
                                **REV_FORM, "co_id": code,
                                "year": str(y - 1911),
                                "month": f"{mo:02d}"}, "t05st10_ifrs")
                            d = parse_mops_revenue(html)
                        except Exception as e2:  # noqa: BLE001
                            print("rev retry fail", code, y, mo, e2)
                            fail_streak += 1
                            if fail_streak >= 4:
                                aborted = True
                                prog("MOPS 節流嚴重，本輪先暫停；已抓到的已保留。")
                                d = None
                                break
                            prog(f"{code} 節流持續，冷卻 30 秒再繼續…")
                            threading.Event().wait(30)
                            d = None
                    if d:
                        fail_streak = 0
                        if d["name"]:
                            self.name_map[code] = d["name"]
                        by_ym[f"{y}-{mo:02d}"] = {
                            "ym": f"{y}-{mo:02d}",
                            "revenue": d["rev"], "yoy": d["yoy"]}
                        if d["prev"] is not None:
                            by_ym[f"{y - 1}-{mo:02d}"] = {
                                "ym": f"{y - 1}-{mo:02d}",
                                "revenue": d["prev"], "yoy": None}
                    threading.Event().wait(2.5)
                pts = [p for p in by_ym.values()
                       if p["revenue"] is not None]
                pts.sort(key=lambda p: p["ym"])
                self.rev_hist[code] = pts
                # 每家抓完立刻存檔＋重繪：有部分資料就先顯示
                write_cache(self.rows, self.rev_hist, self.eps_hist)
                self.after(0, self._apply_history)
                self.after(0, self.render_compare)
                self.after(0, lambda c=code: self.set_status(
                    f"{c} 已載入 {len(self.rev_hist.get(c, []))} 筆，圖表已更新。"))
            token = self.token_var.get().strip()
            if token:
                still = [c for c in codes if not self._history_complete(c)]
                for c in still:
                    if self._stop_flag:
                        break
                    prog(f"{c} 改用 FinMind 兜底補齊…")
                    self._do_fetch_one(token, c)
                    threading.Event().wait(1.0)
            self._finish_history(codes, "MOPS")
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda: self.set_status(f"MOPS 歷史載入失敗：{e}"))

    def _after_rows(self, tag):
        self._apply_history()
        for r in self.rows:
            self.name_map[r["code"]] = r["name"]
        inds = sorted({r["industry"] for r in self.rows if r["industry"]})
        self.ind_combo["values"] = [""] + inds
        self.refresh_selected()
        self.render_table()
        self.render_compare()
        ym = self.rows[0]["ym"] if self.rows else "-"
        n1 = sum(1 for r in self.rows if r.get("market") == "上市")
        n2 = sum(1 for r in self.rows if r.get("market") == "上櫃")
        self.set_status(
            f"已載入上市 {n1}＋上櫃 {n2} 家月營收{tag}・資料年月：{ym}。")

    def on_load_finmind(self):
        token = self.token_var.get().strip()
        if not token:
            self.set_status("請先填 FinMind token（官網免費註冊取得）。")
            return
        if not self.selected:
            self.set_status("請先選至少 1 家公司。")
            return
        self.set_status("FinMind 抓取中…（每家 2 次 API，請勿頻繁重按）")
        self.run_bg(lambda: self._refresh_all_background(force="finmind"))

    def _apply_finmind_snapshot(self, code):
        """用 FinMind 24 個月回算當月 MoM/YoY/累計 YoY，寫回快照列。

        FinMind 沒比較新就不蓋（避免把 TWSE 較新的快照倒退嚕）。
        """
        pts = sorted((p for p in self.trend.get(code, [])
                      if p.get("revenue") is not None),
                     key=lambda p: p["ym"])
        if not pts:
            return
        last = pts[-1]
        y, mo = int(last["ym"][:4]), int(last["ym"][5:7])
        m = {p["ym"]: p["revenue"] for p in pts}
        r12 = m.get(f"{y - 1}-{mo:02d}")
        pm = f"{y}-{mo - 1:02d}" if mo > 1 else f"{y - 1}-12"
        r1 = m.get(pm)
        ytd = sum(v for ym, v in m.items()
                  if ym.startswith(f"{y}-") and ym <= last["ym"])
        ytd_prev = sum(v for ym, v in m.items()
                       if ym.startswith(f"{y - 1}-")
                       and ym <= f"{y - 1}-{mo:02d}")
        row = next((x for x in self.rows if x["code"] == code), None)
        if row is not None and _ym_key(row.get("ym")) > (y, mo):
            return
        if row is None:
            row = {"code": code, "name": self.name_map.get(code, ""),
                   "industry": "", "market": ""}
            self.rows.append(row)
        row.update({
            "ym": f"{y - 1911}{mo:02d}",
            "rev": last["revenue"],
            "mom": round((last["revenue"] - r1) / r1 * 100, 2) if r1 else None,
            "yoy": round((last["revenue"] - r12) / r12 * 100, 2) if r12 else None,
            "accYoy": round((ytd - ytd_prev) / ytd_prev * 100, 2)
            if ytd_prev else row.get("accYoy"),
        })

    def _finish_history(self, codes, source):
        """收尾：存檔＋套用＋重繪＋誠實回報缺口（MOPS / FinMind 共用）。"""
        write_cache(self.rows, self.rev_hist, self.eps_hist)
        self.after(0, self._apply_history)
        self.after(0, self.render_compare)
        self.after(0, self.render_table)
        want = set()
        for y, mo in last_12_months():
            want.add(f"{y}-{mo:02d}")
            want.add(f"{y - 1}-{mo:02d}")
        msgs = []
        ly, lq = latest_published_quarter()
        need_q = f"{ly}Q{lq}"
        missing_eps = [c for c in codes
                       if need_q not in {p["q"]
                                         for p in self.eps_hist.get(c, [])}]
        if missing_eps:
            msgs.append(f"{'、'.join(missing_eps)} 缺EPS")
        for c in codes:
            lack = sorted(want - {p["ym"]
                                  for p in self.rev_hist.get(c, [])})
            if lack:
                msgs.append(f"{c} 缺{len(lack)}個月營收")
        if not msgs:
            done = ("MOPS 真實歷史載入完成（近8季EPS＋24個月營收）。"
                    if source == "MOPS" else "FinMind 補完歷史。")
            self.after(0, lambda: self.set_status(done))
        else:
            final = ("尚缺：" + "；".join(msgs)
                     + "。請隔1-2分鐘再按「更新趨勢＋EPS」補齊（只抓缺的）。")
            if self._stop_flag:
                final = "已手動停止；" + final
            self.after(0, lambda: self.set_status(final))

    def _do_fetch_one(self, token, code):
        try:
            rev = parse_finmind_revenue(
                finmind_fetch(token, "TaiwanStockMonthRevenue",
                              code, "2023-01-01"))
            self.trend[code] = rev
            by = {p["ym"]: p for p in self.rev_hist.get(code, [])}
            for p in rev:
                old = by.get(p["ym"], {})
                by[p["ym"]] = {"ym": p["ym"], "revenue": p["revenue"],
                               "yoy": old.get("yoy")}
            self.rev_hist[code] = sorted(by.values(),
                                         key=lambda p: p["ym"])
        except Exception as e:  # noqa: BLE001
            print(code, "月營收失敗", e)
        try:
            eps = parse_finmind_eps(
                finmind_fetch(token, "TaiwanStockFinancialStatements",
                              code, "2022-01-01"))
            self.eps[code] = eps
            byq = {p["q"]: p for p in self.eps_hist.get(code, [])}
            for p in eps:
                byq[p["q"]] = p
            self.eps_hist[code] = sorted(byq.values(), key=lambda p: p["q"])
        except Exception as e:  # noqa: BLE001
            print(code, "EPS 失敗", e)
        self._apply_finmind_snapshot(code)

    def _do_finmind(self, token, codes):
        """FinMind 整批抓取（由 _refresh_all_background 加鎖呼叫）。"""
        prog = lambda msg: self.after(0, lambda: self.set_status(msg))  # noqa: E731
        try:
            if not self.rows:
                # 先補快照（名稱/產業/市場），否則表格全空
                try:
                    self.rows = self._fetch_snapshot_rows()
                    write_cache(self.rows, self.rev_hist, self.eps_hist)
                    self.after(0, lambda: self._after_rows("（即時）"))
                except Exception as e:  # noqa: BLE001
                    print("snapshot fail", e)
            for code in codes:
                if self._stop_flag:
                    break
                if self._history_complete(code):
                    continue
                self._do_fetch_one(token, code)
                threading.Event().wait(1.0)
            self._mops_backfill_recent(codes, prog)
            self._finish_history(codes, "FinMind")
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda: self.set_status(f"FinMind 歷史載入失敗：{e}"))

    def _merge_eps_kept(self, kept, codes):
        """把 MOPS 綜合損益彙總表（累計制）轉單季後併入 eps_hist。

        只合併錨點齊全的季度（q==1，或同年上一季累計也在 kept；
        否則用 hist 已有單季加總回推，推不出就不合併，避免累計數污染）。
        """
        for c in codes:
            cummap = {}
            for lb, m in kept:
                if c in m:
                    if m[c][0]:
                        self.name_map[c] = m[c][0]
                    if m[c][1] is not None:
                        cummap[lb] = m[c][1]
            if not cummap:
                continue
            byq = {p["q"]: p for p in self.eps_hist.get(c, [])}
            for lb in sorted(cummap):
                y, q = int(lb[:4]), int(lb[5:])
                if q == 1:
                    byq[lb] = {"q": lb, "eps": round(cummap[lb], 2)}
                    continue
                anchor = f"{y}Q{q - 1}"
                if anchor in cummap:
                    byq[lb] = {"q": lb,
                               "eps": round(cummap[lb] - cummap[anchor], 2)}
                    continue
                s, ok = 0.0, True
                for qq in range(1, q):
                    key = f"{y}Q{qq}"
                    if key in byq and byq[key]["eps"] is not None:
                        s += byq[key]["eps"]
                    else:
                        ok = False
                        break
                if ok:
                    byq[lb] = {"q": lb, "eps": round(cummap[lb] - s, 2)}
            self.eps_hist[c] = sorted(byq.values(), key=lambda p: p["q"])

    def _mops_backfill_recent(self, codes, prog):
        """FinMind 缺的近期季度，用 MOPS 回補（FinMind 銀行股常慢一兩季）。

        只抓最近兩季＋轉單季用的錨，全市場一包搞定。
        """
        want2 = eps_window_labels()[-2:]
        have = {c: {p["q"] for p in self.eps_hist.get(c, [])} for c in codes}
        if all(all(q in have[c] for q in want2) for c in codes):
            return
        fetch = set()
        for c in codes:
            for q in want2:
                if q not in have[c]:
                    fetch.add(q)
        for lb in list(fetch):
            y, q = int(lb[:4]), int(lb[5:])
            if q > 1:
                fetch.add(f"{y}Q{q - 1}")
        kept = []
        for lb in sorted(fetch):
            if self._stop_flag:
                return
            y, q = int(lb[:4]), int(lb[5:])
            prog(f"FinMind 缺 {lb}，改用 MOPS 回補抓取中…")
            try:
                html = mops_post("ajax_t163sb04", {
                    "encodeURIComponent": 1, "step": 1,
                    "firstin": 1, "off": 1, "TYPEK": "sii",
                    "year": str(y - 1911), "season": f"{q:02d}"}, "t163sb04")
                m = parse_mops_eps(html)
            except Exception as e:  # noqa: BLE001
                print("backfill fail", lb, e)
                prog(f"{lb} 回補被節流，30 秒後重試…")
                threading.Event().wait(30)
                try:
                    html = mops_post("ajax_t163sb04", {
                        "encodeURIComponent": 1, "step": 1,
                        "firstin": 1, "off": 1, "TYPEK": "sii",
                        "year": str(y - 1911), "season": f"{q:02d}"},
                        "t163sb04")
                    m = parse_mops_eps(html)
                except Exception as e2:  # noqa: BLE001
                    print("backfill retry fail", lb, e2)
                    continue
            if len(m) > 50:
                kept.append((lb, m))
            threading.Event().wait(4.0)
        if not kept:
            return
        got = set()
        for _, m in kept:
            got.update(m)
        if any(c not in got for c in codes):
            for lb, m in kept:
                if self._stop_flag:
                    return
                y, q = int(lb[:4]), int(lb[5:])
                try:
                    html = mops_post("ajax_t163sb04", {
                        "encodeURIComponent": 1, "step": 1,
                        "firstin": 1, "off": 1, "TYPEK": "otc",
                        "year": str(y - 1911),
                        "season": f"{q:02d}"}, "t163sb04")
                    m.update(parse_mops_eps(html))
                except Exception as e:  # noqa: BLE001
                    print("backfill otc fail", lb, e)
                threading.Event().wait(4.0)
        self._merge_eps_kept(kept, codes)

    # ----- 渲染 -----
    def filtered_rows(self):
        kw = self.kw_var.get().strip().lower()
        ind = self.ind_var.get()
        min_y = {"不限": -9999, "> 0%": 0, "> 10%": 10, "> 20%": 20}[self.yoy_var.get()]
        rows = list(self.rows)
        if kw:
            rows = [r for r in rows if kw in (r["code"] + r["name"]).lower()]
        if ind:
            rows = [r for r in rows if r["industry"] == ind]
        mkt = self.mkt_var.get()
        if mkt and mkt != "全部":
            rows = [r for r in rows if r.get("market") == mkt]
        rows = [r for r in rows if r["yoy"] is None or r["yoy"] >= min_y]
        rows.sort(key=lambda r: (r["yoy"] if r["yoy"] is not None else -9999),
                  reverse=True)
        return rows[:300]

    @staticmethod
    def fmt(n):
        return "-" if n is None else f"{n:,.0f}"

    def render_table(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.filtered_rows():
            tag = ("pos" if (r["yoy"] or 0) > 0 else
                   "neg" if (r["yoy"] or 0) < 0 else "")
            vals = [v if v is not None else "-" for v in
                    (r["code"], r["name"], r.get("market", ""),
                     r["industry"], r["ym"])]
            nums = [self.fmt(r["rev"]),
                    f"{r['mom']:.2f}" if r["mom"] is not None else "-",
                    f"{r['yoy']:.2f}" if r["yoy"] is not None else "-",
                    f"{r['accYoy']:.2f}" if r["accYoy"] is not None else "-"]
            self.tree.insert("", "end", values=vals + nums, tags=(tag,))

    def on_refresh_compare(self):
        """增減公司後，手動補齊所有已選公司的趨勢＋EPS 並重繪（一次跑完全部）。"""
        if not self.selected:
            self.set_status("尚未選擇公司。")
            return
        self.set_status("檢查並補齊趨勢＋EPS…")
        for c in list(self.selected):
            self._apply_cached_history(c)
        self.render_compare()
        self.run_bg(self._refresh_all_background)

    def render_compare(self):
        self.refresh_selected()
        self.cmp.delete(*self.cmp.get_children())
        for c in self.selected:
            r = next((x for x in self.rows if x["code"] == c), None)
            arr = self.eps.get(c, [])
            last = arr[-1]["eps"] if arr else None
            ttm = (round(sum(x["eps"] for x in arr[-4:]
                             if x["eps"] is not None), 2)
                   if arr else None)
            name = f"{c} {self.name_map.get(c, '') or (r['name'] if r else '')}"
            self.cmp.insert("", "end", values=(
                name, r.get("market", "") if r else "-",
                r["ym"] if r else "-",
                self.fmt(r["rev"]) if r else "-",
                f"{r['mom']:.2f}" if r and r["mom"] is not None else "-",
                f"{r['yoy']:.2f}" if r and r["yoy"] is not None else "-",
                f"{r['accYoy']:.2f}" if r and r["accYoy"] is not None else "-",
                last if last is not None else "-",
                ttm if ttm is not None else "-"))
        if HAS_MPL:
            self.draw_yoy()
            self.draw_trend()
            self.draw_eps()

    def draw_yoy(self):
        ax = self.axes["yoy"]
        ax.clear()
        codes, vals = [], []
        for c in self.selected:
            r = next((x for x in self.rows if x["code"] == c), None)
            codes.append(self.disp_name(c))
            vals.append(r["yoy"] if r and r["yoy"] is not None else 0)
        colors = ["#c53030" if v >= 0 else "#2f855a" for v in vals]
        ax.bar(codes, vals, color=colors)
        ax.set_title("當月營收 YoY%（最新月）")
        ax.set_ylabel("YoY%")
        ax.tick_params(axis="x", rotation=15, labelsize=8)
        for lbl in ax.get_xticklabels():
            lbl.set_ha("right")
        self.figs["yoy"].tight_layout()
        self.canvases["yoy"].draw()

    def draw_trend(self):
        ax = self.axes["trend"]
        ax.clear()
        codes = [c for c in self.selected if self.trend.get(c)]
        if not codes:
            ax.text(0.5, 0.5, "尚無歷史：按「載入 TWSE+MOPS」",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="#888")
            ax.set_title("近24個月營收趨勢（MOPS真實資料）")
            self.canvases["trend"].draw()
            return
        months = rev_window_labels()
        for c in codes:
            m = {p["ym"]: p["revenue"] for p in self.trend[c]}
            ax.plot(months, [m.get(x) for x in months],
                    marker="o", label=self.disp_name(c))
        ax.set_title("近24個月營收趨勢（MOPS真實資料）")
        ax.legend(fontsize=8)
        ax.tick_params(axis="x", rotation=30, labelsize=8)
        for lbl in ax.get_xticklabels():
            lbl.set_ha("right")
        self.figs["trend"].tight_layout()
        self.canvases["trend"].draw()

    def draw_eps(self):
        ax = self.axes["eps"]
        ax.clear()
        codes = [c for c in self.selected if self.eps.get(c)]
        if not codes:
            ax.text(0.5, 0.5, "尚無歷史：按「載入 TWSE+MOPS」",
                    ha="center", va="center", transform=ax.transAxes,
                    fontsize=10, color="#888")
            ax.set_title("近8季單季 EPS（MOPS真實資料）")
            self.canvases["eps"].draw()
            return
        qs = eps_window_labels()
        x = range(len(qs))
        w = 0.8 / max(len(codes), 1)
        for i, c in enumerate(codes):
            m = {p["q"]: p["eps"] for p in self.eps[c]}
            ax.bar([v + i * w for v in x],
                   [m.get(q, 0) for q in qs], width=w,
                   label=self.disp_name(c))
        ax.set_xticks([v + w * (len(codes) - 1) / 2 for v in x])
        ax.set_xticklabels(qs, fontsize=7)
        ax.set_title("近8季單季 EPS（MOPS真實資料）")
        ax.legend(fontsize=8)
        self.figs["eps"].tight_layout()
        self.canvases["eps"].draw()


if __name__ == "__main__":
    if not HAS_MPL:
        messagebox.showwarning("提醒", "未安裝 matplotlib，圖表將停用。\n"
                               "可執行 pip install matplotlib 後重開。")
    App().mainloop()
