// Password-protected web dashboard served by the Worker at /app (Nov 2026): the jobs the cloud found and
// approved, the same Applied / Seen / Not-for-me actions as the Telegram buttons, and stats per source.
// Password = Worker secret DASH_PASSWORD (never in the code). Login = an HMAC-signed cookie (30 days).
// 10 wrong passwords from one IP in 15 min = that IP is blocked for 15 min.

const DAY = 86400 * 1000;
const COOKIE = "fp_dash";
const enc = new TextEncoder();

async function hmac(secret, text) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(text));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function sameText(a, b) {  // constant-time compare
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
}

async function signedIn(req, env) {
  const m = (req.headers.get("Cookie") || "").match(new RegExp(`${COOKIE}=(\\d+)\\.([0-9a-f]{64})`));
  if (!m || +m[1] < Date.now()) return false;
  return sameText(m[2], await hmac(env.CLOUD_SECRET + "|dash", m[1]));
}

const html = (body, status = 200, headers = {}) =>
  new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
    "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", ...headers } });
const json = (d, s = 200) => new Response(JSON.stringify(d), { status: s, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

const STYLE = `
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#2563eb;--ok:#15803d;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--text:#e6e9ef;--muted:#98a2b3;--line:#2a303b;--accent:#60a5fa;--ok:#4ade80;--warn:#fbbf24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;z-index:2}
h1{font-size:17px;margin:0 auto 0 0}main{max-width:900px;margin:0 auto;padding:16px}
.tabs button,.f select{border:1px solid var(--line);background:var(--card);color:var(--text);border-radius:8px;padding:6px 12px;font:inherit;cursor:pointer}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.job{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 12px}
.job h3{margin:0 0 4px;font-size:16px}.meta{color:var(--muted);font-size:13px}.why{margin:8px 0 10px;font-size:14px}
.acts{display:flex;gap:8px;flex-wrap:wrap}.acts a,.acts button{border:1px solid var(--line);background:none;color:var(--text);border-radius:8px;padding:6px 10px;font:inherit;font-size:14px;cursor:pointer;text-decoration:none}
.acts a.apply{background:var(--accent);border-color:var(--accent);color:#fff}
.tag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);margin-left:6px}
.applied{color:var(--ok);border-color:var(--ok)}.seen{color:var(--muted)}.hidden{color:var(--warn);border-color:var(--warn)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}
.empty{color:var(--muted);text-align:center;padding:40px 0}
form.login{max-width:320px;margin:15vh auto;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
form.login input{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit;margin:8px 0 12px}
form.login button{width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
.err{color:#dc2626;font-size:14px}`;

function loginPage(msg = "") {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FresherPilot</title><meta name="robots" content="noindex"><style>${STYLE}</style></head><body>
<form class="login" method="post" action="/app/login"><h1>FresherPilot</h1><p class="meta">Enter the password to see your jobs.</p>
<input type="password" name="password" autocomplete="current-password" autofocus required>
${msg ? `<p class="err">${msg}</p>` : ""}<button>Open</button></form></body></html>`;
}

const APP = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FresherPilot</title><meta name="robots" content="noindex"><style>${STYLE}</style></head><body>
<header><h1>FresherPilot</h1><div class="tabs"><button data-t="jobs" class="on">Jobs</button> <button data-t="pipeline">Pipeline</button> <button data-t="stats">Stats</button></div>
<div class="f"><select id="days"><option value="1">Last 24 h</option><option value="3">3 days</option><option value="7" selected>7 days</option><option value="21">21 days</option></select>
<select id="show"><option value="open">Not marked</option><option value="all">All approved</option><option value="applied">Applied</option></select>
<select id="mode"><option value="">Any apply</option><option value="easy">📱 Easy Apply</option><option value="site">💻 Company site</option></select></div></header>
<main id="main"><p class="empty">Loading…</p></main>
<script>
const $ = (s) => document.querySelector(s), esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let tab = "jobs";
function ago(ms) { const m = (Date.now() - ms) / 60000; return m < 60 ? Math.round(m) + " min ago" : m < 2160 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " days ago"; }
async function load() {
  const r = await fetch("/app/api/" + tab + "?days=" + $("#days").value + "&show=" + $("#show").value + "&mode=" + $("#mode").value);
  if (r.status === 401) return location.reload();
  const d = await r.json();
  $("#main").innerHTML = tab === "jobs" ? jobs(d.jobs) : tab === "pipeline" ? pipeline(d) : stats(d);
}
function jobs(list) {
  if (!list.length) return '<p class="empty">No jobs here yet.</p>';
  return list.map((j) => \`<div class="job" data-uid="\${esc(j.id)}"><h3>\${esc(j.title)}\${j.state ? '<span class="tag ' + esc(j.state) + '">' + esc(j.state) + '</span>' : ''}</h3>
  <div class="meta">\${esc(j.company)} · \${esc(j.location)} · \${esc(j.source)} · found \${ago(j.found_at)}\${j.mode === "easy" ? " · 📱 Easy Apply" : j.mode === "site" ? " · 💻 Company site" : ""}</div>
  <div class="why">\${esc(j.why)}</div><div class="acts">\${j.url ? '<a class="apply" target="_blank" rel="noopener" href="' + esc(j.url) + '">Apply</a>' : ''}
  <button data-a="a">Applied</button><button data-a="v">Seen</button><button data-a="x">Not for me</button></div></div>\`).join("");
}
function pipeline(p) {
  const when = (m) => m === null ? "never" : m < 60 ? m + " min ago" : Math.round(m / 60) + " h ago";
  const rows = p.sources.map((x) => \`<tr><td>\${x.status === "ok" ? "✅" : "⚠️"} \${esc(x.name)}<div class="meta">\${esc(x.reader)}\${x.note ? " · " + esc(x.note) : ""}</div></td>
    <td>\${when(x.last_read_min)}<div class="meta">every ~\${x.every_min} min</div></td><td>\${x.found_1h}</td><td>\${x.found_24h}</td><td>\${x.approved_24h}</td></tr>\`).join("");
  const s = p.stages;
  const step = (label, n, warn) => \`<div class="job" style="flex:1;min-width:130px;margin:0"><div class="meta">\${label}</div><h3>\${warn ? "⚠️ " : ""}\${n}</h3></div>\`;
  return \`<h3 style="margin:0 0 8px">Sources</h3><table><tr><th>Source</th><th>Last read</th><th>New 1 h</th><th>New 24 h</th><th>Approved 24 h</th></tr>\${rows}</table>
  <h3 style="margin:18px 0 8px">Last hour, stage by stage</h3><div style="display:flex;gap:8px;flex-wrap:wrap">
  \${step("New jobs found", s.found_1h)}\${step("Ruled out by rules", s.rule_rejected_1h)}\${step("Rejected by AI", s.ai_rejected_1h)}
  \${step("Approved", s.approved_1h)}\${step("Alerts sent", s.alerts_1h)}</div>
  <p class="meta" style="margin-top:12px">\${s.waiting_ai ? "⏳" : "✅"} Waiting for the AI check: \${s.waiting_ai}\${s.oldest_waiting_min !== null ? " (oldest " + when(s.oldest_waiting_min) + ")" : ""}
  \${s.stuck_ai ? " · ⚠️ " + s.stuck_ai + " failed 3 times" : ""} · Alerts in 24 h: \${s.alerts_24h}, last \${when(s.last_alert_min)}
  \${p.alerts_paused ? " · ⚠️ alerts PAUSED (send /resume in Telegram)" : ""}\${p.last_error ? "<br>Last error: " + esc(p.last_error) : ""}</p>\`;
}
function stats(d) {
  const rows = d.sources.map((s) => \`<tr><td>\${esc(s.source)}</td><td>\${s.read}</td><td>\${s.kept}</td><td>\${s.read ? (100 * s.kept / s.read).toFixed(1) : 0}%</td></tr>\`).join("");
  return \`<table><tr><th>Source</th><th>Read</th><th>Approved</th><th>Rate</th></tr>\${rows}</table>
  <p class="meta">Waiting for the AI check: \${d.waiting}. Alerts sent: \${d.alerts}. Applied: \${d.applied}.</p>\`;
}
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-t]"), a = e.target.closest("[data-a]");
  if (t) { tab = t.dataset.t; document.querySelectorAll("[data-t]").forEach((b) => b.classList.toggle("on", b === t)); load(); }
  if (a) { const card = a.closest(".job"); await fetch("/app/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ uid: card.dataset.uid, action: a.dataset.a }) }); load(); }
});
$("#days").onchange = load; $("#show").onchange = load; $("#mode").onchange = load; load();
</script></body></html>`;

export async function handleApp(req, env, url, setState, getState, pipelineStatus) {
  const now = Date.now();
  if (url.pathname === "/app/login" && req.method === "POST") {
    const ip = req.headers.get("CF-Connecting-IP") || "?";
    const fails = (await getState(env, "dash_fail", {})) || {};
    const f = fails[ip] && now - fails[ip][1] < 15 * 60 * 1000 ? fails[ip] : [0, now];
    if (f[0] >= 10) return html(loginPage("Too many tries. Wait 15 minutes."), 429);
    const pw = String((await req.formData()).get("password") || "");
    if (!env.DASH_PASSWORD || !sameText(await hmac("pw", pw), await hmac("pw", env.DASH_PASSWORD))) {
      fails[ip] = [f[0] + 1, f[1]];
      await setState(env, "dash_fail", fails);
      return html(loginPage(env.DASH_PASSWORD ? "Wrong password." : "Dashboard password not set."), 401);
    }
    const exp = String(now + 30 * DAY);
    return new Response(null, { status: 303, headers: { Location: "/app",
      "Set-Cookie": `${COOKIE}=${exp}.${await hmac(env.CLOUD_SECRET + "|dash", exp)}; Path=/app; Max-Age=${30 * 86400}; HttpOnly; Secure; SameSite=Lax` } });
  }
  const ok = await signedIn(req, env);
  if (url.pathname.startsWith("/app/api/")) {
    if (!ok) return json({ detail: "sign in" }, 401);
    const days = Math.min(21, Math.max(1, +(url.searchParams.get("days") || 7)));
    const since = now - days * DAY;
    if (url.pathname === "/app/api/jobs") {
      const show = url.searchParams.get("show") || "open";
      const where = show === "applied" ? "AND a.state = 'applied'" : show === "open" ? "AND (a.state IS NULL OR a.state = 'dup')" : "";
      const rows = (await env.DB.prepare(`SELECT j.id, j.title, j.company, j.location, j.source, j.url, j.reason, j.fit, j.found_at,
        j.apply_mode, a.state FROM jobs j LEFT JOIN alerts a ON a.uid = j.id WHERE j.status = 'keep' AND j.found_at > ? ${where}
        AND COALESCE(a.state, '') NOT IN ('hidden', 'old') ORDER BY j.found_at DESC LIMIT 300`).bind(since).all()).results;
      const want = url.searchParams.get("mode") || "";
      const byDefault = { Internshala: "easy", Wellfound: "easy", Careers: "site", Telegram: "site" };
      return json({ jobs: rows.map((r) => {
        let why = r.reason;
        try { why = JSON.parse(r.fit || "{}").why || why; } catch { /* keep the reason */ }
        const mode = r.apply_mode || byDefault[r.source] || "";
        return { ...r, why, mode, fit: undefined, reason: undefined, apply_mode: undefined, state: r.state === "dup" ? null : r.state };
      }).filter((j) => !want || j.mode === want) });
    }
    if (url.pathname === "/app/api/pipeline") return json(await pipelineStatus(env));
    if (url.pathname === "/app/api/stats") {
      const sources = (await env.DB.prepare(`SELECT source, COUNT(*) AS read, SUM(status = 'keep') AS kept FROM jobs
        WHERE found_at > ? GROUP BY source ORDER BY kept DESC`).bind(since).all()).results;
      const c = await env.DB.prepare(`SELECT (SELECT COUNT(*) FROM jobs WHERE status = 'new' AND tries < 3) AS waiting,
        (SELECT COUNT(*) FROM alerts WHERE at > ? AND text != '') AS alerts,
        (SELECT COUNT(*) FROM alerts WHERE at > ? AND state = 'applied') AS applied`).bind(since, since).first();
      return json({ sources, ...c });
    }
    if (url.pathname === "/app/api/action" && req.method === "POST") {
      const { uid, action } = await req.json();
      const state = { a: "applied", v: "seen", x: "hidden" }[action];
      if (!uid || !state) return json({ detail: "bad action" }, 400);
      await env.DB.batch([
        env.DB.prepare("INSERT INTO actions (uid, action, at) VALUES (?, ?, ?)").bind(uid, action, now),
        env.DB.prepare("INSERT INTO alerts (uid, text, source, state, at) VALUES (?, '', '', ?, ?) ON CONFLICT(uid) DO UPDATE SET state = excluded.state").bind(uid, state, now),
      ]);
      return json({ ok: true });
    }
    return json({ detail: "not found" }, 404);
  }
  if (!ok) return html(loginPage());
  return html(APP);
}
