import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import {
  handle, verifyAccess, resetCertCache, parseAct, buildEmail, buildWeeklyEmail, buildAlertEmail, sendDigest, sendWeekly, sendAlerts, dispatchTrack,
  route, inQuietHours, toBase64, ACTIONS,
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
    { key: "o/r#7", action: "summary" }, // the impact line is required
    { key: "o/r#7", action: "summary", body: 5 },
    { key: "o/r#7", action: "summary", body: "line one\nline two" },
    { key: "o/r#7", action: "summary", body: "line one\r\nline two" },
    { key: "o/r#7", action: "summary", body: "s".repeat(201) },
    { key: "o/r", action: "feature" },
    { key: "o/r#x", action: "pair" },
    { key: "o/r#7", action: "Prepare" },
  ];
  for (const payload of bad) {
    const res = await run(await postAct(payload), DISPATCH);
    assert.equal(res.status, 400, JSON.stringify(payload).slice(0, 60));
  }
  assert.equal(githubCalls().length, 0);
});

test("accepts every action and the size limits", async () => {
  for (const action of ACTIONS) assert.equal(parseAct({ key: "a-b/c.d_e#123", action, body: "" }).action, action);
  assert.ok(!parseAct({ key: "o/r#7", action: "submit", title: "t".repeat(256), body: "b".repeat(40000) }).error);
  assert.deepEqual([...ACTIONS].sort(), ["approve", "feature", "followup", "later", "pair", "post", "prepare", "refresh", "skip", "submit", "summary", "unfeature", "unpair"]);
});

test("the new actions send no text, except summary", () => {
  const blank = { title: "", body_b64: "", dry_run: "false" };
  for (const action of ["prepare", "pair", "unpair", "feature", "unfeature", "refresh"]) {
    // title and body are ignored, even when they would be invalid for another action
    assert.deepEqual(parseAct({ key: "o/r#7", action, title: "t", body: "b" }).inputs, { key: "o/r#7", action, ...blank });
    assert.deepEqual(parseAct({ key: "o/r#7", action, title: 5, body: { x: 1 } }).inputs, { key: "o/r#7", action, ...blank });
    assert.deepEqual(parseAct({ key: "o/r#7", action }).inputs, { key: "o/r#7", action, ...blank });
  }
  const line = "Fixes a crash for 10M installs";
  assert.deepEqual(parseAct({ key: "o/r#7", action: "summary", body: line, title: "ignored" }).inputs, { key: "o/r#7", action: "summary", title: "", body_b64: toBase64(line), dry_run: "false" });
  assert.equal(parseAct({ key: "o/r#7", action: "summary", body: "s".repeat(200) }).error, undefined);
  assert.equal(parseAct({ key: "o/r#7", action: "summary", body: "ünï ✓" }).inputs.body_b64, toBase64("ünï ✓"));
  // an empty summary is allowed and means remove
  assert.deepEqual(parseAct({ key: "o/r#7", action: "summary", body: "" }).inputs, { key: "o/r#7", action: "summary", ...blank });
});

test("refresh is dispatched through the act workflow with no text", async () => {
  const res = await run(await postAct({ key: "o/r#7", action: "refresh", title: "ignored", body: "ignored" }), DISPATCH);
  assert.equal(res.status, 200);
  assert.deepEqual(JSON.parse(githubCalls()[0].init.body).inputs, { key: "o/r#7", action: "refresh", title: "", body_b64: "", dry_run: "false" });
  assert.equal((await run(await postAct({ key: "o/r#7", action: "refreshh" }), DISPATCH)).status, 400);
});

