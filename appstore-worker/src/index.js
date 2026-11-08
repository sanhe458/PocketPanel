/**
 * PocketPanel 应用商店 · Cloudflare Worker
 *
 * 只做一件事：把「有哪些应用、每个应用怎么装」以 JSON 发出去。
 * 数据放 KV（读多写少，免费额度足够），KV 空的时候退回内置清单，
 * 这样刚部署完还没灌数据也能用。
 *
 * 路由：
 *   GET  /v1/health          健康检查
 *   GET  /v1/catalog         全量清单（ETag / 304）
 *   GET  /v1/apps/:id        单个应用
 *   POST /v1/admin/catalog   覆盖写入清单（需要 ADMIN_KEY）
 *   GET  /                   一行说明页
 */

const VERSION = "1.0.0";
const CATALOG_KEY = "catalog:v1";
const MAX_BODY = 512 * 1024;

// KV 里没数据时的兜底清单：和 seed/catalog.json 保持同步
const FALLBACK = {
  version: 1,
  updated: "2026-10-30",
  apps: [],
};

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "X-Store-Key, Content-Type, If-None-Match",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Expose-Headers": "ETag",
};

function json(data, status = 200, extra = {}) {
  return new Response(JSON.stringify(data, null, 1), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-cache",
      ...CORS,
      ...extra,
    },
  });
}

/** 常量时间比较，避免按字符逐位试探 */
function safeEqual(a, b) {
  const x = String(a || "");
  const y = String(b || "");
  if (!y) return true;              // 没配置就是不校验
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

/** FNV-1a，用来算弱 ETag */
function hash(text) {
  let h = 2166136261;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(16);
}

async function loadCatalog(env) {
  let raw = null;
  try {
    raw = await env.APP_STORE.get(CATALOG_KEY);
  } catch (e) {
    // KV 没绑或读失败都不该让接口挂掉
    return { catalog: FALLBACK, raw: JSON.stringify(FALLBACK) };
  }
  if (!raw) {
    return { catalog: FALLBACK, raw: JSON.stringify(FALLBACK) };
  }
  try {
    return { catalog: JSON.parse(raw), raw };
  } catch (e) {
    return { catalog: FALLBACK, raw: JSON.stringify(FALLBACK) };
  }
}

function pickKey(req, url) {
  return req.headers.get("X-Store-Key") || url.searchParams.get("key") || "";
}

function html(title, body) {
  return new Response(
    `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<style>body{font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:640px;margin:60px auto;padding:0 20px;color:#1f2328}
code{background:#f6f8fa;padding:2px 6px;border-radius:4px;font-size:13px}
h1{font-size:20px;margin-bottom:4px}p{color:#59636e}</style>
<h1>${title}</h1>${body}`,
    { headers: { "Content-Type": "text/html; charset=utf-8", ...CORS } }
  );
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    if (path === "/" ) {
      const { catalog } = await loadCatalog(env);
      return html(
        "PocketPanel 应用商店",
        `<p>版本 ${VERSION} · 共 ${(catalog.apps || []).length} 个应用</p>
         <p>接口：<code>GET /v1/catalog</code> · <code>GET /v1/apps/:id</code> · <code>GET /v1/health</code></p>`
      );
    }

    if (path === "/v1/health") {
      const { catalog } = await loadCatalog(env);
      return json({
        ok: true, version: VERSION,
        apps: (catalog.apps || []).length,
        updated: catalog.updated || "",
        keyed: Boolean(env.STORE_KEY),
      });
    }

    // 下面这些要校验 key
    if (!safeEqual(pickKey(req, url), env.STORE_KEY)) {
      return json({ error: "unauthorized", message: "缺少或错误的 X-Store-Key" }, 401);
    }

    if (path === "/v1/catalog") {
      const { raw } = await loadCatalog(env);
      const etag = `W/"${hash(raw)}"`;
      const inm = req.headers.get("If-None-Match") || "";
      if (inm && inm === etag) {
        return new Response(null, { status: 304, headers: { ETag: etag, ...CORS } });
      }
      return new Response(raw, {
        status: 200,
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "public, max-age=300",
          "ETag": etag,
          ...CORS,
        },
      });
    }

    const m = path.match(/^\/v1\/apps\/([A-Za-z0-9._-]{1,64})$/);
    if (m) {
      const { catalog } = await loadCatalog(env);
      const app = (catalog.apps || []).find((a) => a.id === m[1]);
      if (!app) return json({ error: "not_found", message: `没有应用 ${m[1]}` }, 404);
      return json(app, 200, { "Cache-Control": "public, max-age=300" });
    }

    // 管理员覆盖写清单
    if (path === "/v1/admin/catalog" && req.method === "POST") {
      if (!safeEqual(pickKey(req, url), env.ADMIN_KEY)) {
        return json({ error: "forbidden", message: "需要 ADMIN_KEY" }, 403);
      }
      const body = await req.text();
      if (body.length > MAX_BODY) {
        return json({ error: "too_large", message: "清单超过 512KB" }, 413);
      }
      let data;
      try {
        data = JSON.parse(body);
      } catch (e) {
        return json({ error: "bad_json", message: "不是合法 JSON" }, 400);
      }
      if (!Array.isArray(data.apps)) {
        return json({ error: "bad_shape", message: "缺少 apps 数组" }, 400);
      }
      data.updated = data.updated || new Date().toISOString().slice(0, 10);
      await env.APP_STORE.put(CATALOG_KEY, JSON.stringify(data));
      return json({ ok: true, apps: data.apps.length, updated: data.updated });
    }

    return json({ error: "not_found", message: `没有这个接口：${path}` }, 404);
  },
};
