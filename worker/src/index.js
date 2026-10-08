// The private dashboard at me.aadityad.dev.
//
// Serves dashboard.html from the private data repo, turns a button press into a
// run of the data repo's act workflow (which does the GitHub writes, with its own
// token), sends the daily email, starts the data repo's review check every 3 hours
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
export const CRONS = { "0 15 * * *": "digest", "0 */3 * * *": "track", "30 */3 * * *": "alerts" };

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
const openButton = (env) =>
  `<p style="margin:24px 0"><a href="${esc(env.DASHBOARD_URL)}" style="display:inline-block;background:#1e6b5a;color:#fff;text-decoration:none;font-weight:600;font-size:18px;padding:14px 26px;border-radius:10px">Open the dashboard</a></p>`;

// "2026-10-12" as "Oct 12", read as a plain date so the timezone can't move it
const shortDate = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso ?? ""));
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }) : "";
};

export function buildEmail(d, env) {
  const ready = d.ready || [], waiting = d.waiting_on_you || [], fresh = d.new_briefings || [], pairing = d.pairing || [];
  const stuck = d.stuck || [];
  const parts = [];
  if (stuck.length === 1) parts.push(`Couldn't send ${stuck[0].key}`);
  else if (stuck.length) parts.push(`${stuck.length} couldn't send`);
  const late = waiting.some((w) => w.overdue) ? "overdue" : "needed";
  if (waiting.length === 1) parts.push(`Reply ${late} on ${waiting[0].key}`);
  else if (waiting.length) parts.push(`${waiting.length} replies ${late}`);
  if (ready.length) parts.push(`${ready.length} ready`);
  if (fresh.length) parts.push(`${fresh.length} new`);
  if (d.token_warning) parts.push("token needs rotating");
  const subject = parts.join(" · ") || "Nothing needs you today";

  const list = (title, items, line) =>
    items.length ? `<h2 style="font-size:16px;margin:20px 0 6px">${title}</h2><ul style="padding-left:20px;margin:0">${items.map((i) => `<li style="margin:4px 0">${line(i)}</li>`).join("")}</ul>` : "";
  const titled = (i) => linked(env, i.slug, `${esc(i.title)} <span style="color:#5b6964">${esc(i.key)}</span>`);
  const when = (i) => (i.refreshing ? "Refreshing now." : shortDate(i.refresh_by) ? `Refresh by ${shortDate(i.refresh_by)}.` : "");
  const stuckList = stuck.length
    ? `<h2 style="font-size:16px;margin:20px 0 6px;color:#cf222e">Couldn't send</h2>${stuck.map((i) => `<div style="margin:0 0 12px">${linked(env, i.slug, `<b>${esc(i.plain)}</b>`)}${i.why ? `<br>${esc(i.why)}` : ""}${when(i) ? `<br><b>${esc(when(i))}</b>` : ""}<br><span style="color:#5b6964">${esc(i.title)} ${esc(i.key)}</span></div>`).join("")}`
    : "";
  const html = `<div style="font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#16201d;max-width:560px">
<h1 style="font-size:20px;margin:0 0 8px">OSS Scout · ${esc(d.date)}</h1>
${stuckList}
${d.token_warning ? `<p style="background:#fff1e0;padding:10px 12px;border-radius:8px"><b>Your GitHub token is ${esc(d.token_age_days)} days old.</b> Rotate it soon: submitting stops when it expires.</p>` : ""}
${list("Ready for one tap", ready, titled)}
${list("Waiting on you", waiting, (w) => `${linked(env, w.slug, esc(w.key))}${w.overdue ? ' <b style="color:#cf222e">overdue</b>' : ""}`)}
${list("New briefings", fresh, titled)}
${pairing.length ? `<p style="margin:20px 0 0">Pairing queue: ${pairing.length}</p>` : ""}
${openButton(env)}
</div>`;
  const plain = [
    `OSS Scout ${d.date}`,
    ...(stuck.length ? ["Couldn't send:"] : []),
    ...stuck.flatMap((i) => [`${i.plain}${i.why ? ` ${i.why}` : ""}${when(i) ? ` ${when(i)}` : ""} (${i.key})`]),
    d.token_warning ? `Your GitHub token is ${d.token_age_days} days old. Rotate it soon.` : "",
    ...ready.map((i) => `Ready: ${i.title} (${i.key})`),
    ...waiting.map((w) => `Waiting on you: ${w.key}${w.overdue ? " (overdue)" : ""}`),
    ...fresh.map((i) => `New: ${i.title} (${i.key})`),
    pairing.length ? `Pairing queue: ${pairing.length}` : "",
    env.DASHBOARD_URL,
  ].filter(Boolean).join("\n");
  return { subject, html, text: plain };
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
    else console.log(`unknown cron "${event.cron}", doing nothing`);
  },
};
