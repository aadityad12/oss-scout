import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  handle, verifyAccess, resetCertCache, parseAct, buildEmail, sendDigest, toBase64, ACTIONS,
} from "../src/index.js";

const TEAM = "team.cloudflareaccess.com";
const ENV = {
  OWNER_LOGIN: "aadityad12",
  OWNER_EMAIL: "aaditya.d.desai@gmail.com",
  DATA_REPO: "aadityad12/oss-scout-data",
  DATA_REF: "claude/scout-data",
  DASHBOARD_URL: "https://me.aadityad.dev",
  ACCESS_TEAM_DOMAIN: TEAM,
  ACCESS_AUD: "aud-123",
  GITHUB_TOKEN: "ghp_test",
  RESEND_API_KEY: "re_test",
};
const NOW = Date.parse("2026-10-02T15:00:00Z");

const b64u = (bytes) => Buffer.from(bytes).toString("base64url");
const ALG = { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" };

async function makeKey(kid) {
  const pair = await crypto.subtle.generateKey(ALG, true, ["sign", "verify"]);
  const jwk = { ...(await crypto.subtle.exportKey("jwk", pair.publicKey)), kid, alg: "RS256", use: "sig" };
  return { pair, jwk, kid };
}

async function sign(key, claims, header = {}) {
  const head = b64u(Buffer.from(JSON.stringify({ alg: "RS256", kid: key.kid, typ: "JWT", ...header })));
  const body = b64u(Buffer.from(JSON.stringify(claims)));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key.pair.privateKey, Buffer.from(`${head}.${body}`));
  return `${head}.${body}.${b64u(new Uint8Array(sig))}`;
}

const goodClaims = (over = {}) => ({
  aud: ["aud-123"], iss: `https://${TEAM}`, email: ENV.OWNER_EMAIL, exp: NOW / 1000 + 600, iat: NOW / 1000, ...over,
});

let key, other, calls;
beforeEach(async () => {
  resetCertCache();
  key ??= await makeKey("k1");
  other ??= await makeKey("k1"); // same kid, different key: a forged signature
  calls = [];
});

// One fetch mock for everything: Access certs, GitHub and Resend.
function fetchMock(routes = {}) {
  return async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url) === `https://${TEAM}/cdn-cgi/access/certs`) return Response.json({ keys: [key.jwk] });
    for (const [prefix, reply] of Object.entries(routes)) {
      if (String(url).startsWith(prefix)) return typeof reply === "function" ? reply(url, init) : reply;
    }
    return new Response("unexpected", { status: 500 });
  };
}
const githubCalls = () => calls.filter((c) => c.url.includes("api.github.com"));

async function req(path, { method = "GET", headers = {}, body, token, claims } = {}) {
  const jwt = token ?? (await sign(key, claims ?? goodClaims()));
  return new Request(`https://me.aadityad.dev${path}`, {
    method, body, headers: { "Cf-Access-Jwt-Assertion": jwt, ...headers },
  });
}
const postAct = (payload, headers = {}) =>
  req("/api/act", {
    method: "POST", body: typeof payload === "string" ? payload : JSON.stringify(payload),
    headers: { "content-type": "application/json", origin: "https://me.aadityad.dev", ...headers },
  });
const run = (request, routes) => handle(request, ENV, { fetchFn: fetchMock(routes), now: NOW });

// -- Access token ------------------------------------------------------------------

test("accepts a valid Access token", async () => {
  const r = await verifyAccess(await req("/"), ENV, { fetchFn: fetchMock(), now: NOW });
  assert.deepEqual(r, { ok: true, email: ENV.OWNER_EMAIL });
});

test("compares the email case-insensitively and accepts a string audience", async () => {
  const token = await sign(key, goodClaims({ email: "Aaditya.D.Desai@gmail.com", aud: "aud-123" }));
  assert.equal((await verifyAccess(await req("/", { token }), ENV, { fetchFn: fetchMock(), now: NOW })).ok, true);
});

test("caches the certs between requests", async () => {
  const fetchFn = fetchMock();
  await verifyAccess(await req("/"), ENV, { fetchFn, now: NOW });
  await verifyAccess(await req("/"), ENV, { fetchFn, now: NOW + 1000 });
  assert.equal(calls.filter((c) => c.url.endsWith("/certs")).length, 1);
});

