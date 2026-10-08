// The private dashboard at me.aadityad.dev.
//
// Serves dashboard.html from the private data repo, turns a button press into a
// run of the data repo's act workflow (which does the GitHub writes, with its own
// token), sends the daily email and the Saturday laptop email, starts the data repo's review check every 3 hours
// and emails when a reviewer has replied. Cloudflare Access sits in front; this code
// checks the Access token again on every request.

export const ACTIONS = ["submit", "post", "followup", "approve", "later", "skip", "prepare", "pair", "unpair", "feature", "unfeature", "summary", "refresh"];
const NO_TEXT_ACTIONS = ["prepare", "pair", "unpair", "feature", "unfeature", "refresh"]; // these ignore title and body
export const MAX_SUMMARY = 200; // the impact line of a PR
export const KEY_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+#[0-9]+$/;
export const MAX_TITLE = 256;
export const MAX_BODY = 60000;
const MAX_REQUEST = 200000;
const MAX_DISPATCH_BODY = 60000; // GitHub caps all workflow inputs at 65,535 characters; the body travels as base64
const GITHUB = "https://api.github.com";
const CERT_TTL_MS = 60 * 60 * 1000;
const ALERT_WINDOW_MS = 3 * 60 * 60 * 1000; // an alerts.json older than one check cycle is stale
const QUIET_TZ = "America/Los_Angeles";
export const CRONS = { "0 15 * * *": "digest", "0 */3 * * *": "track", "30 */3 * * *": "alerts", "10 15 * * SAT": "weekly" };

const enc = new TextEncoder();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// -- Cloudflare Access token ---------------------------------------------------

const b64uBytes = (s) => {
  const padded = s.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(s.length / 4) * 4, "=");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
};
const b64uJson = (s) => JSON.parse(new TextDecoder().decode(b64uBytes(s)));

export function toBase64(text) {
  const bytes = enc.encode(text);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

const certCache = new Map(); // certs url -> { keys, at }
export const resetCertCache = () => certCache.clear();

async function accessKeys(url, fetchFn, now, force = false) {
  const hit = certCache.get(url);
  if (hit && !force && now - hit.at < CERT_TTL_MS) return hit.keys;
  const res = await fetchFn(url);
  if (!res.ok) throw new Error(`certs ${res.status}`);
  const keys = (await res.json()).keys || [];
  certCache.set(url, { keys, at: now });
  return keys;
}

export async function verifyAccess(request, env, { fetchFn = fetch, now = Date.now() } = {}) {
  const fail = (reason) => ({ ok: false, reason });
  const token = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!token) return fail("no token");
  if (!env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD || !env.OWNER_EMAIL) return fail("not configured");
  const parts = token.split(".");
  if (parts.length !== 3) return fail("malformed");
  let header, claims;
  try {
    header = b64uJson(parts[0]);
    claims = b64uJson(parts[1]);
  } catch {
    return fail("malformed");
  }
  if (header.alg !== "RS256" || !header.kid) return fail("bad alg");

  const issuer = `https://${env.ACCESS_TEAM_DOMAIN}`;
  const certs = `${issuer}/cdn-cgi/access/certs`;
  let jwk;
  try {
    jwk = (await accessKeys(certs, fetchFn, now)).find((k) => k.kid === header.kid);
    if (!jwk) jwk = (await accessKeys(certs, fetchFn, now, true)).find((k) => k.kid === header.kid);
  } catch {
    return fail("certs unavailable");
  }
  if (!jwk) return fail("unknown key");
  try {
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const good = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, b64uBytes(parts[2]), enc.encode(`${parts[0]}.${parts[1]}`));
    if (!good) return fail("bad signature");
  } catch {
    return fail("bad signature");
  }

  const seconds = Math.floor(now / 1000);
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.includes(env.ACCESS_AUD)) return fail("wrong audience");
  if (claims.iss !== issuer) return fail("wrong issuer");
  if (typeof claims.exp !== "number" || claims.exp <= seconds) return fail("expired");
  if (typeof claims.nbf === "number" && claims.nbf > seconds) return fail("not yet valid");
  if (String(claims.email || "").toLowerCase() !== env.OWNER_EMAIL.toLowerCase()) return fail("wrong user");
  return { ok: true, email: claims.email };
}

// -- responses -------------------------------------------------------------------

const CSP = [
  "default-src 'none'",
  "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com",
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src https://fonts.gstatic.com",
  "img-src 'self' data:",
  "connect-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "frame-ancestors 'none'",
].join("; ");

const BASE_HEADERS = {
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
};

