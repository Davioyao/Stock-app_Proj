# 台股月營收 + EPS 比較工具

免資料庫的小工具，檔案放在 `Stock-app_Proj/`：網頁版（`index.html`）跟 Python 桌面版（`stock_gui.py`，功能較完整，建議用這個）。

## 開啟方式

```bash
cd Stock-app_Proj
python3 -m http.server 8000
# 瀏覽器開 http://localhost:8000/
```

直接雙擊 `index.html` 也能開，但 TWSE API 有 CORS 限制，建議用上面的本機 server。

## 網頁版三種資料模式（`index.html`）

1. **TWSE（免 key）**：按「載入 TWSE 最新月營收」，抓上市當月快照。含代號、名稱、產業、當月營收、MoM、YoY、累計 YoY。
2. **Demo（免 API）**：按「載入 Demo 趨勢資料」，產生假趨勢，只看版面用。
3. **FinMind（需 token）**：到 FinMind 官網免費註冊拿 token，貼到輸入框按儲存，再按「用 FinMind 補歷史 + EPS」。可拿 24 個月營收歷史、上櫃、近 8 季 EPS。

## 網頁版多公司比較

- 上方搜尋框輸入 `2330` 或 `台積電` 都可，按加入比較（最多 5 家）。
- 篩選列表可用關鍵字、產業、YoY 門檻過濾，勾選加入比較。
- 網址會帶 `?s=2330,2317`，可直接分享比較組合。

## 進階：隱藏 token 的 Proxy

前端直連 FinMind 會暴露 token，自用沒關係，要分享給別人請部署 `worker.js` 到 Cloudflare Worker（設定環境變數 `FINMIND_TOKEN`），再把 `index.html` 內的 `finmindFetch` 改打自己的 Worker 網址。

## Python 桌面版（tkinter）

### 用 conda 開環境（建議）

```bash
conda activate stock_evk
cd Stock-app_Proj
pip install -r requirements.txt  # 第一次才需裝，之後直接 python stock_gui.py
python stock_gui.py
```

註：miniconda 的 Python 在 macOS 通常自帶 tkinter；若啟動時報 tkinter 缺失，再補 `conda install tk -y`。`requirements.txt` 目前只有 `matplotlib>=3.7` 一行。

### 不用 conda 的裝法

```bash
cd Stock-app_Proj
pip install -r requirements.txt
python3 stock_gui.py
```

功能（以下皆為真實資料，無 Demo 假數）：

- **快照**：上市（TWSE OpenAPI）＋上櫃（TPEx OpenAPI）當月營收，免 key。
- **歷史**：近 8 季單季 EPS＋24 個月營收。MOPS 綜合損益是累計制，程式會還原成單季；FinMind 原生就是單季，兩邊已對賬一致。營收一律單月合併（千元）。
- **按鈕分工**：`TWSE+MOPS(no key)` 強制走 MOPS；`FinMind(key)` 強制走 FinMind（它缺的近期季度自動用 MOPS 回補）；`Updata`／新加公司自動選（有 token 優先 FinMind＋MOPS 備援，否則 MOPS）；`Stop Action` 中斷抓取。
- **選擇**：最多 8 家，可輸代號或名稱（名稱自動轉代號，須唯一對應；先載入一次才有名錄）。已選顯示名稱晶片，點一下變紅標記再按「移除所選」。
- **篩選列表**：關鍵字／產業／市場（上市上櫃）／YoY 門檻，雙擊加入比較。
- **比較圖**：當月 YoY、近 24 個月營收趨勢、近 8 季單季 EPS，標籤都是「代號＋名稱」。
- **快取**：`.tw_cache.json` 存 12 小時（有版本號，改版自動作廢重抓）。
- **token**：存 `.env` 的 `FINMIND_TOKEN`（已進 `.gitignore` 不會上傳）。
- **MOPS 節流**：官方會擋短時間大量請求，程式會自動冷卻重試；看到「尚缺」就隔 1-2 分鐘再按 `Updata`（只補缺的）。重複按不會開第二條線，只會排待辦。
