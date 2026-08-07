const RATE_LIMIT_WINDOW_MS = 60_000;
const RATE_LIMIT_MAX_REQUESTS = 60;
const MAX_TRACKED_CLIENTS = 4096;
const QUOTE_CACHE_TTL_MS = 2_000;

const requestBuckets = new Map();
const quoteCache = new Map();
const quoteInflight = new Map();
let checksSincePrune = 0;

function clientKey(request) {
  const cfIp = request.headers.get("cf-connecting-ip");
  if (cfIp) {
    return cfIp.trim();
  }

  const forwarded = request.headers.get("x-forwarded-for");
  if (forwarded) {
    return forwarded.split(",", 1)[0].trim();
  }

  return "anonymous";
}

function pruneExpiredBuckets(now) {
  for (const [key, bucket] of requestBuckets) {
    if (now - bucket.windowStartedAt >= RATE_LIMIT_WINDOW_MS) {
      requestBuckets.delete(key);
    }
  }

  if (requestBuckets.size <= MAX_TRACKED_CLIENTS) {
    return;
  }

  const oldest = [...requestBuckets.entries()]
    .sort((a, b) => a[1].windowStartedAt - b[1].windowStartedAt)
    .slice(0, requestBuckets.size - MAX_TRACKED_CLIENTS);

  for (const [key] of oldest) {
    requestBuckets.delete(key);
  }
}

export function checkRequestLimit(request, now = Date.now()) {
  checksSincePrune += 1;
  if (checksSincePrune >= 128 || requestBuckets.size > MAX_TRACKED_CLIENTS) {
    pruneExpiredBuckets(now);
    checksSincePrune = 0;
  }

  const url = new URL(request.url);
  const key = `${clientKey(request)}:${url.pathname}`;
  let bucket = requestBuckets.get(key);

  if (!bucket || now - bucket.windowStartedAt >= RATE_LIMIT_WINDOW_MS) {
    bucket = { windowStartedAt: now, count: 0 };
    requestBuckets.set(key, bucket);
  }

  if (bucket.count >= RATE_LIMIT_MAX_REQUESTS) {
    const retryAfterSeconds = Math.max(
      1,
      Math.ceil(
        (RATE_LIMIT_WINDOW_MS - (now - bucket.windowStartedAt)) / 1000,
      ),
    );
    return {
      allowed: false,
      limit: RATE_LIMIT_MAX_REQUESTS,
      remaining: 0,
      retryAfterSeconds,
    };
  }

  bucket.count += 1;
  return {
    allowed: true,
    limit: RATE_LIMIT_MAX_REQUESTS,
    remaining: Math.max(0, RATE_LIMIT_MAX_REQUESTS - bucket.count),
    retryAfterSeconds: 0,
  };
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
  requestBuckets.clear();
  quoteCache.clear();
  quoteInflight.clear();
  checksSincePrune = 0;
}

export const ABUSE_PROTECTION_POLICY = Object.freeze({
  rateLimitWindowMs: RATE_LIMIT_WINDOW_MS,
  rateLimitMaxRequests: RATE_LIMIT_MAX_REQUESTS,
  quoteCacheTtlMs: QUOTE_CACHE_TTL_MS,
});