export const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...BASE_HEADERS, "content-type": "application/json; charset=utf-8" } });
const text = (msg, status) => new Response(msg, { status, headers: { ...BASE_HEADERS, "content-type": "text/plain; charset=utf-8" } });

const gh = (env, path, fetchFn, init = {}) =>
  fetchFn(`${GITHUB}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "user-agent": "oss-scout-dashboard",
      "x-github-api-version": "2022-11-28",
      ...init.headers,
    },
  });

const dataFile = (env, name, fetchFn) =>
  gh(env, `/repos/${env.DATA_REPO}/contents/${name}?ref=${encodeURIComponent(env.DATA_REF)}`, fetchFn, {
    headers: { accept: "application/vnd.github.raw" },
  });

// -- routes ----------------------------------------------------------------------

async function dashboard(env, fetchFn) {
  const res = await dataFile(env, "dashboard.html", fetchFn);
  if (!res.ok) return text(`The dashboard isn't available yet (GitHub said ${res.status}).`, 502);
  return new Response(await res.text(), {
    headers: { ...BASE_HEADERS, "content-type": "text/html; charset=utf-8", "content-security-policy": CSP },
  });
}

export function parseAct(input) {
  if (!input || typeof input !== "object" || Array.isArray(input)) return { error: "expected a JSON object" };
  const { key, action, title, body } = input;
  if (typeof key !== "string" || !KEY_RE.test(key)) return { error: "bad key" };
  if (!ACTIONS.includes(action)) return { error: "bad action" };
  if (NO_TEXT_ACTIONS.includes(action)) return { key, action, inputs: { key, action, title: "", body_b64: "", dry_run: "false" } };
  if (action === "summary") {
    if (typeof body !== "string") return { error: "summary must be text" };
    if (body.length > MAX_SUMMARY) return { error: `summary must be at most ${MAX_SUMMARY} characters` };
    if (/[\r\n\u2028\u2029]/.test(body)) return { error: "summary must be a single line" };
    return { key, action, inputs: { key, action, title: "", body_b64: body ? toBase64(body) : "", dry_run: "false" } };
  }
  if (title != null && (typeof title !== "string" || title.length > MAX_TITLE)) return { error: `title must be at most ${MAX_TITLE} characters` };
  if (body != null && (typeof body !== "string" || body.length > MAX_BODY)) return { error: `text must be at most ${MAX_BODY} characters` };
  const body_b64 = body ? toBase64(body) : "";
  if (body_b64.length > MAX_DISPATCH_BODY) return { error: "text is too long to send; shorten it" };
  return { key, action, inputs: { key, action, title: title || "", body_b64, dry_run: "false" } };
}

async function act(request, env, fetchFn) {
  if (!(request.headers.get("content-type") || "").toLowerCase().startsWith("application/json")) {
    return json({ ok: false, error: "expected application/json" }, 415);
  }
  let expected;
  try {
    expected = new URL(env.DASHBOARD_URL).origin;
  } catch {
    return json({ ok: false, error: "not configured" }, 500);
  }
  if (request.headers.get("origin") !== expected) return json({ ok: false, error: "bad origin" }, 403);
  const raw = await request.text();
  if (raw.length > MAX_REQUEST) return json({ ok: false, error: "request too large" }, 413);
  let input;
  try {
    input = JSON.parse(raw);
  } catch {
    return json({ ok: false, error: "bad JSON" }, 400);
  }
  const parsed = parseAct(input);
  if (parsed.error) return json({ ok: false, error: parsed.error }, 400);

  const dispatched_at = new Date().toISOString();
  const res = await gh(env, `/repos/${env.DATA_REPO}/actions/workflows/act.yml/dispatches`, fetchFn, {
    method: "POST",
    headers: { accept: "application/vnd.github+json", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main", inputs: parsed.inputs }),
  });
  if (res.status !== 204) return json({ ok: false, error: `GitHub refused the request (${res.status})` }, 502);
  return json({ ok: true, dispatched_at });
}

async function runs(env, fetchFn) {
  const res = await gh(env, `/repos/${env.DATA_REPO}/actions/workflows/act.yml/runs?per_page=5`, fetchFn, {
    headers: { accept: "application/vnd.github+json" },
  });
  if (!res.ok) return json({ ok: false, error: `GitHub said ${res.status}` }, 502);
  const data = await res.json();
  return json({
    runs: (data.workflow_runs || []).slice(0, 5).map((r) => ({
      status: r.status, conclusion: r.conclusion, html_url: r.html_url, created_at: r.created_at, display_title: r.display_title,
    })),
  });
}

export async function handle(request, env, { fetchFn = fetch, now = Date.now() } = {}) {
  const who = await verifyAccess(request, env, { fetchFn, now });
  if (!who.ok) return text("Forbidden", 403);
  const { pathname } = new URL(request.url);
  const { method } = request;
  if (pathname === "/" && method === "GET") return dashboard(env, fetchFn);
  if (pathname === "/api/act" && method === "POST") return act(request, env, fetchFn);
  if (pathname === "/api/runs" && method === "GET") return runs(env, fetchFn);
  return text("Not found", 404);
}

// -- emails ------------------------------------------------------------------------

// A link to one item on the dashboard; the page opens the item named by the hash.
const deepLink = (env, slug) => (slug ? `${String(env.DASHBOARD_URL).replace(/\/+$/, "")}/#${encodeURIComponent(slug)}` : env.DASHBOARD_URL);
const linked = (env, slug, inner) => (slug ? `<a href="${esc(deepLink(env, slug))}" style="color:inherit">${inner}</a>` : inner);
const dashLink = (env) => `<p style="margin:24px 0 0"><a href="${esc(env.DASHBOARD_URL)}" style="color:#0000e0">Open the dashboard</a></p>`;
const openButton = (env) =>
  `<p style="margin:24px 0"><a href="${esc(env.DASHBOARD_URL)}" style="display:inline-block;background:#0000f2;color:#fff;text-decoration:none;font-weight:600;font-size:18px;padding:14px 26px;border-radius:10px">Open the dashboard</a></p>`;

// "2026-10-12" as "Oct 12", read as a plain date so the timezone can't move it
const shortDate = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso ?? ""));
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "";
};

