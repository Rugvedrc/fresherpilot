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
$("start").onclick = async () => {
  if (!hasExtension()) return alert("The FresherPilot extension isn't active on this page. Reload it in chrome://extensions, then refresh this page.");
  if (!confirm("Apply to ALL approved jobs, one by one in background tabs?\nEasy Apply jobs go first, company sites after. Jobs that need you wait in “Waiting for you” while it goes on.")) return;
  try {
    const res = await fetch("/api/autoapply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ max: 0 }) });
    const start = await res.json();
    if (!res.ok) throw new Error(start.detail || "Couldn't start");
    await toExtension({ type: "jf-autoapply-start", start });
    toast("Started ▶");
  } catch (err) { alert(err.message); }
  refresh();
};

refresh();
setInterval(refresh, 2000);
