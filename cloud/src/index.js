// FresherPilot Cloud: reads job sources, AI-checks new jobs with your Groq keys and sends Telegram alerts,
// all on Cloudflare, so alerts keep coming while the laptop is off.
//
// Hand-over with the laptop app: the app sends a heartbeat every few minutes. While it's fresh, the Worker
// doesn't read any source (the app does, with more boards); it only sends the app's alerts (one place for
// "never alert the same job twice") and answers the Telegram buttons. When the laptop is quiet for 15 min,
// the Worker reads Telegram, LinkedIn (lightly), company career pages and Naukri (Apify) itself.
// When the app is back it pulls the jobs found meanwhile and the button taps (/api/jobs, /api/actions).
//
// Free plan limits respected: one source step per minute, at most ~45 outgoing requests per run.
// Config (prompts, searches, rules) is made from the laptop app's own settings by cloud/deploy.py.
import CONFIG from "./config.json";
import { handleApp } from "./dashboard.js";

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const LAPTOP_QUIET_MS = 15 * 60 * 1000;
const MAX_ALERTS_PER_RUN = 6;
const DAY = 86400 * 1000;

// ---------------------------------------------------------------- small helpers

class Budget {  // free plan: 50 subrequests per invocation
  constructor(max = 45) { this.left = max; }
  async fetch(url, init) {
    if (this.left <= 0) throw new Error("subrequest budget used");
    this.left--;
    return fetch(url, init);
  }
}

const now = () => Date.now();
const norm = (s) => String(s ?? "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const decode = (s) => String(s ?? "")
  .replace(/<br\s*\/?>/gi, "\n").replace(/<\/p>/gi, "\n").replace(/<[^>]+>/g, " ")
  .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"').replace(/&#39;|&#x27;|&apos;/g, "'")
  .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(+n)).replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
  .replace(/[ \t\r\f\v]+/g, " ").replace(/ *\n */g, "\n").trim();
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const daysAgo = (iso) => { const t = Date.parse(iso || ""); return Number.isFinite(t) ? Math.max(0, Math.floor((now() - t) / DAY)) : null; };
const host = (u) => { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; } };

async function getState(env, k, dflt = null) {
  const r = await env.DB.prepare("SELECT v FROM state WHERE k = ?").bind(k).first();
  return r ? JSON.parse(r.v) : dflt;
}
async function setState(env, k, v) {
  await env.DB.prepare("INSERT INTO state (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v").bind(k, JSON.stringify(v)).run();
}

// ---------------------------------------------------------------- same rules as the laptop app

function titleExcluded(title) {  // llm_filter.title_excluded
  let t = " " + String(title || "").toLowerCase().replace(/[(),|/]/g, " ") + " ";
  t = t.replace(/\s+/g, " ");
  const onStack = (CONFIG.on_stack_words || []).some((w) => new RegExp("(?<![a-z])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(t));
  for (const [cat, words0] of Object.entries(CONFIG.title_exclude)) {
    let words = words0;
    if (cat === "off_stack") { if (onStack) continue; words = CONFIG.off_stack || []; }
    for (const w of words) if (t.includes(w)) return [cat, w.trim()];
  }
  if (new RegExp(CONFIG.title_architect).test(t)) return ["too_senior", "architect"];
  if (!onStack && !new RegExp(CONFIG.title_soft_skip).test(t)) {
    for (const [cat, words] of Object.entries(CONFIG.title_exclude_soft || {}))
      for (const w of words) if (new RegExp("(?<![a-z])" + w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).test(t)) return [cat, w.trim()];
  }
  const m = String(title || "").match(new RegExp(CONFIG.title_years, "i"));
  if (m && parseFloat(m[1]) >= CONFIG.title_years_min) return ["needs_experience", m[0]];
  return null;
}

function boardReject(job) {  // llm_filter._board_reject (stretch jobs off)
  const words = titleExcluded(job.title);
  if (words) return words;
  const level = String(job.experience || "");
  if (job.source === "LinkedIn" && /director|executive/i.test(level)) return ["too_senior", "LinkedIn level"];
  const m = level.match(/^\s*(\d+(?:\.\d+)?)\s*(?:-\s*\d+\s*|\+\s*)years/i);
  if (m && parseFloat(m[1]) >= CONFIG.reject_from_years + (CONFIG.stretch_jobs ? 1 : 0)) return ["needs_experience", level];
  return null;
}

const EMP = { ft: "full_time", intern: "internship", intern_ft: "internship_then_full_time", contract: "contract", part: "part_time", freelance: "freelance", unclear: "unclear" };
const SEN = { entry: "entry", mid: "mid", senior: "senior", lead: "lead_or_manager", unclear: "unclear" };
const ROLE = { dev: "software_development", aiml: "ai_ml_data", devops: "devops_sre_cloud", qa_auto: "qa_automation", qa_manual: "qa_manual", support: "it_support", bpo: "customer_support_bpo", sales: "sales_marketing", non_sw: "non_software_engineering", other: "other_non_developer" };
const ALLOWED_ROLES = new Set(["software_development", "ai_ml_data", "devops_sre_cloud", "qa_automation"]);

function decide(f, job) {  // llm_filter.decide
  const freshers = !!f.freshers_welcome, years = f.min_years_required, level = String(job.experience || "");
  const platformMin = level.match(/^\s*(\d+)\s*-\s*\d+\s*years/i);
  if (["internship", "internship_then_full_time"].includes(f.employment_type) || /internship/i.test(level)) return ["reject", "internship"];
  if (!["full_time", "unclear"].includes(f.employment_type)) return ["reject", "not_full_time"];
  const rf = CONFIG.reject_from_years;
  const low = typeof years === "number" && years < rf;
  if (!["entry", "unclear"].includes(f.seniority) && !(f.seniority === "mid" && (freshers || low))) return ["reject", "too_senior"];
  if (/director|executive/i.test(level)) return ["reject", "too_senior"];
  if (/mid-senior/i.test(level) && !(freshers || low)) return ["reject", "too_senior"];
  if (!ALLOWED_ROLES.has(f.role_type)) return ["reject", f.role_type === "non_software_engineering" ? "non_software_engineering" : "non_developer"];
  if (!freshers) {
    if (typeof years === "number" && years >= rf) return ["reject", "needs_experience"];
    if (platformMin && parseInt(platformMin[1], 10) >= rf) return ["reject", "needs_experience"];
  }
  return ["keep", "ok"];
}

// visited._loose_keys: the same job from another board / a repost has the same keys
const COMPANY_NOISE = new Set(CONFIG.company_noise), NO_COMPANY = new Set(CONFIG.no_company), TITLE_NOISE = new Set(CONFIG.title_noise);
async function looseKeys(job) {
  const keys = [];
  const company = String(job.company || "");
  const words = norm(company.replace(/@/g, " ")).split(" ").filter((w) => w && !COMPANY_NOISE.has(w));
  const core = words.join(" ") || norm(company);
  const t = String(job.title || "").replace(/\([^)]*\)|\[[^\]]*\]/g, " ");
  const tcore = [...new Set(norm(t).split(" ").filter((w) => w && !TITLE_NOISE.has(w)))].sort().join(" ");
  if (!NO_COMPANY.has(norm(company)) && core && tcore) keys.push(`fz:${core}|${tcore}`);
  const desc = norm(job.description);
  if (desc.length >= 300) {
    const h = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(desc));
    keys.push("ds:" + [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 20));
  }
  return keys;
}
const uidOf = (job) => `${job.source}:${job.id}`;

const REMOTE = new RegExp(CONFIG.remote_regex, "i");
const NATIONWIDE = new RegExp(CONFIG.nationwide_regex || "$^", "i");
function inWantedLocation(job) {  // apify_jobs.in_wanted_location: unknown / "India"-only locations stay
  const words = CONFIG.city_words || [];
  if (!words.length && !CONFIG.remote_ok) return true;
  const loc = String(job.location || "").toLowerCase();
  if (!loc.trim() || ["india", "in"].includes(loc.trim().replace(/^[ ,]+|[ ,]+$/g, ""))) return true;
  if (CONFIG.remote_ok && REMOTE.test(loc)) return true;
  if (NATIONWIDE.test(loc)) return true;  // "PAN India", "Across India", "Virtual Drive": open to every city
  return words.some((w) => loc.includes(w));
}

// ---------------------------------------------------------------- storing what was read