test("rejects bad tokens", async () => {
  const check = async (token, reason) => {
    const r = await verifyAccess(await req("/", { token }), ENV, { fetchFn: fetchMock(), now: NOW });
    assert.equal(r.ok, false, reason);
    assert.match(r.reason, new RegExp(reason));
  };
  await check(await sign(key, goodClaims({ aud: ["someone-else"] })), "audience");
  await check(await sign(key, goodClaims({ exp: NOW / 1000 - 1 })), "expired");
  await check(await sign(key, goodClaims({ exp: undefined })), "expired");
  await check(await sign(key, goodClaims({ nbf: NOW / 1000 + 60 })), "not yet");
  await check(await sign(key, goodClaims({ iss: "https://evil.cloudflareaccess.com" })), "issuer");
  await check(await sign(key, goodClaims({ email: "someone@else.com" })), "user");
  await check(await sign(key, goodClaims({ email: undefined })), "user");
  await check(await sign(other, goodClaims()), "signature");
  await check(await sign(key, goodClaims(), { alg: "HS256" }), "alg");
  await check(await sign(key, goodClaims(), { alg: "none" }), "alg");
  await check(await sign({ ...key, kid: "unknown" }, goodClaims()), "unknown key");
  await check("not.a.jwt", "malformed");
  await check("a.b", "malformed");
  const tampered = (await sign(key, goodClaims())).split(".");
  tampered[1] = b64u(Buffer.from(JSON.stringify(goodClaims({ email: "x@y.z" }))));
  await check(tampered.join("."), "signature");
});

test("rejects requests without a token or with the placeholders left in", async () => {
  const bare = new Request("https://me.aadityad.dev/");
  assert.equal((await verifyAccess(bare, ENV, { fetchFn: fetchMock(), now: NOW })).ok, false);
  const r = await verifyAccess(await req("/"), { ...ENV, ACCESS_AUD: "" }, { fetchFn: fetchMock(), now: NOW });
  assert.equal(r.ok, false);
  const placeholder = { ...ENV, ACCESS_TEAM_DOMAIN: "REPLACE-ME.cloudflareaccess.com" };
  assert.equal((await verifyAccess(await req("/"), placeholder, { fetchFn: async () => new Response("no", { status: 404 }), now: NOW })).ok, false);
});

test("every route answers 403 without touching GitHub when the token is bad", async () => {
  const fetchFn = fetchMock();
  for (const [path, method] of [["/", "GET"], ["/api/act", "POST"], ["/api/runs", "GET"], ["/nope", "GET"]]) {
    const request = await req(path, { method, token: "garbage", headers: { origin: "https://me.aadityad.dev", "content-type": "application/json" }, body: method === "POST" ? "{}" : undefined });
    const res = await handle(request, ENV, { fetchFn, now: NOW });
    assert.equal(res.status, 403, path);
  }
  assert.equal(githubCalls().length, 0);
});

// -- GET / ---------------------------------------------------------------------------

test("serves the dashboard from the data repo with strict headers", async () => {
  const res = await run(await req("/"), { "https://api.github.com/repos/": new Response("<h1>hi</h1>") });
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "<h1>hi</h1>");
  const h = res.headers;
  assert.equal(h.get("cache-control"), "no-store");
  assert.equal(h.get("x-content-type-options"), "nosniff");
  assert.equal(h.get("referrer-policy"), "no-referrer");
  const csp = h.get("content-security-policy");
  assert.match(csp, /connect-src 'self'/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /script-src 'self' 'unsafe-inline' https:\/\/cdnjs\.cloudflare\.com/);
  const [g] = githubCalls();
  assert.equal(g.url, "https://api.github.com/repos/aadityad12/oss-scout-data/contents/dashboard.html?ref=claude%2Fscout-data");
  assert.equal(g.init.headers.accept, "application/vnd.github.raw");
  assert.equal(g.init.headers.authorization, "Bearer ghp_test");
});

test("says so when the dashboard is not there yet", async () => {
  const res = await run(await req("/"), { "https://api.github.com/": new Response("nope", { status: 404 }) });
  assert.equal(res.status, 502);
});

test("unknown routes and methods", async () => {
  assert.equal((await run(await req("/secret"))).status, 404);
  assert.equal((await run(await req("/api/act"))).status, 404); // GET on a POST route
  assert.equal((await run(await req("/", { method: "POST", body: "x" }))).status, 404);
});

// -- POST /api/act ----------------------------------------------------------------------

const DISPATCH = { "https://api.github.com/": new Response(null, { status: 204 }) };

