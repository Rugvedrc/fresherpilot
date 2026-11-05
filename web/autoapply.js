// Auto-apply page: live status of the run (every 2 s) and controls that act right away.
// Clicks are recorded by the app at once (counts and lists change immediately); the extension picks them up
// within ~2 s, even in the middle of a job.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ICON = { applied: "✅", emailed: "📧", email_failed: "📧⚠", skipped: "⏭", failed: "⚠", parked: "✋", waiting: "✋",
               resumed: "▶", start: "▶", finished: "🏁" };
let S = null;
let filter = "";

function toast(text) {
  const t = $("toast");
  t.textContent = text;
  t.style.display = "block";
  clearTimeout(toast.h);
  toast.h = setTimeout(() => (t.style.display = "none"), 2500);
}

const hasExtension = () => !!document.documentElement.dataset.jobfinderExtension;
function toExtension(message) {
  return new Promise((resolve, reject) => {
    const id = Math.random().toString(36).slice(2);
    const onMsg = (e) => {
      if (e.source !== window || !e.data || e.data.__jf !== "from-extension" || e.data.id !== id) return;
      window.removeEventListener("message", onMsg);
      const r = e.data.response;
      r && r.ok ? resolve(r.data) : reject(new Error((r && r.error) || "Extension didn't answer"));
    };
    window.addEventListener("message", onMsg);
    window.postMessage({ __jf: "to-extension", id, message }, "*");
    setTimeout(() => { window.removeEventListener("message", onMsg); reject(new Error("Extension didn't answer. Reload it in chrome://extensions, then refresh this page.")); }, 10000);
  });
}

async function send(action, uid = null, btn = null) {
  if (btn) btn.closest(".job, #controls")?.classList.add("busy");
  try {
    const r = await fetch("/api/autoapply/command", { method: "POST", headers: { "Content-Type": "application/json" },
                                                      body: JSON.stringify(uid ? { action, uid } : { action }) });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    toast({ applied: "Marked as applied ✅", skip: "Skipped ⏭", stop: "Stopping… (the open job ends within ~2 s)", pause: "Paused after this job ⏸",
            resume: "Resumed ▶", continue: "Continuing this job ▶", open: "Opening its tab…" }[action] || "Done");
  } catch (err) {
    toast(`Didn't work: ${err.message}`);
  }
  await refresh();
}

const ago = (iso) => {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};
const time = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

function render() {
  const s = S;
  const emailed = s.log.filter((e) => e.event === "emailed").length;
  $("state").textContent = s.running ? (s.paused ? "⏸ Paused (set-aside jobs are still watched)" : "▶ Running in your browser") : "Not running";
  $("start").hidden = s.running;
  $("chooser").hidden = s.running;
  if (render.wasRunning && !s.running) loadPlatforms();  // counts change after a run
  render.wasRunning = s.running;
  $("pause").hidden = !s.running || s.paused;
  $("resume").hidden = !s.running || !s.paused;
  $("stop").hidden = !s.running;
  const pct = s.total ? Math.round((100 * (s.index || 0)) / s.total) : 0;
  $("bar").style.width = `${pct}%`;
  $("progress").textContent = s.total ? `Job ${s.index || 0} of ${s.total} (${pct}%)` : "";
  $("nApplied").textContent = s.applied || 0;
  $("nEmailed").textContent = emailed;
  $("nParked").textContent = (s.parked || []).length;
  $("nSkipped").textContent = s.skipped || 0;
  $("nFailed").textContent = s.failed || 0;

  const c = s.current;
  if (s.running && c) {
    const last = [...s.log].reverse().find((e) => e.uid === c.uid && e.event !== "job_start");
    const secs = s.current_at ? (Date.now() - Date.parse(s.current_at)) / 1000 : 0;
    $("current").className = "";
    $("current").innerHTML = `<div class="job"><div class="who"><b>${esc(c.title)}</b> · ${esc(c.company || "")} <span class="small muted">(${esc(c.source || "")})</span>
      <div class="why">${s.current_at ? `On it for ${ago(s.current_at)}` : ""}${secs > 240 ? ' <span class="warn">· slow page: it is set aside automatically at 6 min</span>' : ""}
      ${last ? ` · last: ${esc(last.event)} ${esc((last.detail || "").slice(0, 120))}` : ""}${s.waiting ? ` · ✋ ${esc(s.waiting.reason || "")}` : ""}</div></div>
      <div class="row"><button class="btn" data-a="applied">✅ I applied it myself</button><button class="btn" data-a="skip">⏭ Skip this job</button>
      ${c.url ? `<a class="btn" href="${esc(c.url)}" target="_blank" rel="noopener">Open job ↗</a>` : ""}</div></div>`;
  } else {
    $("current").className = "empty";
    $("current").textContent = s.running ? "Between jobs…" : "Nothing running.";
  }

  const parked = s.parked || [];
  $("parked").className = parked.length ? "" : "empty";
  $("parked").innerHTML = parked.length ? parked.map((p) => `<div class="job" data-uid="${esc(p.uid)}"><div class="who"><b>${esc(p.title || p.uid)}</b> · ${esc(p.company || "")}
      <div class="why">${esc(p.reason || "")} · waiting ${ago(p.at)} (skipped after 45 min)</div></div>
      <div class="row"><button class="btn" data-p="open">Open tab</button><button class="btn primary" data-p="applied">✅ I applied</button>
      <button class="btn" data-p="continue">▶ Done, let the bot continue</button><button class="btn" data-p="skip">⏭ Skip</button></div></div>`).join("")
    : "Nothing waiting.";

  const nxt = s.next || [];
  $("next").className = nxt.length ? "" : "empty";
  $("next").innerHTML = nxt.length ? nxt.map((j, i) => `<div class="small">${i + 1}. ${esc(j.title)} · ${esc(j.company || "")} <span class="muted">(${esc(j.source || "")}${j.kind ? `, ${esc(j.kind)}` : ""})</span></div>`).join("") : "—";

  const rows = [...s.log].reverse().filter((e) => e.event !== "job_start" && (!filter || e.event === filter || (filter === "parked" && e.event === "waiting")));
  $("log").innerHTML = rows.length ? rows.map((e) => `<tr><td class="t">${time(e.at)}</td><td>${ICON[e.event] || ""} ${esc(e.event)}</td>
      <td><b>${esc(e.title || "")}</b>${e.company ? ` · ${esc(e.company)}` : ""}<div class="why">${esc(e.detail || "")}</div></td></tr>`).join("")
    : `<tr><td class="empty">Nothing yet.</td></tr>`;
}

