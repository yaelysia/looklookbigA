import assert from "node:assert/strict";
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

test("public quote limiter returns 429 boundary state after burst limit", () => {
  resetAbuseProtectionForTests();
  const now = 1_000_000;
  const req = request();

  for (let i = 0; i < ABUSE_PROTECTION_POLICY.rateLimitMaxRequests; i += 1) {
    const result = checkRequestLimit(req, now);
    assert.equal(result.allowed, true);
  }

  const blocked = checkRequestLimit(req, now);
  assert.equal(blocked.allowed, false);
  assert.equal(blocked.remaining, 0);
  assert.equal(blocked.retryAfterSeconds, 60);

  const reset = checkRequestLimit(
    req,
    now + ABUSE_PROTECTION_POLICY.rateLimitWindowMs + 1,
  );
  assert.equal(reset.allowed, true);
});

test("rate counters are isolated by route and client", () => {
  resetAbuseProtectionForTests();
  const now = 2_000_000;

  const quoteA = checkRequestLimit(request("/quote", "203.0.113.10"), now);
  const rootA = checkRequestLimit(request("/", "203.0.113.10"), now);
  const quoteB = checkRequestLimit(request("/quote", "203.0.113.11"), now);

  assert.equal(quoteA.remaining, ABUSE_PROTECTION_POLICY.rateLimitMaxRequests - 1);
  assert.equal(rootA.remaining, ABUSE_PROTECTION_POLICY.rateLimitMaxRequests - 1);
  assert.equal(quoteB.remaining, ABUSE_PROTECTION_POLICY.rateLimitMaxRequests - 1);
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
