// Cloudflare Worker 範例：隱藏 FinMind token，避免前端外洩
// 部署：貼到 Cloudflare Worker，設定環境變數 FINMIND_TOKEN

export default {
  async fetch(req, env) {
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type",
      "Access-Control-Allow-Methods": "GET, OPTIONS",
    };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });

    const url = new URL(req.url);
    const dataset = url.searchParams.get("dataset") || "TaiwanStockMonthRevenue";
    const data_id = url.searchParams.get("data_id") || "2330";
    const start_date = url.searchParams.get("start_date") || "2023-01-01";

    const api = new URL("https://api.finmindtrade.com/api/v4/data");
    api.searchParams.set("dataset", dataset);
    api.searchParams.set("data_id", data_id);
    api.searchParams.set("start_date", start_date);

    const res = await fetch(api, {
      headers: { Authorization: `Bearer ${env.FINMIND_TOKEN}` },
    });
    const body = await res.text();
    return new Response(body, {
      status: res.status,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  },
};