test("dispatches the act workflow with the right payload", async () => {
  const res = await run(await postAct({ key: "o/r#7", action: "submit", title: "New title", body: "Body with ünïcode ✓" }), DISPATCH);
  assert.equal(res.status, 200);
  const out = await res.json();
  assert.equal(out.ok, true);
  assert.ok(Math.abs(Date.parse(out.dispatched_at) - Date.now()) < 60000);
  const [g] = githubCalls();
  assert.equal(g.url, "https://api.github.com/repos/aadityad12/oss-scout-data/actions/workflows/act.yml/dispatches");
  assert.equal(g.init.method, "POST");
  assert.equal(g.init.headers.authorization, "Bearer ghp_test");
  const sent = JSON.parse(g.init.body);
  assert.deepEqual(sent, {
    ref: "main",
    inputs: { key: "o/r#7", action: "submit", title: "New title", body_b64: toBase64("Body with ünïcode ✓"), dry_run: "false" },
  });
  assert.equal(Buffer.from(sent.inputs.body_b64, "base64").toString(), "Body with ünïcode ✓");
});

test("title and body are optional", async () => {
  await run(await postAct({ key: "o/r#7", action: "skip" }), DISPATCH);
  assert.deepEqual(JSON.parse(githubCalls()[0].init.body).inputs, { key: "o/r#7", action: "skip", title: "", body_b64: "", dry_run: "false" });
});

test("reports a GitHub failure without leaking details", async () => {
  const res = await run(await postAct({ key: "o/r#7", action: "later" }), { "https://api.github.com/": new Response("secret detail", { status: 422 }) });
  assert.equal(res.status, 502);
  const out = await res.json();
  assert.equal(out.ok, false);
  assert.doesNotMatch(JSON.stringify(out), /secret detail/);
});

test("rejects cross-origin and origin-less posts (CSRF)", async () => {
  for (const origin of ["https://evil.example", "https://me.aadityad.dev.evil.example", "http://me.aadityad.dev", "null"]) {
    const res = await run(await postAct({ key: "o/r#7", action: "submit" }, { origin }), DISPATCH);
    assert.equal(res.status, 403, origin);
  }
  const noOrigin = await req("/api/act", { method: "POST", body: JSON.stringify({ key: "o/r#7", action: "submit" }), headers: { "content-type": "application/json" } });
  assert.equal((await run(noOrigin, DISPATCH)).status, 403);
  assert.equal(githubCalls().length, 0);
});

test("requires a JSON content type", async () => {
  const form = await req("/api/act", { method: "POST", body: "key=o/r%237&action=submit", headers: { "content-type": "application/x-www-form-urlencoded", origin: "https://me.aadityad.dev" } });
  assert.equal((await run(form, DISPATCH)).status, 415);
  const plain = await req("/api/act", { method: "POST", body: "{}", headers: { "content-type": "text/plain", origin: "https://me.aadityad.dev" } });
  assert.equal((await run(plain, DISPATCH)).status, 415);
  assert.equal(githubCalls().length, 0);
});

test("validates the input", async () => {
  const bad = [
    "not json",
    "[]",
    "null",
    { action: "submit" },
    { key: "o/r#7" },
    { key: "o/r#7", action: "merge" },
    { key: "o/r#7", action: ["submit"] },
    { key: "o/r", action: "submit" },
    { key: "o/r#x", action: "submit" },
    { key: "../../x#1", action: "submit" },
    { key: "o/r#7\nfoo", action: "submit" },
    { key: "o/r#7; rm -rf /", action: "submit" },
    { key: 7, action: "submit" },
    { key: "o/r#7", action: "submit", title: "t".repeat(257) },
    { key: "o/r#7", action: "submit", title: 5 },
    { key: "o/r#7", action: "submit", body: "b".repeat(60001) },
    { key: "o/r#7", action: "submit", body: "b".repeat(50000) }, // fits the cap but not GitHub's dispatch limit
    { key: "o/r#7", action: "submit", body: { x: 1 } },
  ];
  for (const payload of bad) {
    const res = await run(await postAct(payload), DISPATCH);
    assert.equal(res.status, 400, JSON.stringify(payload).slice(0, 60));
  }
  assert.equal(githubCalls().length, 0);
});

test("accepts every action and the size limits", async () => {
  for (const action of ACTIONS) assert.equal(parseAct({ key: "a-b/c.d_e#123", action }).action, action);
  assert.ok(!parseAct({ key: "o/r#7", action: "submit", title: "t".repeat(256), body: "b".repeat(40000) }).error);
  assert.deepEqual([...ACTIONS].sort(), ["approve", "followup", "later", "post", "skip", "submit"]);
});

// -- GET /api/runs ----------------------------------------------------------------------

