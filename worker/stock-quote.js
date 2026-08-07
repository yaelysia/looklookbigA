import {
  ABUSE_PROTECTION_POLICY,
  checkRequestLimit,
  getCachedQuote,
} from "./abuse-protection.js";

const STOCKS = [
  {
    code: "002558",
    secid: "0.002558",
    displayName: "巨人网络",
  },
  {
    code: "600795",
    secid: "1.600795",
    displayName: "国电电力",
  },
];

const FIELDS =
  "f43,f44,f45,f46,f47,f48,f57,f58,f60,f71,f86,f169,f170,f171";

const WORKING_QUOTE_WORKER =
  "https://a-share-quote.2949293866.workers.dev/quote";

const NO_CACHE_HEADERS = {
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  "CDN-Cache-Control": "no-store",
  "Cloudflare-CDN-Cache-Control": "no-store",
  Pragma: "no-cache",
  Expires: "0",
  "X-Content-Type-Options": "nosniff",
};

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatChinaTimeFromUnix(timestamp) {
  if (!timestamp || !Number.isFinite(Number(timestamp))) {
    return "未知";
  }

  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(Number(timestamp) * 1000));
}

function formatChinaTime(date = new Date()) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatNumber(value, digits = 2) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "-";
  }

  return number.toFixed(digits);
}

function formatAmount(value) {
  const number = Number(value);

  if (!Number.isFinite(number)) {
    return "-";
  }

  return `${(number / 100000000).toFixed(2)}亿元`;
}

async function fetchQuote(stock) {
  return getCachedQuote(stock.code, async () => {
    const upstreamUrl = new URL(WORKING_QUOTE_WORKER);
    upstreamUrl.searchParams.set("code", stock.code);

    // 每次真正访问上游时生成不同URL，避免命中不可控的旧行情缓存。
    // 当前Worker自身只做2秒best-effort短缓存，用于合并突发重复请求。
    upstreamUrl.searchParams.set("_", Date.now().toString());

    const response = await fetch(upstreamUrl.toString(), {
      method: "GET",
      headers: {
        Accept: "application/json, text/plain, */*",
        Referer: "https://quote.eastmoney.com/",
      },
    });

    if (!response.ok) {
      throw new Error(
        `${stock.code}上游请求失败，状态码：${response.status}`,
      );
    }

    let payload;

    try {
      payload = await response.json();
    } catch {
      throw new Error(`${stock.code}上游没有返回有效JSON`);
    }

    const quote = payload?.quote;

    if (!quote) {
      throw new Error(`${stock.code}没有获得行情数据`);
    }

    return quote;
  });
}

function renderQuoteCard(quote) {
  const changePercent = Number(quote.changePercent);
  const directionClass =
    changePercent > 0
      ? "up"
      : changePercent < 0
        ? "down"
        : "flat";

  const changePrefix = changePercent > 0 ? "+" : "";

  return `
    <section class="quote-card">
      <header class="quote-header">
        <div>
          <h2>${escapeHtml(quote.name)}</h2>
          <div class="code">${escapeHtml(quote.code)}</div>
        </div>

        <div class="price-block ${directionClass}">
          <div class="latest">${formatNumber(quote.latest)}</div>
          <div class="change">
            ${changePrefix}${formatNumber(quote.change)}
            ·
            ${changePrefix}${formatNumber(quote.changePercent)}%
          </div>
        </div>
      </header>

      <div class="grid">
        <div class="item">
          <span>今开</span>
          <strong>${formatNumber(quote.open)}</strong>
        </div>

        <div class="item">
          <span>最高</span>
          <strong>${formatNumber(quote.high)}</strong>
        </div>

        <div class="item">
          <span>最低</span>
          <strong>${formatNumber(quote.low)}</strong>
        </div>

        <div class="item">
          <span>昨收</span>
          <strong>${formatNumber(quote.previousClose)}</strong>
        </div>

        <div class="item">
          <span>均价</span>
          <strong>${formatNumber(quote.average)}</strong>
        </div>

        <div class="item">
          <span>振幅</span>
          <strong>${formatNumber(quote.amplitudePercent)}%</strong>
        </div>

        <div class="item">
          <span>成交额</span>
          <strong>${formatAmount(quote.amountRaw)}</strong>
        </div>

        <div class="item">
          <span>行情时间</span>
          <strong>${escapeHtml(quote.marketTime)}</strong>
        </div>
      </div>
    </section>
  `;
}