async function refresh() {
  try {
    S = await (await fetch("/api/autoapply")).json();
    render();
  } catch (_) { $("state").textContent = "App not reachable (restarting?)"; }
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.a) send(b.dataset.a, null, b);
  else if (b.dataset.p) send(b.dataset.p, b.closest("[data-uid]").dataset.uid, b);
  else if (b.dataset.f !== undefined) {
    filter = b.dataset.f;
    document.querySelectorAll("#filters .chip").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    render();
  }
});
$("pause").onclick = () => send("pause");
$("resume").onclick = () => send("resume");
$("stop").onclick = () => { if (confirm("Stop auto-apply? The job being applied right now is ended.")) send("stop"); };
// Choosing what to apply to: platforms (remembered), "found in the last N hours", search, and the job list itself
// (untick any job). Blocked companies and never-again jobs are left out by the app and listed below the chooser.
const PLAT_KEY = "fp-autoapply-platforms", HOURS_KEY = "fp-autoapply-hours";
const store = { get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } } };
let P = { platforms: [], jobs: [], blocked: [], never: [] };
const off = new Set();  // jobs you unticked (this page visit)

const ticked = () => [...document.querySelectorAll("#platforms input:checked")].map((x) => x.value);
function shown() {
  const plats = new Set(ticked());
  const hours = +$("hours").value || 0;
  const cutoff = hours ? Date.now() - hours * 3600e3 : 0;
  const q = $("q").value.trim().toLowerCase();
  return P.jobs.filter((x) => plats.has(x.source) && (!cutoff || Date.parse(x.found || 0) >= cutoff)
    && (!q || `${x.title} ${x.company}`.toLowerCase().includes(q)));
}
const since = (iso) => {  // "12 min ago", "3 h ago", "2 days ago"
  const m = Math.max(0, Math.round((Date.now() - Date.parse(iso || 0)) / 60000));
  if (!iso || !Number.isFinite(m)) return "some time ago";
  return m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};
const picked = () => shown().filter((x) => !off.has(x.uid));

