const RATE_LIMIT_MAX_REQUESTS = 60;
const RATE_LIMIT_PERIOD_SECONDS = 60;
const QUOTE_CACHE_TTL_MS = 2_000;

// The quote cache is intentionally isolate-local and best-effort. It only
// coalesces duplicate upstream loads for a couple of seconds; it is not a
// security boundary and never backs current-price history.
const quoteCache = new Map();
const quoteInflight = new Map();

function clientKey(request) {
  const cfIp = request.headers.get("cf-connecting-ip");
  if (cfIp) {
    return cfIp.trim();
  }

  // Local tests/previews do not always provide Cloudflare request metadata.
  // Production traffic is expected to carry CF-Connecting-IP.
  return "anonymous";
}

export async function checkRequestLimit(request, rateLimiter) {
  const url = new URL(request.url);
  const key = `${clientKey(request)}:${url.pathname}`;

  if (!rateLimiter || typeof rateLimiter.limit !== "function") {
    return {
      allowed: false,
      unavailable: true,
      limit: RATE_LIMIT_MAX_REQUESTS,
      retryAfterSeconds: RATE_LIMIT_PERIOD_SECONDS,
      key,
    };
  }

  try {
    const result = await rateLimiter.limit({ key });
    return {
      allowed: Boolean(result?.success),
      unavailable: false,
      limit: RATE_LIMIT_MAX_REQUESTS,
      retryAfterSeconds: RATE_LIMIT_PERIOD_SECONDS,
      key,
    };
  } catch {
    // Fail closed if the configured security binding itself is unavailable.
    return {
      allowed: false,
      unavailable: true,
      limit: RATE_LIMIT_MAX_REQUESTS,
      retryAfterSeconds: RATE_LIMIT_PERIOD_SECONDS,
      key,
    };
  }
}

export async function getCachedQuote(code, loader, now = Date.now()) {
  const cached = quoteCache.get(code);
  if (cached && cached.expiresAt > now) {
    return cached.value;
  }

  const pending = quoteInflight.get(code);
  if (pending) {
    return pending;
  }

  const loadPromise = Promise.resolve()
    .then(loader)
    .then((value) => {
      quoteCache.set(code, {
        value,
        expiresAt: Date.now() + QUOTE_CACHE_TTL_MS,
      });
      return value;
    })
    .finally(() => {
      quoteInflight.delete(code);
    });

  quoteInflight.set(code, loadPromise);
  return loadPromise;
}

export function resetAbuseProtectionForTests() {
  quoteCache.clear();
  quoteInflight.clear();
}

export const ABUSE_PROTECTION_POLICY = Object.freeze({
  rateLimitMaxRequests: RATE_LIMIT_MAX_REQUESTS,
  rateLimitPeriodSeconds: RATE_LIMIT_PERIOD_SECONDS,
  quoteCacheTtlMs: QUOTE_CACHE_TTL_MS,
  rateLimitBackend: "Cloudflare Rate Limiting binding",
});