test("a new action is dispatched through the act workflow", async () => {
  const res = await run(await postAct({ key: "o/r#7", action: "summary", body: "Faster startup" }), DISPATCH);
  assert.equal(res.status, 200);
  assert.deepEqual(JSON.parse(githubCalls()[0].init.body).inputs, { key: "o/r#7", action: "summary", title: "", body_b64: toBase64("Faster startup"), dry_run: "false" });
  calls = [];
  assert.equal((await run(await postAct({ key: "o/r#7", action: "summary", body: "a\nb" }), DISPATCH)).status, 400);
  assert.equal(githubCalls().length, 0);
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

const LINES = {
  problem: "A script that converts robot configs into FusionCore configs has no tests, and it crashes on empty files.",
  sending: "A pull request: 1 new test file and a small fix (3 files, ~120 lines)",
  your_part: "Read it and tap Submit PR, about 2 minutes",
};
const STUCK = {
  key: "manankharwar/fusioncore#161", slug: "manankharwar__fusioncore__161", title: "Add <tests> for the converter", kind: "pr",
  plain: "Couldn't send: fusioncore changed ci.yml on Oct 5, after this draft was written.",
  why: "The project edited ci.yml, so the saved change no longer fits.", fix_action: "refresh", refresh_by: "2026-10-12", refreshing: false,
  ...LINES, your_part: "Tap Refresh & send by Oct 12",
};

const DIGEST = {
  date: "2026-10-02",
  ready: [{ key: "o/r#1", title: "Fix <b>crash</b> & more", kind: "pr", slug: "o__r__1", ...LINES }],
  waiting_on_you: [
    { key: "o/r#2", pr_url: "https://github.com/o/r/pull/2", overdue: true, slug: "o__r__2",
      problem: "A maintainer replied on your pull request: Fix the crash", sending: "A reply to the maintainer", your_part: "Read the draft and tap Push fix & reply, about 2 minutes" },
    { key: "x/y#3", overdue: false },
  ],
  weekly: [],
  token_age_days: 85, token_warning: true, send: true,
};

test("builds an escaped email whose items lead with the three plain lines and one button", () => {
  const m = buildEmail(DIGEST, ENV);
  assert.equal(m.subject, "1 PR ready to send · Maintainers replied on 2 PRs · GitHub key needs replacing");
  assert.match(m.html, /Fix &lt;b&gt;crash&lt;\/b&gt; &amp; more/);
  assert.doesNotMatch(m.html, /<b>crash/);
  for (const label of ["What's broken", "You'd send", "Your part"]) assert.match(m.html, new RegExp(`>${label.replace("'", "(?:'|&#39;)")}<`));
  assert.match(m.html, /crashes on empty files/);
  assert.match(m.html, /1 new test file and a small fix/);
  assert.match(m.html, /Read it and tap Submit PR, about 2 minutes/);
  assert.match(m.html, /85 days old/);
  assert.match(m.html, /Waiting more than 2 days/);
  assert.match(m.html, /href="https:\/\/me\.aadityad\.dev"/); // the dashboard button at the end
  // the three lines come after the title and before the item's button
  const card = m.html.slice(m.html.indexOf("Fix &lt;b&gt;"));
  assert.ok(card.indexOf("What's broken") < card.indexOf("You'd send") && card.indexOf("Your part") < card.indexOf("Review and send"));
});

test("the sections come in order: couldn't send, ready to send, a maintainer replied; each only when it has items", () => {
  const m = buildEmail({ ...DIGEST, stuck: [STUCK] }, ENV);
  const at = (s) => m.html.indexOf(s);
  assert.ok(at(">Couldn&#39;t send</h2>") > 0 && at(">Couldn&#39;t send</h2>") < at(">Ready to send</h2>") && at(">Ready to send</h2>") < at(">A maintainer replied</h2>"));
  const onlyReady = buildEmail({ ...DIGEST, stuck: [], waiting_on_you: [], token_warning: false }, ENV);
  assert.match(onlyReady.html, />Ready to send</);
  assert.doesNotMatch(onlyReady.html, /Couldn(?:'|&#39;)t send|A maintainer replied/);
  const t = m.text;
  assert.ok(t.indexOf("COULDN'T SEND") < t.indexOf("READY TO SEND") && t.indexOf("READY TO SEND") < t.indexOf("A MAINTAINER REPLIED"));
});

test("each item has exactly one button, deep-linking to it", () => {
  const m = buildEmail({ ...DIGEST, stuck: [STUCK] }, ENV);
  for (const slug of ["manankharwar__fusioncore__161", "o__r__1", "o__r__2"]) {
    assert.equal(m.html.split(`href="https://me.aadityad.dev/#${slug}"`).length - 1, 1, slug);
  }
  assert.equal(m.html.match(/href="https:\/\/me\.aadityad\.dev\/#/g).length, 3); // x/y#3 has no slug, so no button
  assert.match(m.html, /x\/y#3/);
  const hostile = { ...DIGEST, ready: [{ key: "o/r#1", title: "t", slug: 'a"b c/d' }], waiting_on_you: [] };
  assert.match(buildEmail(hostile, ENV).html, /href="https:\/\/me\.aadityad\.dev\/#a%22b%20c%2Fd"/);
  assert.match(buildEmail(DIGEST, { ...ENV, DASHBOARD_URL: "https://me.aadityad.dev/" }).html, /href="https:\/\/me\.aadityad\.dev\/#o__r__1"/);
});

test("the plain text carries the same content as the html", () => {
  const m = buildEmail({ ...DIGEST, stuck: [STUCK] }, ENV);
  for (const needle of [
    "Fix <b>crash</b> & more", "o/r#1", `What's broken: ${LINES.problem}`, `You'd send: ${LINES.sending}`, `Your part: ${LINES.your_part}`,
    "https://me.aadityad.dev/#o__r__1", "Waiting more than 2 days", "Why: The project edited ci.yml", "Refresh by Oct 12",
    "https://me.aadityad.dev/#manankharwar__fusioncore__161", "85 days old",
  ]) assert.ok(m.text.includes(needle), needle);
  assert.doesNotMatch(m.text, /<div|<a |&amp;|&#39;/); // text is not html
});

test("the subject is plain and says what to do", () => {
  const w = (key) => ({ key, overdue: false });
  const one = { date: "2026-10-02", ready: [DIGEST.ready[0]], waiting_on_you: [], send: true };
  assert.equal(buildEmail(one, ENV).subject, "1 PR ready to send");
  assert.equal(buildEmail({ ...one, ready: [one.ready[0], one.ready[0]] }, ENV).subject, "2 PRs ready to send");
  assert.equal(buildEmail({ ...one, ready: [{ ...one.ready[0], kind: "repro" }] }, ENV).subject, "1 item ready to send");
  assert.equal(buildEmail({ ...one, ready: [], waiting_on_you: [w("a/b#1")] }, ENV).subject, "A maintainer replied");
  assert.equal(buildEmail({ ...one, ready: [], waiting_on_you: [w("a/b#1"), w("c/d#2")] }, ENV).subject, "Maintainers replied on 2 PRs");
  assert.equal(buildEmail({ ...one, ready: [], token_warning: true }, ENV).subject, "GitHub key needs replacing");
  assert.equal(buildEmail({ ...one, ready: [] }, ENV).subject, "Nothing needs you today");
  assert.equal(buildEmail({ ...one, stuck: [STUCK] }, ENV).subject, "Couldn't send 1 PR: refresh by Oct 12 · 1 PR ready to send");
  const two = { ...one, ready: [], stuck: [STUCK, { ...STUCK, key: "a/b#2", slug: "a__b__2", refresh_by: "2026-10-09" }] };
  assert.equal(buildEmail(two, ENV).subject, "Couldn't send 2 PRs: refresh by Oct 9");
  assert.equal(buildEmail({ ...one, ready: [], stuck: [{ ...STUCK, refresh_by: "garbage" }] }, ENV).subject, "Couldn't send 1 PR");
});

test("a failed send leads the email: the plain failure, why, and the refresh-by date", () => {
  const m = buildEmail({ ...DIGEST, stuck: [STUCK] }, ENV);
  assert.match(m.html, /Couldn(?:'|&#39;)t send: fusioncore changed ci\.yml on Oct 5, after this draft was written\./);
  assert.match(m.html, /Why: The project edited ci\.yml, so the saved change no longer fits\./);
  assert.match(m.html, /Refresh by Oct 12/);
  assert.match(m.html, /Tap Refresh &amp; send by Oct 12/);
  assert.match(m.html, /Open and refresh/);
  assert.match(m.html, /Add &lt;tests&gt; for the converter/); // escaped
  assert.match(m.text, /COULDN'T SEND\n\nAdd <tests> for the converter\nmanankharwar\/fusioncore#161\nCouldn't send: fusioncore changed ci\.yml on Oct 5, after this draft was written\.\nWhy: The project edited ci\.yml, so the saved change no longer fits\.\nRefresh by Oct 12\n/);
});

test("a refresh already running is said so, and nothing stuck means no stuck section", () => {
  const running = buildEmail({ ...DIGEST, stuck: [{ ...STUCK, refreshing: true }] }, ENV);
  assert.match(running.html, /Refreshing now\./);
  assert.doesNotMatch(running.html, /Refresh by/);
  assert.doesNotMatch(running.html, /Open and refresh/);
  for (const d of [DIGEST, { ...DIGEST, stuck: [] }]) {
    const none = buildEmail(d, ENV);
    assert.doesNotMatch(none.html, /Couldn(?:'|&#39;)t send/);
    assert.doesNotMatch(none.text, /Couldn't send/);
    assert.doesNotMatch(none.subject, /send:|Couldn/);
  }
});

test("laptop briefings and the pairing queue are not in the daily email", () => {
  const withQueue = {
    ...DIGEST,
    new_briefings: [{ key: "o/r#4", title: "Brand new briefing", kind: "pr", slug: "o__r__4" }],
    pairing: [{ key: "o/r#5", title: "Pairing item", slug: "o__r__5" }],
    weekly: [{ key: "o/r#6", title: "Weekly item", slug: "o__r__6", ...LINES }],
  };
  const m = buildEmail(withQueue, ENV);
  for (const gone of ["Brand new briefing", "Pairing", "Weekly item", "o__r__4", "o__r__5", "o__r__6"]) {
    assert.ok(!m.html.includes(gone) && !m.text.includes(gone), gone);
  }
  assert.doesNotMatch(m.subject, /new|pairing/i);
});

const WEEKLY = {
  date: "2026-10-10",
  weekly: [
    { key: "duckdb/duckdb#26144", slug: "duckdb__duckdb__26144", title: "PREPARE with nextval() <crashes>", kind: "pr",
      problem: "Preparing a query that uses a sequence breaks the whole database session until it is restarted.",
      sending: "Nothing prepared yet: a laptop session where we write it together",
      your_part: "Laptop: run /contribute, about 2 hours including the build" },
  ],
};

test("the Saturday email lists laptop briefings with the three lines and one button each", () => {
  const m = buildWeeklyEmail(WEEKLY, ENV);
  assert.equal(m.subject, "Worth doing on your laptop this week");
  assert.match(m.html, /PREPARE with nextval\(\) &lt;crashes&gt;/);
  assert.match(m.html, /breaks the whole database session/);
  assert.match(m.html, /Nothing prepared yet: a laptop session where we write it together/);
  assert.match(m.html, /Laptop: run \/contribute, about 2 hours including the build/);
  assert.equal(m.html.split('href="https://me.aadityad.dev/#duckdb__duckdb__26144"').length - 1, 1);
  assert.match(m.html, /Open the briefing/);
  for (const needle of ["PREPARE with nextval() <crashes>", "What's broken: Preparing a query", "You'd send: Nothing prepared yet", "Your part: Laptop: run /contribute", "https://me.aadityad.dev/#duckdb__duckdb__26144"]) {
    assert.ok(m.text.includes(needle), needle);
  }
});

test("the Saturday email shows at most five and says how many more", () => {
  const many = { ...WEEKLY, weekly: Array.from({ length: 7 }, (_, n) => ({ ...WEEKLY.weekly[0], key: `o/r#${n}`, slug: `o__r__${n}`, title: `Item ${n}` })) };
  const m = buildWeeklyEmail(many, ENV);
  assert.equal(m.html.match(/Open the briefing/g).length, 5);
  assert.match(m.html, /And 2 more on the dashboard/);
  assert.match(m.text, /And 2 more on the dashboard/);
});

test("the Saturday email is sent only when there is something and the digest is from today", async () => {
  const saturday = Date.parse("2026-10-10T15:10:00Z");
  const sent = await sendWeekly(ENV, { fetchFn: fetchMock({ "https://api.github.com/": Response.json(WEEKLY), "https://api.resend.com/": Response.json({}) }), now: saturday });
  assert.deepEqual(sent, { sent: true, subject: "Worth doing on your laptop this week" });
  for (const [digest, now] of [[{ ...WEEKLY, weekly: [] }, saturday], [{ date: "2026-10-10" }, saturday], [WEEKLY, Date.parse("2026-10-11T15:10:00Z")]]) {
    calls = [];
    const out = await sendWeekly(ENV, { fetchFn: fetchMock({ "https://api.github.com/": Response.json(digest) }), now });
    assert.equal(out.sent, false);
    assert.equal(calls.some((c) => c.url.includes("resend")), false);
  }
  assert.equal((await sendWeekly(ENV, { fetchFn: fetchMock({ "https://api.github.com/": new Response("", { status: 404 }) }), now: saturday })).sent, false);
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
  assert.match(sent.subject, /1 PR ready to send/);
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

// -- crons ----------------------------------------------------------------------------

test("routes each cron to its job, and unknown ones to nothing", () => {
  assert.equal(route("0 15 * * *"), "digest");
  assert.equal(route("0 */3 * * *"), "track");
  assert.equal(route("30 */3 * * *"), "alerts");
  assert.equal(route("10 15 * * SAT"), "weekly");
  for (const cron of ["", undefined, "0 0 * * *", "30 15 * * *", "10 15 * * *", "10 15 * * 6"]) assert.equal(route(cron), null);
});

test("wrangler.toml lists exactly the crons the Worker routes", async () => {
  const { readFileSync } = await import("node:fs");
  const toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
  const crons = [...toml.match(/^crons = \[(.*)\]/m)[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(crons, ["0 15 * * *", "0 */3 * * *", "30 */3 * * *", "10 15 * * SAT"]);
  for (const cron of crons) assert.notEqual(route(cron), null);
});

test("the scheduled handler runs the routed job and ignores unknown crons", async () => {
  const { default: worker } = await import("../src/index.js");
  const realFetch = globalThis.fetch, realLog = console.log;
  const logs = [];
  console.log = (m) => logs.push(m);
  globalThis.fetch = fetchMock({ "https://api.github.com/": new Response(null, { status: 204 }) });
  try {
    const waits = [];
    worker.scheduled({ cron: "0 */3 * * *" }, ENV, { waitUntil: (p) => waits.push(p) });
    assert.equal(waits.length, 1);
    assert.deepEqual(await waits[0], { dispatched: true });
    worker.scheduled({ cron: "5 4 * * *" }, ENV, { waitUntil: (p) => waits.push(p) });
    assert.equal(waits.length, 1);
    assert.match(logs[0], /unknown cron "5 4 \* \* \*"/);
  } finally {
    globalThis.fetch = realFetch;
    console.log = realLog;
  }
});

test("dispatches track.yml on main with no inputs", async () => {
  const out = await dispatchTrack(ENV, { fetchFn: fetchMock(DISPATCH) });
  assert.deepEqual(out, { dispatched: true });
  const [g] = githubCalls();
  assert.equal(g.url, "https://api.github.com/repos/aadityad12/oss-scout-data/actions/workflows/track.yml/dispatches");
  assert.equal(g.init.method, "POST");
  assert.equal(g.init.headers.authorization, "Bearer ghp_test");
  assert.deepEqual(JSON.parse(g.init.body), { ref: "main" });
});

test("a track.yml dispatch that is not a 204 is an error, so the cron run shows red", async () => {
  await assert.rejects(dispatchTrack(ENV, { fetchFn: fetchMock({ "https://api.github.com/": new Response("secret", { status: 404 }) }) }), /track\.yml \(404\)/);
  await assert.rejects(dispatchTrack(ENV, { fetchFn: fetchMock({ "https://api.github.com/": new Response("{}", { status: 200 }) }) }), /200/);
});

// -- review alerts --------------------------------------------------------------------

const ALERT = { at: "2026-10-02T14:20:00Z", key: "pydantic/pydantic#9001", slug: "pydantic__pydantic__9001", pr_url: "https://github.com/pydantic/pydantic/pull/9001", author: "samuelcolvin", excerpt: "please add a test" };
const alertsFile = (alerts, generated_at) => ({ generated_at, alerts });
const ALERTS_NOW = Date.parse("2026-10-02T14:30:00Z"); // 07:30 PDT
const alertFetch = (file) => fetchMock({ "https://api.github.com/": Response.json(file), "https://api.resend.com/": Response.json({ id: "1" }) });
const resendCalls = () => calls.filter((c) => c.url.includes("resend"));

test("sends an alert email when the check is fresh, in the window and outside quiet hours", async () => {
  const out = await sendAlerts(ENV, { fetchFn: alertFetch(alertsFile([ALERT], "2026-10-02T14:00:00Z")), now: ALERTS_NOW });
  assert.equal(out.sent, true);
  assert.equal(out.subject, "Reviewer replied on pydantic/pydantic#9001");
  assert.equal(calls[0].url, "https://api.github.com/repos/aadityad12/oss-scout-data/contents/alerts.json?ref=claude%2Fscout-data");
  const [mail] = resendCalls();
  assert.equal(mail.init.headers.authorization, "Bearer re_test");
  const sent = JSON.parse(mail.init.body);
  assert.equal(sent.from, "OSS Scout <scout@aadityad.dev>");
  assert.deepEqual(sent.to, [ENV.OWNER_EMAIL]);
  assert.match(sent.html, /href="https:\/\/me\.aadityad\.dev\/#pydantic__pydantic__9001"/);
});

test("the freshness window is 3 hours", async () => {
  const at = (generated_at) => sendAlerts(ENV, { fetchFn: alertFetch(alertsFile([ALERT], generated_at)), now: ALERTS_NOW });
  assert.equal((await at("2026-10-02T11:30:00Z")).sent, true); // exactly 3 hours
  const stale = await at("2026-10-02T11:29:59Z");
  assert.equal(stale.sent, false);
  assert.match(stale.reason, /too old/);
  assert.equal((await at(undefined)).sent, false);
  assert.equal((await at("not a date")).sent, false);
});

test("does not send when there are no alerts, or alerts.json is missing", async () => {
  for (const file of [alertsFile([], "2026-10-02T14:00:00Z"), { generated_at: "2026-10-02T14:00:00Z" }, { generated_at: "2026-10-02T14:00:00Z", alerts: null }]) {
    calls = [];
    const out = await sendAlerts(ENV, { fetchFn: alertFetch(file), now: ALERTS_NOW });
    assert.equal(out.sent, false);
    assert.equal(resendCalls().length, 0);
  }
  const missing = await sendAlerts(ENV, { fetchFn: fetchMock({ "https://api.github.com/": new Response("", { status: 404 }) }), now: ALERTS_NOW });
  assert.equal(missing.sent, false);
});

test("quiet hours are 23:00 to 07:00 Pacific, in daylight and standard time", async () => {
  const pdt = [ // October: UTC-7
    ["2026-10-02T05:30:00Z", false], // 22:30 the evening before
    ["2026-10-02T06:00:00Z", true], // 23:00
    ["2026-10-02T09:30:00Z", true], // 02:30
    ["2026-10-02T13:59:00Z", true], // 06:59
    ["2026-10-02T14:00:00Z", false], // 07:00
    ["2026-10-02T14:30:00Z", false], // 07:30
  ];
  const pst = [ // December: UTC-8, so the same UTC time is an hour later on the clock
    ["2026-12-02T06:30:00Z", false], // 22:30 the evening before
    ["2026-12-02T07:00:00Z", true], // 23:00
    ["2026-12-02T10:30:00Z", true], // 02:30
    ["2026-12-02T14:59:00Z", true], // 06:59
    ["2026-12-02T15:00:00Z", false], // 07:00
    ["2026-12-02T15:30:00Z", false], // 07:30
  ];
  for (const [iso, quiet] of [...pdt, ...pst]) {
    assert.equal(inQuietHours(Date.parse(iso)), quiet, iso);
    calls = [];
    const now = Date.parse(iso);
    const generated = new Date(now - 30 * 60 * 1000).toISOString();
    const out = await sendAlerts(ENV, { fetchFn: alertFetch(alertsFile([ALERT], generated)), now });
    assert.equal(out.sent, !quiet, iso);
    assert.equal(resendCalls().length, quiet ? 0 : 1, iso);
  }
  // the same UTC clock time lands on different sides of the line in summer and winter
  assert.equal(inQuietHours(Date.parse("2026-10-02T14:30:00Z")), false);
  assert.equal(inQuietHours(Date.parse("2026-12-02T14:30:00Z")), true);
  // midnight and the DST switch days
  assert.equal(inQuietHours(Date.parse("2026-10-02T07:00:00Z")), true); // 00:00 PDT
  assert.equal(inQuietHours(Date.parse("2026-11-01T15:00:00Z")), false); // 07:00 PST, the morning clocks go back
  assert.equal(inQuietHours(Date.parse("2026-11-01T14:30:00Z")), true); // 06:30 PST
});

test("a failed alert send is an error, so the cron run shows red", async () => {
  const fetchFn = fetchMock({ "https://api.github.com/": Response.json(alertsFile([ALERT], "2026-10-02T14:00:00Z")), "https://api.resend.com/": new Response("no", { status: 403 }) });
  await assert.rejects(sendAlerts(ENV, { fetchFn, now: ALERTS_NOW }), /Resend said 403/);
});

test("alert email: subject, short body, links", () => {
  const one = buildAlertEmail([ALERT], ENV);
  assert.equal(one.subject, "Reviewer replied on pydantic/pydantic#9001");
  assert.match(one.html, /<a href="https:\/\/me\.aadityad\.dev\/#pydantic__pydantic__9001"[^>]*>samuelcolvin on pydantic\/pydantic#9001<\/a>/);
  assert.match(one.html, /Open the dashboard/);
  assert.match(one.html, /href="https:\/\/me\.aadityad\.dev"/);
  assert.doesNotMatch(one.html, /please add a test/); // no excerpt
  assert.doesNotMatch(one.text, /please add a test/);
  assert.match(one.text, /samuelcolvin on pydantic\/pydantic#9001: https:\/\/me\.aadityad\.dev\/#pydantic__pydantic__9001/);
  assert.match(one.text, /\nhttps:\/\/me\.aadityad\.dev$/);

  const two = buildAlertEmail([ALERT, { ...ALERT, key: "o/r#2", slug: "o__r__2", author: "bob" }], ENV);
  assert.equal(two.subject, "2 reviewers replied");
  assert.match(two.html, /href="https:\/\/me\.aadityad\.dev\/#o__r__2"/);
  assert.equal(two.html.match(/<li /g).length, 2);
  assert.match(two.text, /bob on o\/r#2/);
});

test("alert email escapes everything and encodes the slug", () => {
  const evil = { ...ALERT, author: '<img src=x onerror=alert(1)>"&', key: "o/r#1<script>", slug: 'a"><script>alert(1)</script>/b' };
  const m = buildAlertEmail([evil], ENV);
  assert.doesNotMatch(m.html, /<img|<script/);
  assert.match(m.html, /&lt;img src=x onerror=alert\(1\)&gt;&quot;&amp;/);
  assert.match(m.html, /href="https:\/\/me\.aadityad\.dev\/#a%22%3E%3Cscript%3Ealert\(1\)%3C%2Fscript%3E%2Fb"/);
  assert.match(m.subject, /o\/r#1<script>/); // the subject is plain text, not HTML
  const noSlug = buildAlertEmail([{ ...ALERT, slug: undefined }], ENV);
  assert.doesNotMatch(noSlug.html, /#undefined/);
});