test("lists the latest runs of the act workflow", async () => {
  const workflow_runs = Array.from({ length: 7 }, (_, i) => ({
    status: "completed", conclusion: "success", html_url: `https://github.com/x/y/actions/runs/${i}`,
    created_at: "2026-10-02T15:00:00Z", display_title: `act: submit o/r#${i}`, id: i, secret: "x",
  }));
  const res = await run(await req("/api/runs"), { "https://api.github.com/": Response.json({ workflow_runs }) });
  const out = await res.json();
  assert.equal(out.runs.length, 5);
  assert.deepEqual(Object.keys(out.runs[0]).sort(), ["conclusion", "created_at", "display_title", "html_url", "status"]);
  assert.equal(githubCalls()[0].url, "https://api.github.com/repos/aadityad12/oss-scout-data/actions/workflows/act.yml/runs?per_page=5");
});

// -- the daily email --------------------------------------------------------------------

const DIGEST = {
  date: "2026-10-02",
  ready: [{ key: "o/r#1", title: "Fix <b>crash</b> & more", kind: "pr" }],
  waiting_on_you: [{ key: "o/r#2", pr_url: "https://github.com/o/r/pull/2", overdue: true }, { key: "x/y#3", overdue: false }],
  new_briefings: [{ key: "o/r#4", title: "Something \"new\"", kind: "pr" }],
  token_age_days: 85, token_warning: true, send: true,
};

test("builds a short escaped email", () => {
  const m = buildEmail(DIGEST, ENV);
  assert.equal(m.subject, "1 ready · 2 waiting on you (overdue) · 1 new · token needs rotating");
  assert.match(m.html, /Fix &lt;b&gt;crash&lt;\/b&gt; &amp; more/);
  assert.doesNotMatch(m.html, /<b>crash/);
  assert.match(m.html, /Something &quot;new&quot;/);
  assert.match(m.html, /overdue/);
  assert.match(m.html, /85 days old/);
  assert.match(m.html, /href="https:\/\/me\.aadityad\.dev"/);
  assert.match(m.text, /https:\/\/me\.aadityad\.dev/);
  assert.equal(buildEmail({ ...DIGEST, ready: [DIGEST.ready[0], DIGEST.ready[0]], waiting_on_you: [DIGEST.waiting_on_you[1]], new_briefings: [], token_warning: false }, ENV).subject, "2 ready · 1 waiting on you");
});

test("sends when send is true and the date is today", async () => {
  const fetchFn = fetchMock({
    "https://api.github.com/": Response.json(DIGEST),
    "https://api.resend.com/": Response.json({ id: "1" }),
  });
  const out = await sendDigest(ENV, { fetchFn, now: NOW });
  assert.equal(out.sent, true);
  const resend = calls.find((c) => c.url === "https://api.resend.com/emails");
  assert.equal(resend.init.headers.authorization, "Bearer re_test");
  const sent = JSON.parse(resend.init.body);
  assert.equal(sent.from, "OSS Scout <scout@aadityad.dev>");
  assert.deepEqual(sent.to, [ENV.OWNER_EMAIL]);
  assert.match(sent.subject, /1 ready/);
  assert.equal(calls[0].url, "https://api.github.com/repos/aadityad12/oss-scout-data/contents/digest.json?ref=claude%2Fscout-data");
});

test("MAIL_FROM overrides the sender", async () => {
  const fetchFn = fetchMock({ "https://api.github.com/": Response.json(DIGEST), "https://api.resend.com/": Response.json({}) });
  await sendDigest({ ...ENV, MAIL_FROM: "Me <me@example.com>" }, { fetchFn, now: NOW });
  assert.equal(JSON.parse(calls.find((c) => c.url.includes("resend")).init.body).from, "Me <me@example.com>");
});

test("does not send when send is false, the date is stale, or the digest is missing", async () => {
  const cases = [
    [{ ...DIGEST, send: false }, NOW],
    [DIGEST, Date.parse("2026-10-03T15:00:00Z")],
    [{ ...DIGEST, date: undefined }, NOW],
  ];
  for (const [digest, now] of cases) {
    calls = [];
    const out = await sendDigest(ENV, { fetchFn: fetchMock({ "https://api.github.com/": Response.json(digest) }), now });
    assert.equal(out.sent, false);
    assert.equal(calls.some((c) => c.url.includes("resend")), false);
  }
  const missing = await sendDigest(ENV, { fetchFn: fetchMock({ "https://api.github.com/": new Response("", { status: 404 }) }), now: NOW });
  assert.equal(missing.sent, false);
});

test("a failed send is an error, so the cron run shows red", async () => {
  const fetchFn = fetchMock({ "https://api.github.com/": Response.json(DIGEST), "https://api.resend.com/": new Response("no", { status: 403 }) });
  await assert.rejects(sendDigest(ENV, { fetchFn, now: NOW }), /Resend said 403/);
});
