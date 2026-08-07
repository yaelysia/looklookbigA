import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ABUSE_PROTECTION_POLICY,
  checkRequestLimit,
  getCachedQuote,
  resetAbuseProtectionForTests,
} from "../worker/abuse-protection.js";

function request(path = "/quote", ip = "203.0.113.10") {
  return new Request(`https://example.test${path}`, {
    headers: {
      "CF-Connecting-IP": ip,
    },
  });
}

function fakeRateLimiter(results) {
  const calls = [];
  let index = 0;
  return {
    calls,
    async limit({ key }) {
      calls.push(key);
      const success = results[Math.min(index, results.length - 1)];
      index += 1;
      return { success };
    },
  };
}

test("Cloudflare rate-limit binding is keyed by client and route", async () => {
  const limiter = fakeRateLimiter([true, false]);
  const req = request("/quote", "203.0.113.10");

  const allowed = await checkRequestLimit(req, limiter);
  const blocked = await checkRequestLimit(req, limiter);

  assert.equal(allowed.allowed, true);
  assert.equal(allowed.unavailable, false);
  assert.equal(blocked.allowed, false);
  assert.equal(blocked.unavailable, false);
  assert.equal(blocked.retryAfterSeconds, 60);
  assert.deepEqual(limiter.calls, [
    "203.0.113.10:/quote",
    "203.0.113.10:/quote",
  ]);
});

test("rate limiter fails closed when binding is unavailable", async () => {
  const result = await checkRequestLimit(request(), null);
  assert.equal(result.allowed, false);
  assert.equal(result.unavailable, true);
  assert.equal(result.retryAfterSeconds, 60);
});

test("managed rate-limit binding is configured and wired into public routes", () => {
  const viteText = readFileSync("vite.config.ts", "utf8");
  const indexText = readFileSync("worker/index.ts", "utf8");
  const quoteText = readFileSync("worker/stock-quote.js", "utf8");
  const protectionText = readFileSync("worker/abuse-protection.js", "utf8");

  assert.match(viteText, /ratelimits\s*:/);
  assert.match(viteText, /name:\s*"PUBLIC_QUOTE_RATE_LIMITER"/);
  assert.match(viteText, /limit:\s*60/);
  assert.match(viteText, /period:\s*60/);
  assert.match(indexText, /PUBLIC_QUOTE_RATE_LIMITER:\s*RateLimit/);
  assert.match(
    indexText,
    /stockQuoteWorker\.fetch\(request, env\.PUBLIC_QUOTE_RATE_LIMITER\)/,
  );
  assert.match(quoteText, /await checkRequestLimit\(request, rateLimiter\)/);
  assert.doesNotMatch(protectionText, /requestBuckets/);
});

test("short quote cache coalesces concurrent and immediate duplicate loads", async () => {
  resetAbuseProtectionForTests();
  let loads = 0;
  const loader = async () => {
    loads += 1;
    await Promise.resolve();
    return { latest: 12.34 };
  };

  const [first, second] = await Promise.all([
    getCachedQuote("002558", loader),
    getCachedQuote("002558", loader),
  ]);
  const third = await getCachedQuote("002558", loader);

  assert.deepEqual(first, { latest: 12.34 });
  assert.deepEqual(second, first);
  assert.deepEqual(third, first);
  assert.equal(loads, 1);
  assert.equal(ABUSE_PROTECTION_POLICY.quoteCacheTtlMs, 2_000);
  assert.equal(
    ABUSE_PROTECTION_POLICY.rateLimitBackend,
    "Cloudflare Rate Limiting binding",
  );
});

test("failed upstream loads are not cached", async () => {
  resetAbuseProtectionForTests();
  let loads = 0;

  await assert.rejects(
    getCachedQuote("002558", async () => {
      loads += 1;
      throw new Error("synthetic upstream failure");
    }),
    /synthetic upstream failure/,
  );

  const recovered = await getCachedQuote("002558", async () => {
    loads += 1;
    return { latest: 12.35 };
  });

  assert.deepEqual(recovered, { latest: 12.35 });
  assert.equal(loads, 2);
});