// Email-safe inline styles: neutral text, one red for what went wrong, one blue for the buttons.
const INK = "#10121a", MUTED = "#545869", LINE = "#dfe2ec", RED = "#be2a2f", BLUE = "#0000f2";
const WRAP = `font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:${INK};background:#ffffff;max-width:560px;padding:4px`;
const h2 = (title, color = INK) => `<h2 style="font-size:17px;margin:28px 0 12px;color:${color}">${esc(title)}</h2>`;
const noun = (items) => (items.every((i) => (i.kind || "pr") === "pr") ? "PR" : "item");
const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// The three plain lines every item leads with. A line the digest didn't carry is left out.
const LINES = [["problem", "What's broken"], ["sending", "You'd send"], ["your_part", "Your part"]];
const lineRows = (i) =>
  LINES.filter(([k]) => i[k])
    .map(([k, label]) => `<div style="margin:8px 0 0"><div style="font-size:12px;color:${MUTED};text-transform:uppercase;letter-spacing:.04em">${label}</div><div>${esc(i[k])}</div></div>`)
    .join("");
const lineText = (i) => LINES.filter(([k]) => i[k]).map(([k, label]) => `${label}: ${i[k]}`);
const head = (i) => (i.title && i.title !== i.key ? [i.title, i.key] : [i.key]);
const itemButton = (env, i, label) =>
  `<p style="margin:14px 0 0"><a href="${esc(deepLink(env, i.slug))}" style="display:inline-block;background:${BLUE};color:#fff;text-decoration:none;font-weight:600;font-size:16px;padding:12px 22px;border-radius:10px">${esc(label)}</a></p>`;

// One item: bold title, its key, anything specific to the section (`extra`), the three lines, one button.
const itemCard = (env, i, label, extra = "") =>
  `<div style="margin:0 0 22px;padding:0 0 22px;border-bottom:1px solid ${LINE}"><div style="font-size:17px;font-weight:700">${esc(i.title || i.key)}</div>${i.title && i.title !== i.key ? `<div style="font-size:13px;color:${MUTED}">${esc(i.key)}</div>` : ""}${extra}${lineRows(i)}${itemButton(env, i, label)}</div>`;
const note = (text, color = INK) => (text ? `<div style="margin:10px 0 0;color:${color};font-weight:600">${esc(text)}</div>` : "");

const refreshNote = (i) => (i.refreshing ? "Refreshing now." : shortDate(i.refresh_by) ? `Refresh by ${shortDate(i.refresh_by)}` : "");
const stuckCard = (env, i) =>
  itemCard(env, i, i.fix_action === "refresh" && !i.refreshing ? "Open and refresh" : "Open",
    `${note(i.plain, RED)}${i.why ? `<div style="margin:4px 0 0">Why: ${esc(i.why)}</div>` : ""}${refreshNote(i) ? `<div style="margin:4px 0 0;font-weight:600">${esc(refreshNote(i))}</div>` : ""}`);

