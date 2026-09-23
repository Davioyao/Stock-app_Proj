// 台股資料中繼（Cloudflare Worker，免費方案即可）
//
// 為什麼需要它：TWSE / TPEx 的 OpenAPI 沒送跨域標頭，瀏覽器會直接擋掉；
// FinMind 雖可直連，但經由這裡可用服務端 token、不用把 key 暴露在前端。
// 另：TPEx 主機擋 Cloudflare 機房連線，上櫃改走 TWSE 家族的 mopsfin CSV
//（內容與 OpenAPI JSON 一致，已驗證 892 家、數值相同）。
//
// 用法（部署後把 https://<你的子網域>.workers.dev 填進頁面「中繼網址」欄）：
//   GET /?src=twse                                   → 上市每月營收快照
//   GET /?src=tpex                                   → 上櫃每月營收快照
//   GET /?src=finmind&dataset=..&data_id=..&start_date=..
//       → FinMind（優先用服務端 FINMIND_TOKEN；沒設就透傳客戶端 Authorization）
//
// 部署：Cloudflare Dashboard → Workers & Pages → Create → 貼上此檔 → Deploy。
// token（選填）：Settings → Variables → 新增 FINMIND_TOKEN。

const UPSTREAM = {
  twse: "https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
  tpex: "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O",
  tpex_csv: "https://mopsfin.twse.com.tw/opendata/t187ap05_O.csv",
};
const BROWSER_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  Accept: "application/json, text/csv, text/plain, */*",
  "Accept-Language": "zh-TW,zh;q=0.9",
};

function jsonResponse(data, status, cors) {
  return new Response(JSON.stringify(data), {
    status: status || 200,
    headers: { ...cors, "Content-Type": "application/json" },
  });
}

async function fetchText(url, headers, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      headers,
      signal: ctrl.signal,
      redirect: "follow",
    });
    if (!res.ok) throw new Error(`upstream HTTP ${res.status}`);
    return await res.text();
  } finally {
    clearTimeout(timer);
  }
}

// 最小 CSV 解析（含引號逗號、BOM），回物件陣列（第一列為表頭）
function parseCsv(text) {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const rows = [];
  let row = [];
  let cur = "";
  let inQ = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQ) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          cur += '"';
          i++;
        } else {
          inQ = false;
        }
      } else {
        cur += ch;
      }
    } else if (ch === '"') {
      inQ = true;
    } else if (ch === ",") {
      row.push(cur);
      cur = "";
    } else if (ch === "\n") {
      row.push(cur);
      rows.push(row);
      row = [];
      cur = "";
    } else if (ch === "\r") {
      // skip
    } else {
      cur += ch;
    }
  }
  if (cur !== "" || row.length) {
    row.push(cur);
    rows.push(row);
  }
  if (!rows.length) return [];
  const head = rows[0].map((h) => h.trim());
  return rows
    .slice(1)
    .filter((r) => r.length === head.length && r.some((c) => c.trim() !== ""))
    .map((r) =>
      Object.fromEntries(head.map((h, j) => [h, (r[j] ?? "").trim()]))
    );
}

async function getTpex() {
  try {
    return JSON.parse(await fetchText(UPSTREAM.tpex, BROWSER_HEADERS, 12000));
  } catch (e) {
    const csv = await fetchText(UPSTREAM.tpex_csv, BROWSER_HEADERS, 25000);
    return parseCsv(csv);
  }
}

export default {
  async fetch(req, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
    };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    if (req.method !== "GET")
      return new Response("GET only", { status: 405, headers: cors });
    try {
      const url = new URL(req.url);
      const src = url.searchParams.get("src");
      if (src === "twse") {
        const t = await fetchText(UPSTREAM.twse, BROWSER_HEADERS, 20000);
        return jsonResponse(JSON.parse(t), 200, cors);
      }
      if (src === "tpex") return jsonResponse(await getTpex(), 200, cors);
      if (src === "finmind") {
        const api = new URL("https://api.finmindtrade.com/api/v4/data");
        for (const k of ["dataset", "data_id", "start_date"]) {
          const v = url.searchParams.get(k);
          if (v) api.searchParams.set(k, v);
        }
        const clientAuth = req.headers.get("Authorization") || "";
        const token =
          env.FINMIND_TOKEN || clientAuth.replace(/^Bearer\s+/i, "");
        const headers = { ...BROWSER_HEADERS };
        if (token) headers["Authorization"] = `Bearer ${token}`;
        const t = await fetchText(api.toString(), headers, 25000);
        return jsonResponse(JSON.parse(t), 200, cors);
      }
      return new Response("unknown src (twse / tpex / finmind)", {
        status: 400,
        headers: cors,
      });
    } catch (e) {
      return jsonResponse(
        { error: String((e && e.message) || e) },
        502,
        cors
      );
    }
  },
};