async function addJobs(env, jobs) {
  if (!jobs.length) return 0;
  const ids = jobs.map(uidOf);
  const known = new Set();
  for (let i = 0; i < ids.length; i += 90) {
    const part = ids.slice(i, i + 90);
    const rows = await env.DB.prepare(`SELECT id FROM seen WHERE id IN (${part.map(() => "?").join(",")})`).bind(...part).all();
    rows.results.forEach((r) => known.add(r.id));
  }
  const fresh = jobs.filter((j) => !known.has(uidOf(j)));
  if (!fresh.length) return 0;
  const t = now();
  const stmts = [];
  for (const j of fresh) {
    const rej = !inWantedLocation(j) ? ["other_city", j.location] : boardReject(j);
    stmts.push(env.DB.prepare("INSERT OR IGNORE INTO seen (id, at) VALUES (?, ?)").bind(uidOf(j), t));
    stmts.push(env.DB.prepare(`INSERT OR IGNORE INTO jobs (id, source, title, company, location, experience, url, blog_url, posted,
      posted_days, description, work_type, status, category, reason, found_at, within_min, apply_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).bind(
      uidOf(j), j.source, j.title, j.company || "", j.location || "", j.experience || "", j.url || "", j.blog_url || "",
      j.posted || "", j.posted_days ?? null, (j.description || "").slice(0, 8000), j.work_type || "",
      rej ? "reject" : "new", rej ? rej[0] : null, rej ? `Rule: ${rej[1]}` : null, t, j.posted_within_min ?? null, j.apply_mode || null));
  }
  await env.DB.batch(stmts);
  return fresh.length;
}

// ---------------------------------------------------------------- sources

// Telegram channels (t.me/s/<name> web preview) + job-blog links resolved to the official apply link.
const LABELS = {
  company: /^\W*(?:company(?: name)?|organi[sz]ation|employer|hiring company)\s*[:\-–]\s*(.+)$/i,
  title: /^\W*(?:role|job role|profile|position|post|designation|job title|title|opening|exam|hiring test)\s*[:\-–]\s*(.+)$/i,  // exam: TCS NQT-style hiring tests
  experience: /^\W*(?:experience|exp|eligibility|batch|qualification)\s*[:\-–]\s*(.+)$/i,
  location: /^\W*(?:location|job location|place)\s*[:\-–]\s*(.+)$/i,
};
const PROMO = new RegExp(CONFIG.tg_promo, "i"), JOBWORD = new RegExp(CONFIG.tg_jobword, "i");
const AD_LINK = new RegExp(CONFIG.tg_ad_link, "i"), SKIP_LINK = new RegExp(CONFIG.tg_skip_link, "i");
const BLOG_HOSTS = CONFIG.blog_hosts, SHORTENERS = CONFIG.shorteners;
const isBlog = (u) => BLOG_HOSTS.some((b) => host(u) === b || host(u).endsWith("." + b));

function parsePost(text, links) {
  const apply = links.filter((u) => u.startsWith("http") && !SKIP_LINK.test(u));
  if (!apply.length) return null;
  const lines = [];
  for (let ln of text.split("\n").map((l) => l.replace(/^[\s•*\-–]+|[\s•*\-–]+$/g, ""))) {
    if (!/[A-Za-z0-9]/.test(ln)) continue;
    if (lines.length && lines[lines.length - 1].endsWith(":") && lines[lines.length - 1].length < 30) lines[lines.length - 1] += " " + ln;
    else lines.push(ln);
  }
  if (!lines.length) return null;
  const lab = (k) => { for (const l of lines) { const m = l.match(LABELS[k]); if (m && !m[1].toLowerCase().startsWith("http")) return m[1].trim(); } return ""; };
  let company = lab("company"), title = lab("title");
  if (!(company && title) && (PROMO.test(lines.slice(0, 3).join(" ")) || !JOBWORD.test(text))) return null;
  const head = lines[0];
  const m = head.match(/^([A-Z0-9][\w&.,'\- ]{1,50}?)\s+(?:is\s+)?(?:hiring|recruiting|off[\s-]?campus(?: drive)?|recruitment|careers?)\b[\s\S]*?[–|:\-]?\s*([^!\n|]{3,80})?$/i);
  if (m) { company = company || m[1].trim(); if (!title && m[2] && !/^(freshers?|apply|apply now|now)$/i.test(m[2].trim())) title = m[2].trim(); }
  if (!title) title = head.replace(/\b(apply now|apply link|hurry)\b.*$/i, "").replace(/^[\s\-–:|!]+|[\s\-–:|!]+$/g, "") || head;
  return { title: title.replace(/^[\s,\-–:|!]+|[\s,\-–:|!]+$/g, "").slice(0, 120), company: (company || "Unknown").slice(0, 80),
           experience: lab("experience") || "Not specified", location: lab("location"), url: apply[0] };
}

async function unshorten(b, url) {
  if (!SHORTENERS.includes(host(url))) return url;
  try { const r = await b.fetch(url, { headers: { "User-Agent": UA }, redirect: "follow" }); r.body?.cancel(); return r.url || url; }
  catch { return url; }
}

async function resolveBlog(b, url) {
  try {
    const r = await b.fetch(url, { headers: { "User-Agent": UA } });
    if (!r.ok) return null;
    if (!isBlog(r.url)) return { url: r.url, description: "" };
    const html = await r.text();
    let official = "", fallback = "";
    for (const m of html.matchAll(/<a\s[^>]*href="(https?:\/\/[^"]+)"[^>]*>([\s\S]*?)<\/a>/gi)) {
      const href = m[1], text = decode(m[2]);
      if (isBlog(href) || /t\.me\/|telegram|whatsapp|instagram|youtube|youtu\.be|facebook|twitter|x\.com\/|linkedin\.com\/(company|in)\/|play\.google|apps\.apple|#/i.test(href)) continue;
      if (/official (portal|page|site|link|website)|apply (on|at|via|for)\b.*official|^apply( here| now| link|\b)/i.test(text)) { official = href; break; }
      if (!fallback && /\bapply\b/i.test(text)) fallback = href;
    }
    const body = html.replace(/<(script|style|nav|footer|aside|header|form)[\s\S]*?<\/\1>/gi, " ");
    const paras = [...body.matchAll(/<(h2|h3|p|li|td)[^>]*>([\s\S]*?)<\/\1>/gi)].map((m) => decode(m[2])).filter(Boolean);
    return { url: official || fallback || url, description: paras.join("\n").slice(0, 5000) };
  } catch { return null; }
}

// Telegram every minute without parsing the page each time (the free plan allows ~10 ms CPU per run): look only
// at the newest post number; read the channel fully when it changed, and at least every 30 min anyway.
async function telegramChanged(env, b) {
  const st = await getState(env, "tg_quick", { last: {}, full: 0 });
  let changed = now() - (st.full || 0) > 30 * 60 * 1000;
  for (const raw of CONFIG.telegram_channels) {
    const name = raw.replace(/^(https?:\/\/)?(www\.)?(t|telegram)\.me\/(s\/)?/, "").replace(/^@/, "").split(/[/?]/)[0];
    if (!name) continue;
    const r = await b.fetch(`https://t.me/s/${name}`, { headers: { "User-Agent": UA } });
    if (!r.ok) continue;
    const html = await r.text();
    const i = html.lastIndexOf(`data-post="${name}/`);
    const newest = i < 0 ? 0 : parseInt(html.slice(i + name.length + 12, i + name.length + 24), 10) || 0;
    if (newest && newest !== (st.last || {})[name]) { changed = true; st.last = { ...(st.last || {}), [name]: newest }; }
  }
  st.checked = now();
  if (changed) st.full = now();
  await setState(env, "tg_quick", st);
  return changed;
}

async function srcTelegram(env, b) {
  const jobs = [];
  for (const raw of CONFIG.telegram_channels) {
    const name = raw.replace(/^(https?:\/\/)?(www\.)?(t|telegram)\.me\/(s\/)?/, "").replace(/^@/, "").split(/[/?]/)[0];
    if (!name) continue;
    const r = await b.fetch(`https://t.me/s/${name}`, { headers: { "User-Agent": UA } });
    if (!r.ok) continue;
    const html = await r.text();
    const posts = html.split('class="tgme_widget_message_wrap').slice(1);
    const parsed = [];
    for (const p of posts) {
      const post = (p.match(/data-post="([^"]+)"/) || [])[1];
      const body = (p.match(/<div class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/) || [])[1];
      if (!post || !body) continue;
      const when = (p.match(/<time[^>]*datetime="([^"]+)"/) || [])[1] || "";
      if (when && now() - Date.parse(when) > CONFIG.catchup_days * DAY) continue;
      const text = decode(body).normalize("NFKC");
      const links = [...new Set([...body.matchAll(/href="([^"]+)"/g)].map((m) => decode(m[1])).concat(text.match(/https?:\/\/\S+/g) || []))];
      const f = parsePost(text, links);
      if (f) parsed.push({ id: `tg:${post}`, source: "Telegram", ...f, posted: when, posted_days: daysAgo(when),
                           description: `${text}\n\n(Posted in Telegram channel @${name}: https://t.me/${post})`, name });
    }
    // only new posts get their links opened
    const ids = parsed.map(uidOf);
    const known = new Set();
    if (ids.length) {
      const rows = await env.DB.prepare(`SELECT id FROM seen WHERE id IN (${ids.map(() => "?").join(",")})`).bind(...ids).all();
      rows.results.forEach((x) => known.add(x.id));
    }
    for (const j of parsed.filter((x) => !known.has(uidOf(x))).slice(0, 6)) {
      const postUrl = await unshorten(b, j.url);
      if (AD_LINK.test(postUrl)) { jobs.push({ ...j, title: "(ad)", status: "ad" }); continue; }
      j.url = postUrl;
      if (isBlog(postUrl)) {
        const blog = await resolveBlog(b, postUrl);
        if (blog) {
          const official = await unshorten(b, blog.url || postUrl);
          if (AD_LINK.test(official)) { jobs.push({ ...j, title: "(ad)", status: "ad" }); continue; }
          j.blog_url = postUrl;
          j.url = official;
          if (blog.description) j.description = j.description.replace("\n\n(Posted in", `\n\nJob details:\n${blog.description}\n\n(Posted in`);
        }
      }
      jobs.push(j);
    }
  }
  // ads: remembered as seen (never opened again) but not stored as jobs
  const ads = jobs.filter((j) => j.status === "ad");
  if (ads.length) await env.DB.batch(ads.map((j) => env.DB.prepare("INSERT OR IGNORE INTO seen (id, at) VALUES (?, ?)").bind(uidOf(j), now())));
  return addJobs(env, jobs.filter((j) => j.status !== "ad"));
}

// LinkedIn guest search: one title x city per step, newest first, since that search was last read.
async function srcLinkedIn(env, b) {
  if (now() < (await getState(env, "li_backoff", 0))) return 0;
  const searches = CONFIG.li_searches;
  const i = (await getState(env, "li_i", 0)) % searches.length;
  const [q, place] = searches[i];
  const lastAll = await getState(env, "li_last", {});
  const key = `${q}@${place}`;
  const since = lastAll[key] ? Math.min(CONFIG.catchup_days * 86400, Math.max(900, Math.round((now() - lastAll[key]) / 1000) + 300))
                             : CONFIG.posted_within_days * 86400;
  const params = new URLSearchParams({ keywords: q, location: place === "Remote" ? "India" : place, f_TPR: `r${since}`, f_JT: "F", sortBy: "DD" });
  if (place === "Remote") params.set("f_WT", "2");
  const backoff = () => setState(env, "li_backoff", now() + 20 * 60 * 1000);
  // Results are newest first: read pages until one holds a job already seen (the rest is older).
  const cards = [];
  let blocked = false;
  for (let page = 0; page < 3 && b.left > 20; page++) {
    params.set("start", String(page * 10));
    const r = await b.fetch("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + params, { headers: { "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9" } });
    if (r.status === 429 || r.status === 999) { blocked = true; await backoff(); break; }
    const html = r.ok ? await r.text() : "";
    const found = [];
    for (const c of html.split(/<li>/).slice(1)) {
      const id = (c.match(/urn:li:jobPosting:(\d+)/) || [])[1];
      const title = decode((c.match(/base-search-card__title[^>]*>([\s\S]*?)<\/h3>/) || [])[1]);
      if (!id || !title) continue;
      found.push({ id, title, company: decode((c.match(/base-search-card__subtitle[^>]*>([\s\S]*?)<\/h4>/) || [])[1]) || "Unknown",
                   location: decode((c.match(/job-search-card__location[^>]*>([\s\S]*?)<\/span>/) || [])[1]),
                   posted: (c.match(/<time[^>]*datetime="([^"]+)"/) || [])[1] || "" });
    }
    const ids = found.map((c) => `LinkedIn:${c.id}`);
    const known = new Set();
    if (ids.length) (await env.DB.prepare(`SELECT id FROM seen WHERE id IN (${ids.map(() => "?").join(",")})`).bind(...ids).all()).results.forEach((x) => known.add(x.id));
    cards.push(...found.filter((c) => !known.has(`LinkedIn:${c.id}`) && !cards.some((x) => x.id === c.id)));
    if (found.length < 10 || known.size) break;
  }
  const jobs = [];
  let unfinished = blocked;  // some new jobs not opened yet: keep this search's window for next time
  for (const c of cards) {
    const job = { id: c.id, source: "LinkedIn", title: c.title, company: c.company,
                  location: place === "Remote" ? remoteLoc(c.location) : c.location, posted: c.posted,
                  posted_days: daysAgo(c.posted), url: `https://www.linkedin.com/jobs/view/${c.id}/`, experience: "Not specified", description: "" };
    if (titleExcluded(c.title)) { jobs.push(job); continue; }  // rejected by its title, never opened
    if (blocked || b.left <= 12) { unfinished = true; continue; }
    const d = await b.fetch(`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/${c.id}`, { headers: { "User-Agent": UA } });
    if (d.status === 429 || d.status === 999) { blocked = unfinished = true; await backoff(); continue; }
    if (!d.ok) { unfinished = true; continue; }
    const h = await d.text();
    job.description = decode((h.match(/show-more-less-html__markup[^>]*>([\s\S]*?)<\/div>/) || [])[1]);
    for (const it of h.matchAll(/description__job-criteria-subheader[^>]*>([\s\S]*?)<\/h3>[\s\S]*?description__job-criteria-text[^>]*>([\s\S]*?)<\/span>/g)) {
      const k = decode(it[1]).toLowerCase(), v = decode(it[2]);
      if (k.includes("seniority")) job.experience = v;
      if (k.includes("employment")) job.work_type = v;
    }
    jobs.push(job);
  }
  const added = await addJobs(env, jobs);
  if (!unfinished) {
    lastAll[key] = now();
    await setState(env, "li_last", lastAll);
    await setState(env, "li_i", i + 1);
  } else if (!blocked) await setState(env, "li_i", i + 1);  // next search; this one keeps its window
  return added;
}

// Company career pages (Greenhouse / Lever / Ashby / SmartRecruiters): 2 boards per step, India, last 3 days.
const INDIA = new RegExp(CONFIG.india_regex, "i");
async function srcCareers(env, b) {
  const pages = CONFIG.career_pages;
  let i = (await getState(env, "car_i", 0)) % pages.length;
  const jobs = [];
  for (let n = 0; n < 2; n++, i = (i + 1) % pages.length) {
    const [kind, token, name] = pages[i];
    try {
      if (kind === "greenhouse") {
        const d = await (await b.fetch(`https://boards-api.greenhouse.io/v1/boards/${token}/jobs?content=true`)).json();
        for (const j of d.jobs || []) {
          const loc = (j.location || {}).name || "";
          if (!INDIA.test(loc + " " + (j.offices || []).map((o) => o.location || o.name || "").join(" "))) continue;
          const posted = j.first_published || j.updated_at;
          jobs.push({ id: `gh:${name}:${j.id}`, source: "Careers", title: decode(j.title), company: name, location: loc, posted,
                      posted_days: daysAgo(posted), url: j.absolute_url, description: decode(decode(j.content || "")) });
        }
      } else if (kind === "lever") {
        const d = await (await b.fetch(`https://api.lever.co/v0/postings/${token}?mode=json`)).json();
        for (const j of Array.isArray(d) ? d : []) {
          const cat = j.categories || {};
          const loc = (cat.allLocations || [cat.location || ""]).join(", ");
          if (!(INDIA.test(loc) || j.country === "IN")) continue;
          const posted = j.createdAt ? new Date(j.createdAt).toISOString() : "";
          jobs.push({ id: `lv:${name}:${j.id}`, source: "Careers", title: j.text, company: name, location: loc, posted,
                      posted_days: daysAgo(posted), url: j.hostedUrl, work_type: cat.commitment || "",
                      description: [j.descriptionPlain, ...(j.lists || []).map((l) => `${l.text}: ${decode(l.content)}`), j.additionalPlain].filter(Boolean).join("\n") });
        }
      } else if (kind === "ashby") {
        const d = await (await b.fetch(`https://api.ashbyhq.com/posting-api/job-board/${token}`)).json();
        for (const j of d.jobs || []) {
          if (j.isListed === false) continue;
          const loc = [j.location, ...(j.secondaryLocations || []).map((s) => s.location)].filter(Boolean).join(", ");
          const country = ((j.address || {}).postalAddress || {}).addressCountry || "";
          if (!INDIA.test(loc + " " + country)) continue;
          jobs.push({ id: `ab:${name}:${j.id}`, source: "Careers", title: j.title, company: name, location: loc, posted: j.publishedAt,
                      posted_days: daysAgo(j.publishedAt), url: j.jobUrl, work_type: j.employmentType || "", description: j.descriptionPlain || decode(j.descriptionHtml) });
        }
      } else if (kind === "smartrecruiters") {
        const d = await (await b.fetch(`https://api.smartrecruiters.com/v1/companies/${token}/postings?country=in&limit=100`)).json();
        for (const j of d.content || []) {
          const loc = [((j.location || {}).city), "India"].filter(Boolean).join(", ");
          jobs.push({ id: `sr:${name}:${j.id}`, source: "Careers", title: j.name, company: name, location: loc, posted: j.releasedDate,
                      posted_days: daysAgo(j.releasedDate), url: `https://jobs.smartrecruiters.com/${token}/${j.id}`, description: "" });
        }
      }
    } catch { /* one board failing never stops the rest */ }
  }
  await setState(env, "car_i", i);
  return addJobs(env, jobs.filter((j) => j.posted_days === null || j.posted_days <= CONFIG.posted_within_days));
}

// Internshala jobs (not internships): one keyword's first page per step; new jobs get their detail page.
async function srcInternshala(env, b) {
  const qs = CONFIG.internshala_queries;
  if (!qs.length) return 0;
  const i = (await getState(env, "is_i", 0)) % qs.length;
  await setState(env, "is_i", i + 1);
  const slug = qs[i].toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  const r = await b.fetch(`https://internshala.com/jobs/keywords-${slug}/`, { headers: { "User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9" } });
  if (!r.ok) return 0;
  const html = await r.text();
  const cards = [];
  for (const part of html.split(/class="container-fluid individual_internship/).slice(1)) {
    const id = (part.match(/internshipId="(\d+)"/i) || [])[1];
    const type = (part.match(/employment_type="(\w+)"/) || [])[1] || "job";
    const href = (part.match(/data-href='([^']+)'/) || part.match(/job-title-href"[^>]*href="([^"]+)"/) || [])[1];
    const title = decode((part.match(/job-title-href"[^>]*>([\s\S]*?)<\/a>/) || [])[1]);
    if (!id || !href || !title || type !== "job") continue;
    const after = (cls) => decode((part.match(new RegExp(cls + '"[^>]*>[\\s\\S]*?<span[^>]*>([\\s\\S]*?)<\\/span>')) || [])[1]);
    const posted = decode((part.match(/color-labels"[^>]*>([\s\S]*?)<\/div>/) || [])[1]);
    const ago = posted.match(/(\d+)\s*(day|week|month)/i);
    cards.push({ id, source: "Internshala", title, url: "https://internshala.com" + href,
                 company: decode((part.match(/company-name"[^>]*>([\s\S]*?)<\/p>/) || [])[1]) || "Unknown",
                 location: [...((part.split(/class="row-1-item\s+locations"/)[1] || "").split("</p>")[0].matchAll(/<a[^>]*>([^<]+)<\/a>/g))].map((m) => m[1].trim()).join(", "),
                 experience: after("ic-16-briefcase") || "Not specified", salary: after("ic-16-money"), posted,
                 posted_days: /today|just now|hour/i.test(posted) ? 0 : ago ? +ago[1] * { day: 1, week: 7, month: 30 }[ago[2].toLowerCase()] : null,
                 work_type: "full time", description: "" });
  }
  const ids = cards.map(uidOf);
  const known = new Set();
  if (ids.length) (await env.DB.prepare(`SELECT id FROM seen WHERE id IN (${ids.map(() => "?").join(",")})`).bind(...ids).all()).results.forEach((x) => known.add(x.id));
  // the listing isn't sorted by date: only jobs from the catch-up window
  const fresh = cards.filter((c) => !known.has(uidOf(c)) && (c.posted_days === null || c.posted_days <= CONFIG.catchup_days));
  for (const c of fresh) {
    if (titleExcluded(c.title) || b.left <= 12) continue;
    const d = await b.fetch(c.url, { headers: { "User-Agent": UA } });
    if (d.ok) {
      const h = await d.text();
      c.description = decode((h.split('class="internship_details')[1] || "").split(/class="(?:activity_section|buttons_container)/)[0].replace(/^[^>]*>/, "")).slice(0, 6000);
    }
  }
  return addJobs(env, fresh.filter((c) => c.description || titleExcluded(c.title)));
}

// Wellfound public role pages (job data in __NEXT_DATA__): two pages per step; blocked = 3 h pause.
async function srcWellfound(env, b) {
  if (now() < (await getState(env, "wf_backoff", 0))) return 0;
  const pages = CONFIG.wellfound_pages;
  let i = (await getState(env, "wf_i", 0)) % pages.length;
  const jobs = [];
  for (let n = 0; n < 2; n++, i = (i + 1) % pages.length) {
    const r = await b.fetch("https://wellfound.com" + pages[i], { headers: { "User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9" } });
    if (r.status === 403 || r.status === 429) { await setState(env, "wf_backoff", now() + 3 * 3600 * 1000); break; }
    if (!r.ok) continue;
    const m = (await r.text()).match(/<script id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/);
    if (!m) continue;
    let data;
    try { const st = JSON.parse(m[1]).props.pageProps.apolloState || {}; data = st.data || st; } catch { continue; }
    const companyOf = {};
    for (const v of Object.values(data)) {
      if (v && v.__typename === "StartupResult") for (const ref of v.highlightedJobListings || []) companyOf[String(ref.__ref || "").split(":").pop()] = v.name || "Unknown";
    }
    for (const v of Object.values(data)) {
      if (!v || v.__typename !== "JobListingSearchResult" || !v.title) continue;
      const id = String(v.id || "");
      const lo = v.yearsExperienceMin, hi = v.yearsExperienceMax;
      const posted = typeof v.liveStartAt === "number" ? new Date(v.liveStartAt * 1000).toISOString() : "";
      jobs.push({ id, source: "Wellfound", title: decode(v.title), company: companyOf[id] || "Unknown",
                  location: [...new Set([...(v.locationNames || []), ...(v.remote ? ["Remote"] : [])])].join(", "),
                  experience: lo != null && hi != null ? `${lo}-${hi} years` : lo != null ? `${lo}+ years` : "Not specified",
                  posted, posted_days: daysAgo(posted), url: `https://wellfound.com/jobs/${id}-${v.slug || ""}`.replace(/-$/, ""),
                  description: decode(v.description || ""), work_type: String(v.jobType || "") });
    }
  }
  await setState(env, "wf_i", i);
  return addJobs(env, jobs.filter((j) => j.posted_days === null || j.posted_days <= CONFIG.catchup_days));
}

// ---------------------------------------------------------------- Apify (paid, capped)
//
// One manager for every paid source. The day's budget = credit left / days left in the cycle; each source
// gets a share and runs only when its turn is due. Spend is counted at the real cost of each run.
//  - Naukri hourly. Its "only new jobs" memory is saved only when a run FINISHES, so results are limited
//    (maxResults) to what the run's cap can pay: a run cut off by its cap re-bought the same ~450 jobs.
//  - LinkedIn per city every 3 h, only for what the free reading missed: skipJobId = every LinkedIn job
//    already seen in that window (never charged), excluded title words dropped before charging.
//  - Indeed every 8 h (no "skip known" option: each run re-buys the last 24 h).
const APIFY = "https://api.apify.com/v2";
const PRICE = { naukri: 0.0004, linkedin: 0.0004, indeed: 0.0001 };

const NAUKRI_EVERY_MS = 4 * 60 * 1000;  // runs start every ~4 min (Apify step every 2 min); ~$0.0015 per run, ~$0.6/day

function apifyJobsDue(st, env) {
  const due = [];
  const t = now(), last = st.last || {};
  // With the GitHub reader (GH_TOKEN) covering LinkedIn + Indeed for free, the whole Apify budget goes to Naukri,
  // every 30 min (measured Nov 5: ~200 new jobs/day x $0.0004 + $0.001 per run = ~$0.13/day of the ~$0.155 left).
  // Without it: Naukri hourly, LinkedIn per city every 3 h (only missed jobs), Indeed every 8 h.
  const reader = !!env.GH_TOKEN;
  for (const g of CONFIG.naukri.groups) {
    const key = `naukri:${g.name}`;
    const every = reader ? NAUKRI_EVERY_MS : 3600 * 1000;
    const runsPerDay = DAY / every;
    if (t - (last[key] || 0) >= every) due.push({ key, share: (reader ? 0.9 : 0.35) / runsPerDay / CONFIG.naukri.groups.length, group: g });
  }
  if (reader) return due;
  for (const place of CONFIG.linkedin_apify.places) {  // LinkedIn = the biggest source: each city every 3 h
    const key = `li:${place}`;
    if (t - (last[key] || 0) >= 3 * 3600 * 1000) due.push({ key, share: 0.45 / 8 / CONFIG.linkedin_apify.places.length, place });
  }
  if (t - (last.indeed || 0) >= 8 * 3600 * 1000) due.push({ key: "indeed", share: 0.2 / 3 });
  return due.sort((a, b) => (last[a.key] || 0) - (last[b.key] || 0));  // longest waiting first
}

const mapNaukri = (x) => ({
  id: String(x.jobId), source: "Naukri", title: decode(x.title), company: x.companyName || "Unknown", location: x.location || "",
  experience: String(x.experienceText || "Not specified").replace("Yrs", "years").replace("Yr", "year"), posted: x.createdDate || "",
  posted_days: daysAgo(x.createdDate), url: x.portalUrl || x.staticUrl || "", description: decode(x.description || x.shortDescription || ""),
  work_type: x.employmentType || x.jobType || "",
  // companyApplyJob true = "Apply on company site", false = Naukri's own apply (checked by hand, Nov 2026)
  apply_mode: x.companyApplyJob === true ? "site" : x.companyApplyJob === false ? "easy" : null });
const mapIndeed = (x) => {
  const attrs = Array.isArray(x.attributes) ? x.attributes : [];
  const posted = x.postedAt || x.datePosted || "";
  return { id: String(x.id || x.url || x.title), source: "Indeed", title: decode(x.title), company: x.companyName || x.company || "Unknown",
           location: x.location || "", experience: attrs.filter((a) => /year/i.test(String(a))).join(", ") || "Not specified",
           posted: String(posted), posted_days: daysAgo(posted), url: x.applyUrl || x.url || "",
           description: decode(x.descriptionText || x.description || ""), work_type: Array.isArray(x.jobTypes) ? x.jobTypes.join(", ") : "" };
};
// A "Remote" search's jobs keep their office city as location; mark them (free_boards.remote_location).
const remoteLoc = (loc) => /remote/i.test(loc || "") ? loc : loc ? `Remote, ${loc}` : "Remote";
const mapLinkedIn = (x) => {
  const posted = x.postedDate || x.publishedAt || "";
  return { id: String(x.id || x.jobUrl || x.title), source: "LinkedIn", title: decode(x.title), company: x.companyName || "Unknown",
           location: x.location || "", experience: x.experienceLevel || "Not specified", posted: String(posted || x.postedTime || ""),
           posted_days: posted ? daysAgo(posted) : null, url: x.jobUrl || x.url || x.applyUrl || "",
           description: decode(x.description || ""), work_type: x.contractType || x.workType || "" };
};

async function srcApify(env, b) {
  if (!env.APIFY_TOKEN) return 0;
  const auth = { headers: { Authorization: `Bearer ${env.APIFY_TOKEN}` } };
  const st = await getState(env, "ap", { runs: {}, last: {}, spend: {} });
  const today = new Date().toISOString().slice(0, 10);
  st.spend = { [today]: (st.spend || {})[today] || 0 };

  // 1) a run in progress: read its results (60 per step), then count its real cost
  const [key, run] = Object.entries(st.runs || {})[0] || [];
  if (key) {
    const info = (await (await b.fetch(`${APIFY}/actor-runs/${run.id}`, auth)).json()).data || {};
    if (["READY", "RUNNING"].includes(info.status) && now() - run.at < 20 * 60 * 1000) return 0;
    let added = 0, done = !info.defaultDatasetId;
    if (info.defaultDatasetId) {
      const items = await (await b.fetch(`${APIFY}/datasets/${info.defaultDatasetId}/items?clean=true&offset=${run.offset || 0}&limit=60`, auth)).json();
      const list = Array.isArray(items) ? items : [];
      const notTech = new RegExp(CONFIG.naukri_not_tech, "i");
      const jobs = key.startsWith("naukri") ? list.filter((x) => x.title && x.jobId && !notTech.test(x.functionalArea || "")).map(mapNaukri)
                 : key === "indeed" ? list.filter((x) => x.title).map(mapIndeed)
                 : list.filter((x) => x.title).map(mapLinkedIn).map((j) => key === "li:Remote" ? { ...j, location: remoteLoc(j.location) } : j);
      added = await addJobs(env, jobs);
      run.offset = (run.offset || 0) + list.length;
      done = list.length < 60;
    }
    if (done) {
      const cost = Number(info.usageTotalUsd) || run.cap;
      st.spend[today] = Math.max(0, (st.spend[today] || 0) - run.cap + cost);  // the reserve becomes the real cost
      delete st.runs[key];
    }
    await setState(env, "ap", st);
    return added;
  }

  // 2) start the source that has waited longest, if today's budget allows
  const lim = (await (await b.fetch(`${APIFY}/users/me/limits`, auth)).json()).data;
  const left = lim.limits.maxMonthlyUsageUsd - lim.current.monthlyUsageUsd;
  const daysLeft = Math.max(1, Math.ceil((Date.parse(lim.monthlyUsageCycle.endAt) - now()) / DAY));
  const daily = Math.max(0, left / daysLeft);
  for (const job of apifyJobsDue(st, env)) {
    // Each run may spend at least MIN_RUN (most cost far less: spend is counted at the real cost), never more
    // than what's left of today's budget.
    // what's left today; while the month has room a run may always spend its small minimum (a day that went
    // over must not stop Naukri / LinkedIn until tomorrow; spend is counted at the real cost, usually far less)
    const leftToday = Math.max(daily - (st.spend[today] || 0), left > 0.5 ? 0.006 : 0);
    const cap = Math.min(Math.max(0.006, daily * job.share * 1.5), leftToday);
    st.last[job.key] = now();
    if (cap < 0.003) continue;  // today's budget is used up: this source waits for its next turn
    const sinceH = Math.min(CONFIG.catchup_days * 24, (now() - ((st.prev || {})[job.key] || now() - CONFIG.posted_within_days * DAY)) / 3600000 + 1);
    let actor, input;
    if (job.key.startsWith("naukri")) {
      actor = CONFIG.naukri.actor;
      const days = Math.ceil(sinceH / 24);
      const g = job.group;
      const shift = Math.floor(now() / 1800000) % Math.max(1, g.terms.length);  // rotate: maxResults is shared, earlier terms go first
      input = { ...CONFIG.naukri.input, searchQueries: [...g.terms.slice(shift), ...g.terms.slice(0, shift)], freshness: days <= 1 ? 1 : days <= 3 ? 3 : 7, skipReposts: true,
                stateKey: CONFIG.naukri.input.stateKey + (g.name === "clean" ? "" : `-${g.name}`),
                maxResults: Math.max(20, Math.floor((cap - 0.002) / PRICE.naukri)) };
      if (g.category) input.roleCategory = g.category;
    } else if (job.key === "indeed") {
      actor = CONFIG.indeed.actor;
      input = { ...CONFIG.indeed.input, maxItems: Math.max(20, Math.floor((cap - 0.002) / PRICE.indeed)) };
    } else {
      actor = CONFIG.linkedin_apify.actor;
      const seconds = Math.round(sinceH * 3600);
      const rows = (await env.DB.prepare("SELECT id FROM seen WHERE id LIKE 'LinkedIn:%' AND at > ?").bind(now() - seconds * 1000 - DAY).all()).results;
      const loc = job.place === "Remote" ? "India" : job.place;
      input = { ...CONFIG.linkedin_apify.input, keywords: CONFIG.linkedin_apify.keywords, location: loc,
                skipJobId: rows.map((r) => r.id.slice("LinkedIn:".length)),
                urlParam: [...CONFIG.linkedin_apify.input.urlParam, { key: "f_TPR", value: `r${seconds}` },
                           ...(job.place === "Remote" ? [{ key: "f_WT", value: "2" }] : [])],
                limit: Math.max(10, Math.floor((cap - 0.002) / PRICE.linkedin)) };
    }
    const res = await b.fetch(`${APIFY}/acts/${actor.replace("/", "~")}/runs?maxTotalChargeUsd=${cap.toFixed(3)}&timeout=900`, {
      method: "POST", headers: { ...auth.headers, "Content-Type": "application/json" }, body: JSON.stringify(input) });
    const data = (await res.json()).data || {};
    if (data.id) {
      st.runs[job.key] = { id: data.id, cap, at: now(), offset: 0 };
      st.spend[today] = (st.spend[today] || 0) + cap;  // reserved until the real cost is known
      st.prev = { ...(st.prev || {}), [job.key]: now() };
    }
    break;
  }
  await setState(env, "ap", st);
  return 0;
}

// ---------------------------------------------------------------- AI check (Groq, your keys)

function condense(text, limit = 2800) {  // llm_filter._condense
  text = String(text || "").replace(/[ \t]+/g, " ").trim();
  if (text.length <= limit) return text;
  const headLen = Math.floor(limit / 4), budget = limit - headLen;
  const pri = /\d+\s*\+?\s*(?:-|–|to)?\s*\d*\s*\+?\s*(?:years?|yrs)|intern|stipend|unpaid|fresher|apprentice|contract|part[\s-]?time|freelance|employment type|job type/gi;
  const key = /years?|yrs|experience|fresher|graduate|batch|intern|stipend|unpaid|duration|contract|part[\s-]?time|full[\s-]?time|freelance|employment|job type|shift|voice process|calls?|senior|level/gi;
  const wins = [];
  for (const [re, rank] of [[pri, 0], [key, 1]]) { re.lastIndex = headLen; let m; while ((m = re.exec(text))) wins.push([m.index, m.index + m[0].length, rank]); }
  wins.sort((a, b) => a[2] - b[2] || a[0] - b[0]);
  const chosen = []; let used = 0;
  for (const [s0, e0] of wins) {
    const s = Math.max(headLen, s0 - 140), e = Math.min(text.length, e0 + 140);
    if (chosen.some(([cs, ce]) => s < ce && e > cs) || used + (e - s) + 3 > budget) continue;
    chosen.push([s, e]); used += e - s + 3;
  }
  chosen.sort((a, b) => a[0] - b[0]);
  return (text.slice(0, headLen) + chosen.map(([s, e]) => " … " + text.slice(s, e)).join("")).replace(/\s*\n\s*/g, " / ");
}

function jobBlock(n, j) {
  const meta = { title: j.title, company: j.company, site: j.source, "exp field": j.experience, type: j.work_type || null };
  const m = Object.entries(meta).filter(([, v]) => v && v !== "Not disclosed" && v !== "Not specified").map(([k, v]) => `${k}: ${v}`).join("; ");
  return `#${n} ${m}\n${condense(j.description) || "(no description)"}`;
}

// Groq key+model pairs that hit their daily limit (kept while this Worker instance lives): skipping them
// saves subrequests (12 failing tries per call otherwise, of ~45 per run).
const aiSkip = new Map();
// NVIDIA (build.nvidia.com, free, per-key limits): fallback after Groq. Tested on the laptop's 45 filter
// cases: nemotron-3-ultra 45/45 (thinking off), nemotron-3.5-lightning 45/45.
const NVIDIA_MODELS = ["nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3.5-lightning-30b-a3b"];

function parseItems(content, allowEmpty) {
  try {
    let text = String(content || "").trim();
    const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (fence) text = fence[1];
    const a = text.indexOf("{"), z = text.lastIndexOf("}");
    if (a !== -1 && z > a) text = text.slice(a, z + 1);
    const data = JSON.parse(text || "{}");
    const items = data.r || data.results;
    if (Array.isArray(items) && (items.length || allowEmpty)) return items;
  } catch { /* not JSON */ }
  return null;
}

async function groq(env, b, system, user, schema, name, allowEmpty = false, effort = "low") {
  const keys = String(env.GROQ_KEYS || "").split(",").map((k) => k.trim()).filter(Boolean);
  const start = Math.floor(now() / 60000) % Math.max(1, keys.length);
  const order = keys.slice(start).concat(keys.slice(0, start));
  for (const model of CONFIG.models) {
    for (const key of order) {
      const skipKey = key.slice(-6) + model;
      if ((aiSkip.get(skipKey) || 0) > now()) continue;
      if (b.left < 3) throw new Error("no subrequests left");
      const body = { model, temperature: 0, max_completion_tokens: 4000,
                     messages: [{ role: "system", content: system }, { role: "user", content: user }],
                     response_format: { type: "json_schema", json_schema: { name, strict: true, schema } } };
      if (model.startsWith("openai/gpt-oss")) body.reasoning_effort = effort;
      else { body.response_format = { type: "json_object" }; body.messages[1].content += "\n\nReply with JSON matching this schema:\n" + JSON.stringify(schema); }
      const r = await b.fetch("https://api.groq.com/openai/v1/chat/completions", {
        method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!r.ok) {  // per-minute / daily limit or bad key: the next key, then the next model
        if (r.status === 401 || r.status === 403) aiSkip.set(skipKey, now() + 30 * 60 * 1000);
        else if (r.status === 429 && /\(TPD\)|\(RPD\)|per day/.test(await r.text())) aiSkip.set(skipKey, now() + 30 * 60 * 1000);
        continue;
      }
      const items = parseItems((await r.json()).choices?.[0]?.message?.content, allowEmpty);
      if (items) return items;
    }
  }
  const nkeys = String(env.NVIDIA_KEYS || "").split(",").map((k) => k.trim()).filter(Boolean);
  const nstart = Math.floor(now() / 60000) % Math.max(1, nkeys.length);
  const norder = nkeys.slice(nstart).concat(nkeys.slice(0, nstart));
  for (const model of NVIDIA_MODELS) {
    for (const key of norder) {
      const skipKey = key.slice(-6) + model;
      if ((aiSkip.get(skipKey) || 0) > now()) continue;
      if (b.left < 3) throw new Error("no subrequests left");
      const body = { model, temperature: 0, max_tokens: 4000, response_format: { type: "json_object" },
                     chat_template_kwargs: { enable_thinking: false },
                     messages: [{ role: "system", content: system },
                                { role: "user", content: user + "\n\nReply with JSON matching this schema:\n" + JSON.stringify(schema) }] };
      let r;
      try {
        r = await b.fetch("https://integrate.api.nvidia.com/v1/chat/completions", {
          method: "POST", headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
          body: JSON.stringify(body), signal: AbortSignal.timeout(60000) });
      } catch { aiSkip.set(skipKey, now() + 10 * 60 * 1000); continue; }  // overloaded: skip it a while
      if (!r.ok) {
        aiSkip.set(skipKey, now() + (r.status === 429 ? 30 * 1000 : 30 * 60 * 1000));
        continue;
      }
      const items = parseItems((await r.json()).choices?.[0]?.message?.content, allowEmpty);
      if (items) return items;
    }
  }
  throw new Error("every AI key is busy or used up");
}

async function aiStep(env, b) {
  let rows = (await env.DB.prepare("SELECT * FROM jobs WHERE status = 'new' AND tries < 3 ORDER BY found_at DESC LIMIT 10").all()).results;
  if (!rows.length) return [];
  // 1) title screen (same as the laptop's prescreen): titles that alone show the job isn't for you
  try {
    const titles = rows.map((j, i) => `${i + 1}. ${j.title}`).join(String.fromCharCode(10));
    const screen = await groq(env, b, CONFIG.title_prompt, titles, CONFIG.title_schema, "title_screen", true);
    const drop = new Map(screen.filter((r) => r && r.drop && CONFIG.title_why[r.why] && rows[r.n - 1]).map((r) => [rows[r.n - 1].id, CONFIG.title_why[r.why]]));
    if (drop.size) {
      await env.DB.batch([...drop].map(([id, cat]) => env.DB.prepare("UPDATE jobs SET status = 'reject', category = ?, reason = 'Ruled out from the title' WHERE id = ?").bind(cat, id)));
      rows = rows.filter((j) => !drop.has(j.id));
      if (!rows.length) return [];
    }
  } catch { /* without the screen, the full check still runs */ }
  let facts;
  try {
    facts = await groq(env, b, CONFIG.facts_prompt, "One result per job; n = job number.\n\n" + rows.map((j, i) => jobBlock(i + 1, j)).join("\n\n"),
                       CONFIG.facts_schema, "job_facts");
  } catch (e) {
    await env.DB.batch(rows.map((j) => env.DB.prepare("UPDATE jobs SET tries = tries + 1 WHERE id = ?").bind(j.id)));
    return [];
  }
  const byN = Object.fromEntries(facts.filter((r) => r && r.n).map((r) => [r.n, r]));
  const verdicts = rows.map((j, i) => {
    const r = byN[i + 1];
    if (!r || !ROLE[r.role] || !EMP[r.emp]) return null;
    const f = { employment_type: EMP[r.emp], min_years_required: r.yrs, freshers_welcome: !!r.fresh, seniority: SEN[r.sen] || "unclear",
                role_type: ROLE[r.role], experience_evidence: r.ev || "", reason: r.why || "" };
    const [decision, category] = decide(f, j);
    return { f, decision, category };
  });
  const passing = rows.map((j, i) => [j, verdicts[i]]).filter(([, v]) => v && v.decision === "keep");
  const fits = {};
  if (passing.length) {
    try {
      const res = await groq(env, b, CONFIG.fit_prompt, "One result per job; n = job number.\n\n" + passing.map(([j], i) => jobBlock(i + 1, j)).join("\n\n"),
                             CONFIG.fit_schema, "skill_fit");
      for (const r of res) if (r && r.n && passing[r.n - 1]) fits[passing[r.n - 1][0].id] = r;
    } catch { /* fit check failed: those jobs stay "new" and are tried again */ }
    // Second look for fit rejections (the audit found most wrong rejections here): the full post, deeper reasoning.
    let looks = 0;
    for (const [j] of passing) {
      const ft = fits[j.id];
      if (!ft || (["strong", "partial"].includes(ft.fit) && ft.code !== false) || looks >= 2 || b.left < 8) continue;
      looks++;
      try {
        const res = await groq(env, b, CONFIG.fit_prompt + "\n\n" + CONFIG.second_look,
          "One result per job; n = job number.\n\n" + `#1 title: ${j.title}; company: ${j.company}\n` + condense(j.description, 5000),
          CONFIG.fit_schema, "skill_fit_recheck", false, "medium");
        const r = res.find((x) => x && x.fit);
        if (r && ["strong", "partial"].includes(r.fit) && r.code !== false) fits[j.id] = { ...r, n: ft.n, second_look: "kept" };
      } catch { /* keep the first answer */ }
    }
  }
  const stmts = [], kept = [];
  rows.forEach((j, i) => {
    const v = verdicts[i];
    if (!v) { stmts.push(env.DB.prepare("UPDATE jobs SET tries = tries + 1 WHERE id = ?").bind(j.id)); return; }
    let status = v.decision, category = v.category, reason = v.f.reason, fit = null;
    if (status === "keep") {
      const ft = fits[j.id];
      if (!ft) { stmts.push(env.DB.prepare("UPDATE jobs SET tries = tries + 1 WHERE id = ?").bind(j.id)); return; }
      fit = JSON.stringify(ft);
      if (ft.code === false) { status = "reject"; category = "non_developer"; reason = ft.why || reason; }
      else if (!["strong", "partial"].includes(ft.fit)) { status = "reject"; category = "off_stack"; reason = ft.why || reason; }
      else kept.push({ ...j, facts: v.f, fitv: ft });
    }
    stmts.push(env.DB.prepare("UPDATE jobs SET status = ?, category = ?, reason = ?, fit = ?, facts = ? WHERE id = ?")
      .bind(status, category, reason, fit, JSON.stringify(v.f), j.id));
  });
  if (stmts.length) await env.DB.batch(stmts);
  return kept;
}

// ---------------------------------------------------------------- Telegram

async function tg(env, b, method, params) {
  const r = await b.fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(params) });
  return r.json();
}

// Where you apply: "easy" = inside the board's own app (LinkedIn Easy Apply, Indeed Apply, Internshala,
// Wellfound: fine on mobile), "site" = the company's careers site (better on a laptop). Read from the job page
// for LinkedIn / Indeed; fixed per source otherwise. "" = unknown.
const MODE_BY_SOURCE = { Internshala: "easy", Wellfound: "easy", Careers: "site", Telegram: "site" };
function applyMode(j) { return j.apply_mode || MODE_BY_SOURCE[j.source] || ""; }
const MODE_LABEL = { easy: "📱 Easy Apply", site: "💻 Company site" };

function jobText(j) {  // same look as the laptop app's alerts
  const f = j.facts || {}, ft = j.fitv || {};
  const years = f.min_years_required;
  const exp = f.freshers_welcome ? "Freshers welcome" : typeof years === "number" && years ? `${years}+ yr` : years === 0 ? "0 yrs" : (j.experience || "");
  const d = j.posted_days;
  let posted = d === 0 ? "today" : d === 1 ? "1 day ago" : d != null ? `${d} days ago` : "";
  const p = String(j.posted || "");
  const ago = (m) => m < 2 ? "just now" : m < 60 ? `${Math.round(m)} min ago` : m < 2160 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
  if (p.includes("T") && !p.includes("T00:00:00") && !isNaN(Date.parse(p))) {  // a real time, not just a date
    posted = ago(Math.max(0, (Date.now() - Date.parse(p)) / 60000));
  } else if (j.within_min && j.found_at) {
    // LinkedIn / Indeed give only a date (UTC: "yesterday" until 05:30 IST), but the read only returned jobs
    // posted in the last within_min minutes: at most this old (Nov 5: "1 day ago" alerts were 0-20 min old)
    const m = (Date.now() - j.found_at) / 60000 + j.within_min;
    if (m < 36 * 60) posted = "< " + ago(m).replace(" ago", "") + " ago";
  }
  const lines = [`🆕 <b>${esc(j.title)}</b>`, `🏢 ${esc(j.company)}` + (j.location ? `  ·  📍 ${esc(j.location)}` : ""),
                 [exp && `🎓 ${esc(exp)}`, posted && `🕒 ${posted}`, `via ${esc(j.source)} ☁️`].filter(Boolean).join(" · ")];
  if (MODE_LABEL[applyMode(j)]) lines.push(MODE_LABEL[applyMode(j)]);
  if (ft.why || f.reason) lines.push(`✅ ${esc(ft.why || f.reason).slice(0, 300)}`);
  return lines.join("\n");
}

function buttons(uid, url, blogUrl, source, done) {
  const rows = [];
  if (/^https?:/.test(url || "")) {
    const row = [{ text: source === "Telegram" ? "Apply ↗" : `Open on ${source || "site"} ↗`, url }];
    if (/^https?:/.test(blogUrl || "") && blogUrl !== url) row.push({ text: "Post details ↗", url: blogUrl });
    rows.push(row);
  }
  rows.push(done ? [{ text: done, callback_data: "noop|" }] : [
    { text: "✅ Applied", callback_data: `a|${uid}`.slice(0, 64) }, { text: "👀 Seen", callback_data: `v|${uid}`.slice(0, 64) },
    { text: "🚫 Not for me", callback_data: `x|${uid}`.slice(0, 64) }]);
  return rows;
}

// One alert per real job: claim its loose keys first (laptop + Worker share this table).
// A job counts as "already shown" for 30 days: a repost within a month is skipped, one after that is
// usually a new opening and is alerted again.
const SHOWN_FOR = 30 * DAY;
async function claim(env, keys) {
  if (!keys.length) return true;
  const t = now();
  const held = await env.DB.prepare(`SELECT k FROM claims WHERE k IN (${keys.map(() => "?").join(",")}) AND at > ?`).bind(...keys, t - SHOWN_FOR).first();
  if (held) return false;
  await env.DB.batch(keys.map((k) => env.DB.prepare("INSERT INTO claims (k, at) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET at = excluded.at").bind(k, t)));
  return true;
}

async function sendAlert(env, b, { uid, text, url, blog_url, source, keys }) {
  const prior = await env.DB.prepare("SELECT uid FROM alerts WHERE uid = ?").bind(uid).first();
  if (prior) return false;
  if (!(await claim(env, keys || []))) {  // the same job (repost / other board) was already alerted or seen
    await env.DB.prepare("INSERT OR IGNORE INTO alerts (uid, text, url, blog_url, source, state, at) VALUES (?, ?, ?, ?, ?, 'dup', ?)")
      .bind(uid, text, url || "", blog_url || "", source || "", now()).run();
    return false;
  }
  if ((await getState(env, "alerts_off", false)) || !env.TELEGRAM_CHAT_ID) return false;
  const r = await tg(env, b, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true,
                                               reply_markup: { inline_keyboard: buttons(uid, url, blog_url, source) } });
  await env.DB.prepare("INSERT OR IGNORE INTO alerts (uid, text, url, blog_url, source, at) VALUES (?, ?, ?, ?, ?, ?)")
    .bind(uid, text, url || "", blog_url || "", source || "", now()).run();
  return !!r.ok;
}

async function onTelegram(env, update) {
  const b = new Budget(40);
  const q = update.callback_query;
  if (q) {
    if (String(q.message?.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return;
    const [action, uid] = String(q.data || "").split(/\|(.*)/s);
    const label = { a: "Marked as applied ✅", v: "Marked as seen 👀", x: "Hidden from new jobs 🚫" }[action];
    if (!label || !uid) { await tg(env, b, "answerCallbackQuery", { callback_query_id: q.id }); return; }
    await env.DB.batch([
      env.DB.prepare("INSERT INTO actions (uid, action, at) VALUES (?, ?, ?)").bind(uid, action, now()),
      env.DB.prepare("UPDATE alerts SET state = ? WHERE uid = ?").bind({ a: "applied", v: "seen", x: "hidden" }[action], uid),
    ]);
    await tg(env, b, "answerCallbackQuery", { callback_query_id: q.id, text: label });
    const al = await env.DB.prepare("SELECT * FROM alerts WHERE uid = ?").bind(uid).first();
    await tg(env, b, "editMessageReplyMarkup", { chat_id: q.message.chat.id, message_id: q.message.message_id,
      reply_markup: { inline_keyboard: buttons(uid, al?.url, al?.blog_url, al?.source, label) } });
    return;
  }
  const m = update.message;
  if (!m || String(m.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return;
  const text = String(m.text || "").trim();
  const say = (t) => tg(env, b, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: t, parse_mode: "HTML" });
  if (text.startsWith("/jobs") || text.startsWith("/easy") || text.startsWith("/site")) {
    // /easy = Easy Apply jobs (do them on the phone), /site = company-site jobs (do them on the laptop)
    const want = text.startsWith("/easy") ? "easy" : text.startsWith("/site") ? "site" : null;
    const all = (await env.DB.prepare(`SELECT a.*, j.apply_mode, j.source AS jsource FROM alerts a LEFT JOIN jobs j ON j.id = a.uid
      WHERE a.state IS NULL AND a.text != '' ORDER BY a.at DESC LIMIT 60`).all()).results;
    const rows = all.filter((r) => !want || applyMode({ apply_mode: r.apply_mode, source: r.jsource || r.source }) === want).slice(0, 5);
    if (!rows.length) return say("Nothing new right now. 🎉 New jobs come here as soon as they pass the AI check.");
    await say(`📋 <b>Newest job(s) you haven't marked:</b>`);
    for (const r of rows) await tg(env, b, "sendMessage", { chat_id: env.TELEGRAM_CHAT_ID, text: r.text, parse_mode: "HTML",
      disable_web_page_preview: true, reply_markup: { inline_keyboard: buttons(r.uid, r.url, r.blog_url, r.source) } });
  } else if (text.startsWith("/stop")) { await setState(env, "alerts_off", true); await say("🔕 Alerts paused. Send /resume to turn them back on."); }
  else if (text.startsWith("/resume") || text.startsWith("/start")) { await setState(env, "alerts_off", false); await say("🔔 Alerts are on."); }
  else if (text.startsWith("/status")) {  // where every job is right now (same as the dashboard's Pipeline tab)
    const p = await pipelineStatus(env);
    const ago = (m) => (m === null ? "never" : m < 60 ? `${m} min ago` : `${Math.round(m / 60)} h ago`);
    const lines = p.sources.map((x) => `${x.status === "ok" ? "✅" : "⚠️"} <b>${x.name}</b>: read ${ago(x.last_read_min)} ` +
      `(every ~${x.every_min} min) · ${x.found_1h} new in 1 h, ${x.approved_24h} approved in 24 h` +
      (x.note ? `\n    ${esc(x.note)}` : ""));
    const st = p.stages;
    await say(`📊 <b>Pipeline now</b>\n${lines.join("\n")}\n\n` +
      `<b>Last hour</b>: ${st.found_1h} new jobs → ${st.rule_rejected_1h} ruled out by rules (city / experience / title) → ` +
      `${st.ai_rejected_1h} rejected by the AI → ${st.approved_1h} approved → ${st.alerts_1h} alerts sent.\n` +
      `${st.waiting_ai ? "⏳" : "✅"} Waiting for the AI check: ${st.waiting_ai}` +
      (st.oldest_waiting_min !== null ? ` (oldest ${ago(st.oldest_waiting_min)})` : "") +
      (st.stuck_ai ? ` · ⚠️ ${st.stuck_ai} failed 3 times` : "") + "\n" +
      `🔔 Alerts: ${st.alerts_24h} in 24 h, last ${ago(st.last_alert_min)}${p.alerts_paused ? " · ⚠️ PAUSED (send /resume)" : ""}`);
  } else await say("Commands: /jobs (newest unmarked jobs), /easy (Easy Apply ones, for the phone), /site (company-site ones, " +
                   "for the laptop), /status, /stop (pause alerts), /resume.");
}

// ---------------------------------------------------------------- the minute tick

const STEPS = { telegram: srcTelegram, linkedin: srcLinkedIn, careers: srcCareers, internshala: srcInternshala,
                wellfound: srcWellfound, apify: srcApify };

// ---------------------------------------------------------------- pipeline status (dashboard + /status)

// Where every job is right now, per source and per stage, so a stall is visible at a glance.
async function pipelineStatus(env) {
  const t = now(), H = 3600 * 1000;
  const [ok, err, tg, gh, ap] = await Promise.all([getState(env, "src_ok", {}), getState(env, "src_err", {}),
    getState(env, "tg_quick", {}), getState(env, "gh_read", {}), getState(env, "ap", {})]);
  const per = (await env.DB.prepare(`SELECT source, SUM(found_at > ?) AS f1, COUNT(*) AS f24, SUM(status = 'keep') AS k24,
    MAX(found_at) AS last FROM jobs WHERE found_at > ? GROUP BY source`).bind(t - H, t - 24 * H).all()).results;
  const by = Object.fromEntries(per.map((r) => [r.source, r]));
  const mins = (ms) => (ms ? Math.round((t - ms) / 60000) : null);
  const naukriStart = Object.entries(ap.last || {}).filter(([k]) => k.startsWith("naukri")).map(([, v]) => v).sort().pop();
  const ghTimes = (keys) => Math.max(0, ...keys.map((k) => (gh[k] || 0) * 1000));
  // name, read by, expected minutes between reads, last good read (ms)
  const rows = [
    ["LinkedIn", "GitHub reader (2 machines)", 5, ghTimes(["linkedin_li0", "linkedin_li1", "linkedin"])],
    ["Indeed", "GitHub reader", 8, ghTimes(["indeed"])],
    ["Telegram", "cloud, checked every minute", 2, tg.checked || 0],
    ["Naukri", "cloud via Apify", 8, naukriStart || 0],
    ["Internshala", "cloud", 8, (ok.srcInternshala || {}).at || 0],
    ["Wellfound", "cloud", 16, (ok.srcWellfound || {}).at || 0],
    ["Careers", "cloud (company career pages)", 16, (ok.srcCareers || {}).at || 0],
  ];
  const srcErr = { Telegram: err.srcTelegram, Internshala: err.srcInternshala, Wellfound: err.srcWellfound, Careers: err.srcCareers, Naukri: err.srcApify };
  const sources = rows.map(([name, reader, every, last]) => {
    const age = mins(last), e = srcErr[name];
    const late = age === null || age > every * 2.5;
    return { name, reader, every_min: every, last_read_min: age, found_1h: (by[name] || {}).f1 || 0, found_24h: (by[name] || {}).f24 || 0,
             approved_24h: (by[name] || {}).k24 || 0, status: late ? "late" : "ok",
             note: e && (!last || e.at > last) ? `last error ${mins(e.at)} min ago: ${e.error}` : "" };
  });
  const s = await env.DB.prepare(`SELECT
      SUM(found_at > ?) AS found_1h,
      SUM(found_at > ? AND status = 'reject' AND reason LIKE 'Rule:%') AS rule_1h,
      SUM(status = 'new' AND tries < 3) AS waiting,
      MIN(CASE WHEN status = 'new' AND tries < 3 THEN found_at END) AS oldest_waiting,
      SUM(status = 'new' AND tries >= 3) AS stuck,
      SUM(found_at > ? AND status = 'keep') AS kept_1h,
      SUM(found_at > ? AND status = 'reject' AND (reason IS NULL OR reason NOT LIKE 'Rule:%')) AS ai_rejected_1h
    FROM jobs WHERE found_at > ?`).bind(t - H, t - H, t - H, t - H, t - 24 * H).first();
  const a = await env.DB.prepare(`SELECT SUM(at > ?) AS sent_1h, COUNT(*) AS sent_24h, MAX(at) AS last FROM alerts
    WHERE at > ? AND text != '' AND COALESCE(state, '') NOT IN ('old', 'dup')`).bind(t - H, t - 24 * H).first();
  return { now: t, sources, stages: { found_1h: s.found_1h || 0, rule_rejected_1h: s.rule_1h || 0, waiting_ai: s.waiting || 0,
           oldest_waiting_min: mins(s.oldest_waiting), stuck_ai: s.stuck || 0, approved_1h: s.kept_1h || 0,
           ai_rejected_1h: s.ai_rejected_1h || 0, alerts_1h: a.sent_1h || 0, alerts_24h: a.sent_24h || 0, last_alert_min: mins(a.last) },
           alerts_paused: !!(await getState(env, "alerts_off", false)), last_error: await getState(env, "last_error", null) };
}

const READER_REPO = CONFIG.reader_repo || "";  // FP_READER_REPO in .env (owner/repo of github_reader/)
const READER_EVERY_MS = 10 * 60 * 1000;

async function dispatchReader(env, b) {
  if (!READER_REPO) return;
  const last = await getState(env, "gh_dispatch", 0);
  if (now() - last < READER_EVERY_MS) return;
  await setState(env, "gh_dispatch", now());
  const r = await b.fetch(`https://api.github.com/repos/${READER_REPO}/actions/workflows/read.yml/dispatches`, {
    method: "POST", body: JSON.stringify({ ref: "main" }),
    headers: { Authorization: `Bearer ${env.GH_TOKEN}`, Accept: "application/vnd.github+json", "User-Agent": "fresherpilot-cloud",
               "X-GitHub-Api-Version": "2022-11-28" } });
  if (r.status !== 204) throw new Error(`GitHub dispatch HTTP ${r.status}: ${(await r.text()).slice(0, 120)}`);
}

async function tick(env, only = null) {
  const b = new Budget(45);
  // Nov 5 2026: the laptop can't stay on, so the cloud reads all the time (the laptop app only syncs, without
  // pausing this). LinkedIn here was mostly rate-limited (Cloudflare IPs): the GitHub reader does LinkedIn + Indeed.
  const laptopOnline = now() - (await getState(env, "laptop_seen", 0)) < LAPTOP_QUIET_MS;  // only an old always-on laptop
  let added = null;
  if (!laptopOnline || only) {
    const minute = Math.floor(now() / 60000);
    // 8-minute cycle: Apify every 2 min (start a run / read its results: Naukri every ~4 min), Internshala every
    // 4 min, Wellfound and career pages every 8 min. Telegram: a cheap "new post?" check every minute (below).
    const steps = [srcApify, srcInternshala, srcApify, srcWellfound, srcApify, srcInternshala, srcApify, srcCareers];
    let step = (only && STEPS[only]) || steps[minute % steps.length];
    if (!only && await telegramChanged(env, b)) step = srcTelegram;  // a new post: read it now (Apify waits a minute)
    try {
      added = await step(env, b);
      // per-source health for the dashboard's Pipeline tab and /status: when each source last read fine
      const ok = await getState(env, "src_ok", {});
      ok[step.name] = { at: now(), added: added || 0 };
      await setState(env, "src_ok", ok);
    } catch (e) {
      await setState(env, "last_error", `${step.name}: ${String(e).slice(0, 200)}`);
      const bad = await getState(env, "src_err", {});
      bad[step.name] = { at: now(), error: String(e).slice(0, 160) };
      await setState(env, "src_err", bad);
    }
  }
  // Laptop off: start the GitHub Actions reader (LinkedIn + Indeed) every 10 min. GitHub's own schedule is
  // unreliable (Nov 5: one run in 6.5 h for "*/10"); this cron fires every minute.
  if (!only && env.GH_TOKEN) await dispatchReader(env, b).catch(async (e) => setState(env, "last_error", `reader: ${String(e).slice(0, 200)}`));
  // AI check + alerts (also for jobs found before the laptop came back)
  const kept = await aiStep(env, b).catch(() => []);
  let sent = 0;
  for (const j of kept) {
    if (sent >= MAX_ALERTS_PER_RUN || b.left < 3) break;
    if (j.posted_days != null && j.posted_days > CONFIG.posted_within_days) continue;
    if (await sendAlert(env, b, { uid: j.id, text: jobText(j), url: j.url, blog_url: j.blog_url, source: j.source, keys: await looseKeys(j) })) sent++;
  }
  if (sent < MAX_ALERTS_PER_RUN && b.left > 6) {
    const missed = (await env.DB.prepare(`SELECT j.* FROM jobs j LEFT JOIN alerts a ON a.uid = j.id
      WHERE j.status = 'keep' AND a.uid IS NULL AND j.found_at > ? ORDER BY j.found_at DESC LIMIT 6`).bind(now() - DAY).all()).results;
    for (const j of missed) {
      if (sent >= MAX_ALERTS_PER_RUN || b.left < 3) break;
      if (j.posted_days != null && j.posted_days > CONFIG.posted_within_days) {
        await env.DB.prepare("INSERT OR IGNORE INTO alerts (uid, text, source, state, at) VALUES (?, '', ?, 'old', ?)").bind(j.id, j.source, now()).run();
        continue;
      }
      const job = { ...j, facts: JSON.parse(j.facts || "{}"), fitv: JSON.parse(j.fit || "{}") };
      if (await sendAlert(env, b, { uid: j.id, text: jobText(job), url: j.url, blog_url: j.blog_url, source: j.source, keys: await looseKeys(j) })) sent++;
    }
  }
  // tidy up: jobs older than 21 days
  if (Math.floor(now() / 60000) % 1440 === 0) await env.DB.prepare("DELETE FROM jobs WHERE found_at < ?").bind(now() - 21 * DAY).run();
  return { added, ai_kept: kept.length, alerts: sent, requests_left: b.left };
}

// ---------------------------------------------------------------- HTTP

const json = (d, s = 200) => new Response(JSON.stringify(d), { status: s, headers: { "Content-Type": "application/json" } });

export default {
  async scheduled(_event, env, ctx) { ctx.waitUntil(tick(env)); },

  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/api/health") return json({ ok: true });
    if (url.pathname === "/app" || url.pathname.startsWith("/app/")) return handleApp(req, env, url, setState, getState, pipelineStatus);  // password dashboard
    if (url.pathname === `/tg/${env.CLOUD_SECRET}` && req.method === "POST") {
      try { await onTelegram(env, await req.json()); } catch { /* never make Telegram retry */ }
      return json({ ok: true });
    }
    if (req.headers.get("X-FP-Secret") !== env.CLOUD_SECRET) return json({ detail: "forbidden" }, 403);
    if (url.pathname === "/api/heartbeat") {
      const hb = req.method === "POST" ? await req.json().catch(() => ({})) : {};
      if (!hb.passive) await setState(env, "laptop_seen", now());  // passive = a manual-mode laptop: don't pause the cloud
      // LinkedIn / Indeed jobs the laptop already read: the GitHub reader skips them
      const seen = (hb.seen || []).slice(0, 400);
      if (seen.length) {
        const t = now();
        await env.DB.batch(seen.map((id) => env.DB.prepare("INSERT INTO seen (id, at) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET at = excluded.at").bind(String(id), t)));
      }
      return json({ ok: true, seen: seen.length });
    }
    if (url.pathname === "/api/notify" && req.method === "POST") {  // the laptop's alerts go through here (one dedupe for both)
      const body = await req.json();
      return json({ sent: await sendAlert(env, new Budget(10), body) });
    }
    if (url.pathname === "/api/claim" && req.method === "POST") return json({ fresh: await claim(env, (await req.json()).keys || []) });
    if (url.pathname === "/api/actions") {
      const since = +(url.searchParams.get("since") || 0);
      return json({ actions: (await env.DB.prepare(`SELECT a.*, j.title, j.company, j.description FROM actions a
        LEFT JOIN jobs j ON j.id = a.uid WHERE a.n > ? ORDER BY a.n LIMIT 500`).bind(since).all()).results });
    }
    if (url.pathname === "/api/jobs") {  // jobs the cloud found (the laptop merges them when it's back)
      const since = +(url.searchParams.get("since") || 0);
      // only up to the oldest job the cloud's AI hasn't checked yet: the laptop gets each job once, with its verdict
      const pend = await env.DB.prepare("SELECT MIN(found_at) AS m FROM jobs WHERE status = 'new' AND tries < 3").first();
      const until = pend && pend.m ? pend.m : now() + 1;
      const rows = (await env.DB.prepare("SELECT id, source, title, company, location, experience, url, blog_url, posted, posted_days, description, work_type, status, category, reason, fit, facts, found_at, apply_mode FROM jobs WHERE found_at > ? AND found_at < ? ORDER BY found_at LIMIT 300").bind(since, until).all()).results;
      return json({ jobs: rows });
    }
    if (url.pathname === "/api/status") {
      const c = await env.DB.prepare("SELECT COUNT(*) AS n, SUM(status='keep') AS k, SUM(status='new') AS p, SUM(status='reject') AS r FROM jobs").first();
      return json({ laptop_seen: await getState(env, "laptop_seen", 0), jobs: c, last_error: await getState(env, "last_error", null),
                    li_backoff: await getState(env, "li_backoff", 0), naukri: await getState(env, "nk", {}) });
    }
    if (url.pathname === "/api/tick" && req.method === "POST") return json(await tick(env, url.searchParams.get("step")));  // manual run (testing)
    // ---- GitHub Actions reader (github_reader/): reads LinkedIn + Indeed while the laptop is off (Cloudflare's
    // IPs are rate-limited there; GitHub's runners measured 31/32 LinkedIn searches OK, Indeed 360/360, Nov 5).
    if (url.pathname === "/api/reader") {
      const laptopSeen = await getState(env, "laptop_seen", 0);
      const quiet = now() - laptopSeen >= LAPTOP_QUIET_MS || url.searchParams.get("force") === "1";  // force = testing
      const read = await getState(env, "gh_read", {});
      let known = [];
      if (quiet) {
        const rows = (await env.DB.prepare("SELECT id FROM seen WHERE at > ? AND (id LIKE 'LinkedIn:%' OR id LIKE 'Indeed:%')")
          .bind(now() - 21 * DAY).all()).results;  // = KEEP_JOBS_DAYS
        known = rows.map((r) => r.id);
      }
      return json({ quiet, laptop_seen: laptopSeen, read, known, li_searches: CONFIG.li_searches,
                    indeed_roles: CONFIG.indeed_roles || [], places: CONFIG.places || [],
                    posted_within_days: CONFIG.posted_within_days });
    }
    if (url.pathname === "/api/reader/dispatch" && req.method === "POST") {  // test: start the GitHub reader now
      if (!env.GH_TOKEN) return json({ ok: false, error: "GH_TOKEN secret missing" });
      await setState(env, "gh_dispatch", 0);
      try { await dispatchReader(env, new Budget(5)); return json({ ok: true }); }
      catch (e) { return json({ ok: false, error: String(e).slice(0, 300) }); }
    }
    if (url.pathname === "/api/titles" && req.method === "POST") {  // the laptop's free title rules
      const titles = ((await req.json()).titles || []).slice(0, 500);
      return json({ keep: titles.map((t) => !titleExcluded(String(t || ""))) });
    }
    if (url.pathname === "/api/ingest" && req.method === "POST") {
      const body = await req.json();
      const jobs = (body.jobs || []).filter((j) => j && j.id && j.source && j.title).slice(0, 300);
      const added = await addJobs(env, jobs);
      if (body.read) await setState(env, "gh_read", { ...(await getState(env, "gh_read", {})), ...body.read });
      return json({ added });
    }
    return json({ detail: "not found" }, 404);
  },
};
export { looseKeys, decide, titleExcluded, boardReject, parsePost, condense };