export function buildEmail(d, env) {
  const stuck = d.stuck || [], ready = d.ready || [], waiting = d.waiting_on_you || [];
  const parts = [];
  if (stuck.length) {
    const by = stuck.map((i) => i.refresh_by).filter((x) => shortDate(x)).sort()[0];
    parts.push(`Couldn't send ${count(stuck.length, noun(stuck))}${by ? `: refresh by ${shortDate(by)}` : ""}`);
  }
  if (ready.length) parts.push(`${count(ready.length, noun(ready))} ready to send`);
  if (waiting.length) parts.push(waiting.length === 1 ? "A maintainer replied" : `Maintainers replied on ${waiting.length} PRs`);
  if (d.token_warning) parts.push("GitHub key needs replacing");
  const subject = parts.join(" · ") || "Nothing needs you today";

  const overdue = (w) => (w.overdue ? "Waiting more than 2 days" : "");
  const html = `<div style="${WRAP}">
<h1 style="font-size:20px;margin:0 0 8px">OSS Scout · ${esc(d.date)}</h1>
${d.token_warning ? `<p style="background:#fcebec;padding:10px 12px;border-radius:8px"><b>Your GitHub key is ${esc(d.token_age_days)} days old.</b> Replace it soon: sending stops when it expires.</p>` : ""}
${stuck.length ? h2("Couldn't send", RED) + stuck.map((i) => stuckCard(env, i)).join("") : ""}
${ready.length ? h2("Ready to send") + ready.map((i) => itemCard(env, i, "Review and send")).join("") : ""}
${waiting.length ? h2("A maintainer replied") + waiting.map((w) => itemCard(env, w, "Read the reply", note(overdue(w), RED))).join("") : ""}
${dashLink(env)}
</div>`;

  const link = (i) => deepLink(env, i.slug);
  const block = (title, items, lines) => (items.length ? ["", title.toUpperCase(), ...items.flatMap((i) => ["", ...lines(i)])] : []);
  const plain = [
    `OSS Scout ${d.date}`,
    d.token_warning ? `Your GitHub key is ${d.token_age_days} days old. Replace it soon: sending stops when it expires.` : "",
    ...block("Couldn't send", stuck, (i) => [...head(i), i.plain, i.why ? `Why: ${i.why}` : "", refreshNote(i), ...lineText(i), link(i)]),
    ...block("Ready to send", ready, (i) => [...head(i), ...lineText(i), link(i)]),
    ...block("A maintainer replied", waiting, (w) => [...head(w), overdue(w), ...lineText(w), link(w)]),
    "",
    env.DASHBOARD_URL,
  ].filter((line, n, all) => line || all[n - 1]).join("\n");
  return { subject, html, text: plain };
}

const WEEKLY_MAX = 5; // longer than this and it stops being a short list
export const WEEKLY_SUBJECT = "Worth doing on your laptop this week";

// The Saturday email: briefings from the last week that need a laptop session. Never urgent.
export function buildWeeklyEmail(d, env) {
  const all = d.weekly || [], items = all.slice(0, WEEKLY_MAX), more = all.length - items.length;
  const html = `<div style="${WRAP}">
<h1 style="font-size:20px;margin:0 0 8px">${WEEKLY_SUBJECT}</h1>
<p style="margin:0 0 20px;color:${MUTED}">Nothing here is urgent. Each one is a laptop session: you and Claude, with the project's code in front of you.</p>
${items.map((i) => itemCard(env, i, "Open the briefing")).join("")}
${more > 0 ? `<p style="color:${MUTED}">And ${more} more on the dashboard.</p>` : ""}
${dashLink(env)}
</div>`;
  const text = [
    WEEKLY_SUBJECT,
    "Nothing here is urgent. Each one is a laptop session.",
    ...items.flatMap((i) => ["", ...head(i), ...lineText(i), deepLink(env, i.slug)]),
    more > 0 ? ["", `And ${more} more on the dashboard.`] : [],
    "",
    env.DASHBOARD_URL,
  ].flat().filter((line, n, all) => line || all[n - 1]).join("\n");
  return { subject: WEEKLY_SUBJECT, html, text };
}

export function buildAlertEmail(alerts, env) {
  const subject = alerts.length === 1 ? `Reviewer replied on ${alerts[0].key}` : `${alerts.length} reviewers replied`;
  const html = `<div style="font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#16201d;max-width:560px">
<ul style="padding-left:20px;margin:0">${alerts.map((a) => `<li style="margin:4px 0">${linked(env, a.slug, `${esc(a.author)} on ${esc(a.key)}`)}</li>`).join("")}</ul>
${openButton(env)}
</div>`;
  const plain = [...alerts.map((a) => `${a.author} on ${a.key}: ${deepLink(env, a.slug)}`), env.DASHBOARD_URL].join("\n");
  return { subject, html, text: plain };
}