function renderHtml(quotes) {
  const cards = quotes.map(renderQuoteCard).join("\n");

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta
    name="viewport"
    content="width=device-width, initial-scale=1"
  >

  <meta
    http-equiv="Cache-Control"
    content="no-store, no-cache, must-revalidate"
  >
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">

  <meta
    name="description"
    content="A股实时行情查询，支持巨人网络（002558）和国电电力（600795），提供最新价格、涨跌幅、开盘价、最高价、最低价、均价、振幅、成交额和行情时间。"
  >
  <link rel="canonical" href="https://uploaded-code-site.zhangjinhao949792.chatgpt.site/">

  <title>A股实时行情｜巨人网络002558｜国电电力600795</title>

  <style>
    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      padding: 32px 18px;
      background: #f4f6f8;
      color: #1f2937;
      font-family:
        system-ui,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        "Microsoft YaHei",
        sans-serif;
    }

    .container {
      width: min(900px, 100%);
      margin: 0 auto;
    }

    h1 {
      margin: 0 0 6px;
      font-size: 26px;
    }

    .description {
      margin-bottom: 22px;
      color: #667085;
      font-size: 14px;
    }

    .quote-card {
      margin-bottom: 18px;
      padding: 22px;
      border: 1px solid #e5e7eb;
      border-radius: 14px;
      background: white;
      box-shadow: 0 5px 16px rgba(0, 0, 0, 0.05);
    }

    .quote-header {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 20px;
    }

    h2 {
      margin: 0 0 5px;
      font-size: 22px;
    }

    .code {
      color: #667085;
      font-size: 14px;
    }

    .price-block {
      text-align: right;
    }

    .latest {
      font-size: 30px;
      font-weight: 700;
      line-height: 1;
    }

    .change {
      margin-top: 7px;
      font-size: 14px;
      font-weight: 600;
    }

    .up {
      color: #d92d20;
    }

    .down {
      color: #039855;
    }

    .flat {
      color: #475467;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }

    .item {
      padding: 12px;
      border-radius: 9px;
      background: #f8fafc;
    }

    .item span {
      display: block;
      margin-bottom: 5px;
      color: #667085;
      font-size: 12px;
    }

    .item strong {
      font-size: 15px;
      overflow-wrap: anywhere;
    }

    .footer {
      margin-top: 18px;
      color: #667085;
      font-size: 12px;
      line-height: 1.7;
    }

    @media (max-width: 700px) {
      .grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .quote-header {
        align-items: flex-end;
      }
    }
  </style>
</head>

<body>
  <main class="container">
    <h1>A股实时行情</h1>

    <div class="description">
      A股实时行情查询，当前支持巨人网络（002558）和国电电力（600795）。
      页面由服务器生成；短时间内的重复访问会复用最多2秒的服务端行情结果，避免重复请求上游。<br>
      数据源：东方财富｜服务器生成时间：
      ${escapeHtml(formatChinaTime())}
    </div>

    ${cards}

    <div class="footer">
      页面由Cloudflare Worker服务器端生成，不依赖浏览器执行JavaScript。
      浏览器端不缓存行情；公共行情路由由Cloudflare托管限流，服务器仅做极短TTL的best-effort上游去重。
    </div>
  </main>
</body>
</html>`;
}

function rateLimitHeaders(rateLimit) {
  if (!rateLimit) {
    return {};
  }

  return {
    "X-RateLimit-Limit": String(rateLimit.limit),
  };
}

function jsonResponse(data, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(data, null, 2), {
    status,
    headers: {
      ...NO_CACHE_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      ...extraHeaders,
    },
  });
}

function rateLimitedResponse(requestUrl, rateLimit) {
  const unavailable = Boolean(rateLimit.unavailable);
  const status = unavailable ? 503 : 429;
  const message = unavailable ? "Rate limiter unavailable" : "Rate limit exceeded";
  const headers = {
    ...rateLimitHeaders(rateLimit),
    "Retry-After": String(rateLimit.retryAfterSeconds),
  };

  if (requestUrl.pathname === "/quote") {
    return jsonResponse(
      {
        error: message,
        retryAfterSeconds: rateLimit.retryAfterSeconds,
      },
      status,
      headers,
    );
  }

  return new Response(unavailable ? "Service Unavailable" : "Too Many Requests", {
    status,
    headers: {
      ...NO_CACHE_HEADERS,
      "Content-Type": "text/plain; charset=utf-8",
      ...headers,
    },
  });
}

export default {
  async fetch(request, rateLimiter) {
    const requestUrl = new URL(request.url);
    const protectedRoute =
      requestUrl.pathname === "/" || requestUrl.pathname === "/quote";
    const rateLimit = protectedRoute
      ? await checkRequestLimit(request, rateLimiter)
      : null;

    if (rateLimit && !rateLimit.allowed) {
      return rateLimitedResponse(requestUrl, rateLimit);
    }

    try {
      // 保留原来的单只股票JSON接口
      if (requestUrl.pathname === "/quote") {
        const code = requestUrl.searchParams.get("code");

        const stock = STOCKS.find((item) => item.code === code);

        if (!stock) {
          return jsonResponse(
            {
              error: "Unsupported stock code",
              supportedCodes: STOCKS.map((item) => item.code),
            },
            400,
            rateLimitHeaders(rateLimit),
          );
        }

        const quote = await fetchQuote(stock);

        return jsonResponse(
          {
            source: "Eastmoney",
            fetchedAt: new Date().toISOString(),
            quote,
            abuseProtection: {
              rateLimitBackend: ABUSE_PROTECTION_POLICY.rateLimitBackend,
              rateLimitMaxRequests: ABUSE_PROTECTION_POLICY.rateLimitMaxRequests,
              rateLimitPeriodSeconds: ABUSE_PROTECTION_POLICY.rateLimitPeriodSeconds,
              serverQuoteCacheTtlMs: ABUSE_PROTECTION_POLICY.quoteCacheTtlMs,
            },
          },
          200,
          rateLimitHeaders(rateLimit),
        );
      }

      // 固定根地址：直接返回服务器生成的HTML
      if (requestUrl.pathname === "/") {
        const quotes = await Promise.all(
          STOCKS.map((stock) => fetchQuote(stock)),
        );

        const html = renderHtml(quotes);

        return new Response(html, {
          status: 200,
          headers: {
            ...NO_CACHE_HEADERS,
            "Content-Type": "text/html; charset=utf-8",
            ...rateLimitHeaders(rateLimit),
          },
        });
      }

      return new Response("Not Found", {
        status: 404,
        headers: {
          ...NO_CACHE_HEADERS,
          "Content-Type": "text/plain; charset=utf-8",
        },
      });
    } catch (error) {
      const message =
        error instanceof Error ? error.message : String(error);

      const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>行情获取失败</title>
</head>
<body>
  <h1>行情获取失败</h1>
  <p>${escapeHtml(message)}</p>
  <p>时间：${escapeHtml(formatChinaTime())}</p>
</body>
</html>`;

      return new Response(html, {
        status: 502,
        headers: {
          ...NO_CACHE_HEADERS,
          "Content-Type": "text/html; charset=utf-8",
          ...rateLimitHeaders(rateLimit),
        },
      });
    }
  },
};