function renderChooser() {
  const saved = JSON.parse(store.get(PLAT_KEY) || "null");
  if (!$("platforms").dataset.built) {
    $("platforms").innerHTML = P.platforms.length ? P.platforms.map((p) => {
      const on = saved ? saved.includes(p.source) : p.source !== "Internshala";
      return `<label class="plat"><input type="checkbox" value="${esc(p.source)}" ${on ? "checked" : ""}> <b>${esc(p.source)}</b>
        <span class="small muted">${p.total}</span></label>`;
    }).join("") : `<span class="small muted">No approved jobs left to apply to.</span>`;
    $("platforms").dataset.built = "1";
  }
  const list = shown();
  const n = list.filter((x) => !off.has(x.uid)).length;
  $("pickCount").textContent = `${n} job${n === 1 ? "" : "s"} ticked (of ${list.length} shown)`;
  $("start").textContent = n ? `▶ Apply to these ${n} job${n === 1 ? "" : "s"}` : "▶ Apply";
  $("start").disabled = !n;
  $("pickList").innerHTML = list.length ? list.slice(0, 800).map((x) => `<label class="jr"><input type="checkbox" data-job="${esc(x.uid)}" ${off.has(x.uid) ? "" : "checked"}>
      <span class="who"><b>${esc(x.title)}</b> · ${esc(x.company)} <span class="small muted">(${esc(x.source)}${x.mode === "easy" ? ", easy apply" : ""})</span>
      <span class="why" style="display:block">found ${since(x.found)}${x.location ? ` · ${esc(x.location)}` : ""}</span>
      <span style="display:block"><a class="mini" href="${esc(x.url || "#")}" target="_blank" rel="noopener">open ↗</a>
      <button class="mini" type="button" data-never="${esc(x.uid)}">never this job</button>
      <button class="mini" type="button" data-block="${esc(x.company)}">never ${esc(x.company)}</button></span></span></label>`).join("")
    : `<div class="empty">No jobs match. Tick more platforms or pick a longer time.</div>`;
  $("nBlocked").textContent = P.blocked.length;
  $("blockedList").innerHTML = P.blocked.map((c) => `<span class="chip">${esc(c)} <button class="mini" type="button" data-unblock="${esc(c)}" aria-label="Unblock ${esc(c)}">✕</button></span>`).join("")
    || `<span class="small muted">None.</span>`;
  $("companies").innerHTML = [...new Set(P.jobs.map((x) => x.company))].sort().map((c) => `<option value="${esc(c)}">`).join("");
  $("nNever").textContent = P.never.length;
  $("neverList").innerHTML = P.never.length ? P.never.slice(0, 300).map((x) => `<div class="job"><div class="who"><b>${esc(x.title || x.uid)}</b> · ${esc(x.company || "")}
      <div class="why">${esc(x.reason || "")}</div></div><button class="btn" type="button" data-allow="${esc(x.uid)}">Allow again</button></div>`).join("")
    : `<div class="empty">None.</div>`;
}

async function loadChooser() {
  try {
    P = await (await fetch("/api/autoapply/preview")).json();
    $("platforms").dataset.built = "";
    renderChooser();
  } catch (_) { $("platforms").innerHTML = `<span class="small muted">Couldn't load the jobs.</span>`; }
}
const loadPlatforms = loadChooser;  // (render() reloads it after a run)

async function choose(body) {
  try {
    const r = await fetch("/api/autoapply/choices", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    toast({ block: `Never auto-applying to ${body.company}`, unblock: `${body.company} allowed again`, never: "This job is left out for good",
            allow: "This job can be auto-applied again" }[body.action]);
  } catch (err) { toast(`Didn't work: ${err.message}`); }
  await loadChooser();
}

document.addEventListener("change", (e) => {
  if (e.target.closest("#platforms")) { store.set(PLAT_KEY, JSON.stringify(ticked())); renderChooser(); }
  else if (e.target.dataset.job) { e.target.checked ? off.delete(e.target.dataset.job) : off.add(e.target.dataset.job); renderChooser(); }
  else if (e.target.id === "hours") { store.set(HOURS_KEY, e.target.value); renderChooser(); }
});
$("q").addEventListener("input", renderChooser);
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-pick],[data-never],[data-block],[data-unblock],[data-allow],#blockBtn");
  if (!t) return;
  if (t.dataset.never || t.dataset.block) e.preventDefault();  // inside a <label>: don't toggle the checkbox
  if (t.dataset.pick) { shown().forEach((x) => (t.dataset.pick === "all" ? off.delete(x.uid) : off.add(x.uid))); renderChooser(); }
  else if (t.dataset.never) choose({ action: "never", uid: t.dataset.never });
  else if (t.dataset.block) { if (confirm(`Never auto-apply to any job at ${t.dataset.block}?`)) choose({ action: "block", company: t.dataset.block }); }
  else if (t.dataset.unblock) choose({ action: "unblock", company: t.dataset.unblock });
  else if (t.dataset.allow) choose({ action: "allow", uid: t.dataset.allow });
  else if (t.id === "blockBtn") { const c = $("blockInput").value.trim(); if (c) { $("blockInput").value = ""; choose({ action: "block", company: c }); } }
});
{ const h = store.get(HOURS_KEY); if (h !== null) $("hours").value = h; }

$("start").onclick = async () => {
  if (!hasExtension()) return alert("The FresherPilot extension isn't active on this page. Reload it in chrome://extensions, then refresh this page.");
  const pick = picked().map((x) => x.uid);
  if (!pick.length) return alert("Tick at least one job.");
  if (!confirm(`Apply to ${pick.length} job${pick.length === 1 ? "" : "s"}, one by one in background tabs?\nEasy Apply first. Jobs that need you wait in “Waiting for you” while it goes on.`)) return;
  try {
    const res = await fetch("/api/autoapply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max: 0, pick }) });
    const start = await res.json();
    if (!res.ok) throw new Error(start.detail || "Couldn't start");
    await toExtension({ type: "jf-autoapply-start", start });
    toast("Started ▶");
  } catch (err) { alert(err.message); }
  refresh();
};

loadChooser();
refresh();
setInterval(refresh, 2000);