async function sendMail(env, mail, fetchFn) {
  const out = await fetchFn("https://api.resend.com/emails", {
    method: "POST",
    headers: { authorization: `Bearer ${env.RESEND_API_KEY}`, "content-type": "application/json" },
    body: JSON.stringify({ from: env.MAIL_FROM || "OSS Scout <scout@aadityad.dev>", to: [env.OWNER_EMAIL], ...mail }),
  });
  if (!out.ok) throw new Error(`Resend said ${out.status}`);
}

export async function sendDigest(env, { fetchFn = fetch, now = Date.now() } = {}) {
  const res = await dataFile(env, "digest.json", fetchFn);
  if (!res.ok) return { sent: false, reason: `digest.json: ${res.status}` };
  const d = await res.json();
  const today = new Date(now).toISOString().slice(0, 10);
  if (!d.send) return { sent: false, reason: "nothing to send" };
  if (d.date !== today) return { sent: false, reason: `digest is from ${d.date}, not ${today}` };
  const mail = buildEmail(d, env);
  await sendMail(env, mail, fetchFn);
  return { sent: true, subject: mail.subject };
}

// Saturday morning: briefings from the last week that need a laptop session. Not sent when there are none.
export async function sendWeekly(env, { fetchFn = fetch, now = Date.now() } = {}) {
  const res = await dataFile(env, "digest.json", fetchFn);
  if (!res.ok) return { sent: false, reason: `digest.json: ${res.status}` };
  const d = await res.json();
  const today = new Date(now).toISOString().slice(0, 10);
  if (!Array.isArray(d.weekly) || !d.weekly.length) return { sent: false, reason: "nothing to send" };
  if (d.date !== today) return { sent: false, reason: `digest is from ${d.date}, not ${today}` };
  const mail = buildWeeklyEmail(d, env);
  await sendMail(env, mail, fetchFn);
  return { sent: true, subject: mail.subject };
}

// Quiet hours are 23:00 to 07:00 Pacific. Intl knows when daylight time starts and ends.
export function inQuietHours(now) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: QUIET_TZ, hour: "numeric", hourCycle: "h23" }).formatToParts(now);
  const hour = Number(parts.find((p) => p.type === "hour").value) % 24;
  return hour >= 23 || hour < 7;
}

// Emails when a reviewer has replied. The data repo's track.yml rewrites alerts.json
// every 3 hours; replies that land in quiet hours are not emailed (the digest has them).
export async function sendAlerts(env, { fetchFn = fetch, now = Date.now() } = {}) {
  const res = await dataFile(env, "alerts.json", fetchFn);
  if (!res.ok) return { sent: false, reason: `alerts.json: ${res.status}` };
  const data = await res.json();
  const alerts = Array.isArray(data.alerts) ? data.alerts : [];
  if (!alerts.length) return { sent: false, reason: "no alerts" };
  if (!(now - Date.parse(data.generated_at) <= ALERT_WINDOW_MS)) return { sent: false, reason: `alerts are from ${data.generated_at}, too old` };
  if (inQuietHours(now)) return { sent: false, reason: "quiet hours" };
  const mail = buildAlertEmail(alerts, env);
  await sendMail(env, mail, fetchFn);
  return { sent: true, subject: mail.subject };
}

// -- review check ------------------------------------------------------------------

// Starts the data repo's track.yml, which looks for reviewer replies and rewrites alerts.json.
export async function dispatchTrack(env, { fetchFn = fetch } = {}) {
  const res = await gh(env, `/repos/${env.DATA_REPO}/actions/workflows/track.yml/dispatches`, fetchFn, {
    method: "POST",
    headers: { accept: "application/vnd.github+json", "content-type": "application/json" },
    body: JSON.stringify({ ref: "main" }),
  });
  if (res.status !== 204) throw new Error(`GitHub refused track.yml (${res.status})`);
  return { dispatched: true };
}

// -- cron ----------------------------------------------------------------------------

export const route = (cron) => CRONS[cron] || null;

export default {
  fetch: (request, env) => handle(request, env),
  scheduled: (event, env, ctx) => {
    const job = route(event.cron);
    if (job === "digest") ctx.waitUntil(sendDigest(env));
    else if (job === "track") ctx.waitUntil(dispatchTrack(env));
    else if (job === "alerts") ctx.waitUntil(sendAlerts(env));
    else if (job === "weekly") ctx.waitUntil(sendWeekly(env));
    else console.log(`unknown cron "${event.cron}", doing nothing`);
  },
};
