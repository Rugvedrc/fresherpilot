"""What to search for on each job board. Edit this file to change coverage.

Searches are specific to the candidate's stack (AI / ML / GenAI / LLM / RAG / data science /
Python / backend / software developer), only jobs posted in the last 3 days, fresher / entry
level where the board supports it, and no result caps: whatever matches comes in. The AI filter
and skill-fit check then decide what is shown.
"""

import json
from urllib.parse import quote_plus

# Merged near-duplicate searches (measured: overlapping searches made Indeed return 37% duplicates
# and Naukri 11%). "ml engineer" is covered by machine learning engineer, "fastapi/django developer"
# by python developer, "software developer" by software engineer, "rag engineer" by llm engineer.
ROLES = [
    "ai engineer", "machine learning engineer", "generative ai engineer", "llm engineer", "nlp engineer",
    "data scientist", "python developer", "python backend developer", "backend developer", "software engineer",
    # fresher-titled roles (Naukri / Indeed list many jobs only under these)
    "software developer", "associate software engineer", "graduate engineer trainee", "ai ml engineer",
]
ALL_ROLES = ROLES

# How long a job stays in the list after it was first fetched.
KEEP_JOBS_DAYS = 21
POSTED_WITHIN_DAYS = 3
# After the app was off for a while, boards are read back to the last read (at most this many days),
# so nothing posted meanwhile is lost.
CATCHUP_DAYS = 7
# Wellfound charges ~$0.06 just to start a run: run it at most once per this many hours.
WELLFOUND_MIN_GAP_HOURS = 8  # Wellfound runs with the first fetch of each day (start fee per run)

# Safety caps per run (USD). Not result limits: a cap only stops a run that would cost more than this.
# The Apify free plan has $5/month, so these are set to stop a single run eating most of it.

# Naukri: blackfalcondata/naukri-jobs-feed (~$0.0004/job). Naukri's own department filter keeps only
# Engineering - Software & QA (5) and Data Science & Analytics (3): in a test 88% of results were in
# those departments instead of the doctors / teachers / BPO jobs plain keyword search returned.
NAUKRI = {
    "actor": "blackfalcondata/naukri-jobs-feed",
    "input": {
        "searchQueries": ROLES,
        "experience": "0",          # jobs whose range includes 0 years (see NAUKRI_EXPERIENCE_PASSES)
        "departmentIds": ["5", "3"],
        "freshness": POSTED_WITHIN_DAYS,
        "sortBy": "date",
        "fetchDetails": True,       # full description + department
        "maxResults": 0,            # no cap
        "descriptionFormat": "text",
        "excludeEmptyFields": True,
        "incremental": True,        # only new / changed jobs after the first run
        "stateKey": "jobfinder-naukri",
    },
    "max_charge_usd": 0.6,
}
# Naukri's experience filter takes one value: "0" = ranges that include 0 years, which never returns
# "1-3 years" jobs (that's how a Data Scientist call like Vividnex's was missed). A second pass with "1"
# brings those in. Measured (Oct 2026): experienceMin/experienceMax only apply the minimum, so there is
# no single-run way; each pass keeps its own "only new jobs" state (a shared one isn't shared across
# experience values and could be overwritten). Jobs in both passes (e.g. "0-2 years") are paid twice
# ($0.00039 each), about +50% on the second pass. The "1" pass only runs when the user wants 1-year
# "stretch" jobs (prefs.stretch_jobs); see naukri_passes().
NAUKRI_EXPERIENCE_PASSES = ["0", "1"]

# What we pay Apify for (measured Nov 2026, unique jobs per search term -> approved): machine learning engineer
# 81 -> 15, python developer 272 -> 11, ai engineer 34 -> 10, data scientist 119 -> 6, llm engineer 27 -> 4,
# software engineer 30 -> 4 ... (under 1 cent per approved job), but graduate engineer trainee 183 -> 0 (manufacturing,
# sales, service jobs) and generative ai engineer 17 -> 0 (its good jobs come via "ai engineer" / "llm engineer").
# Those two are not sent to Naukri. The actor ignores departmentIds / roleCategoryIds; its roleCategory post-filter
# works but takes one category and dropped 7 of 26 approved jobs in a back-test (QA-category and uncategorised GenAI
# jobs), so it isn't used.
NAUKRI_SKIP_TERMS = {"graduate engineer trainee", "generative ai engineer"}


def naukri_groups(roles: list[str]) -> list[tuple[str, list[str], str | None]]:
    """[(group name, search terms, roleCategory or None)] for the Naukri runs (one group: no category filter)."""
    terms = [r for r in roles if r.lower() not in NAUKRI_SKIP_TERMS]
    return [("clean", terms, None)] if terms else []


def naukri_passes(prefs: dict) -> list[str]:
    """Naukri experience passes for this user: "1" (the 1-3 yrs jobs) only when stretch jobs are wanted."""
    return list(NAUKRI_EXPERIENCE_PASSES) if prefs.get("stretch_jobs") else [p for p in NAUKRI_EXPERIENCE_PASSES if p == "0"] or ["0"]

# LinkedIn search links posted in the last 3 days (f_TPR in seconds; the actor's own time filter only
# offers 1, 7 or 30 days) and full-time only (f_JT=F). Tested: LinkedIn's entry-level filter (f_E) makes
# this actor return nothing, so experience is left to the AI filter.
_LI_SECONDS = POSTED_WITHIN_DAYS * 24 * 3600
# Most valuable first: searches run in this order while the daily budget lasts. Each title is its
# own search: one combined "A OR B" search returned only ~300 jobs a day vs ~585 for "AI Engineer" alone.
LINKEDIN_TITLES = [
    "AI Engineer", "Machine Learning Engineer", "Data Scientist",
    "Python Developer", "Backend Developer", "Software Engineer",
]
# Measured (Nov 2026, 24 h, India): the GenAI one finds ~120 jobs (56 pass the title rules), the
# fresher one ~70 (42) with titles the plain searches miss ("Associate Data Scientist", "FDE", ...).
LINKEDIN_QUERIES = [
    '"Generative AI" OR GenAI OR LLM OR RAG OR "Agentic AI"',
    '(fresher OR freshers OR "entry level" OR "graduate trainee" OR "2026 batch" OR "0-1 years") AND '
    '(python OR software OR developer OR "machine learning" OR AI OR "data science")',
]
# Title words that never fit (checked on ~4,800 judged jobs: they removed ~50% of them, and only
# 2 of those had been approved). Used twice: LinkedIn drops them before you pay, and every board's
# jobs with them are rejected before the AI check (no tokens). Words are matched inside the title,
# so keep the spaces ("lead " doesn't hit "leading"). Stack words are ignored when the title also
# says Python / AI / ML ("Java, Python, AWS developer" stays).
# Learned from the owner's ~7,500 judged jobs (Nov 2026): title words with 0 approvals in 40+ AI-checked jobs.
# Back-tested on all history: they drop ~530 jobs before the AI and would have lost 0 approved ones.
# "Soft" words are ignored when the title also names an own-stack word (Python/AI/ML/data...):
# "Junior Machine Learning Executive" still goes to the AI. "architect" = the word only (not "architecture").
TITLE_EXCLUDE_SOFT = {"non_developer": ["executive", "operations", "process ", "management"]}
TITLE_SOFT_SKIP = r"engineer|developer|programmer|scientist|data|sde"  # soft words never drop these titles
TITLE_ARCHITECT = r"(?<![a-z])architects?(?![a-z])"
# "AI Engineer (3-5 yrs)": 2+ years in the title (1-year titles go to the AI: some welcome freshers).
TITLE_YEARS = r"(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:(?:-|–|to)\s*\d+\s*)?(?:yrs|years|year|yr)\b"
TITLE_YEARS_MIN = 2

TITLE_EXCLUDE = {
    "too_senior": ["senior", "sr.", "sr ", "lead ", "team lead", "tech lead", "principal", "staff engineer", "staff software",
                   "staff data", "staff ml", "manager", "director", "head ", "head of", "vp ", "vice president", " ii ", " iii ",
                   " iv ", "sde 2", "sde-2", "sde2", "sde 3", "sde-3", "sde3", "staff ai", "staff machine", "staff backend",
                   "staff python", "smts", "lmts", "pmts"],
    "internship": ["intern ", "internship", "apprentice"],
    "off_stack": ["java ", "java/", "java-", ".net", "dotnet", "c#", "php", "laravel", "salesforce", "sap ", "servicenow",
                  "android", "ios ", "flutter", "angular", "vue", "frontend", "front end", "front-end", "ruby", "golang",
                  "mainframe", "cobol", "wordpress", "shopify"],
    "non_developer": ["sales", "marketing", "recruiter", "talent acquisition", "accountant", "teacher", "faculty", "tutor",
                      "nurse", "doctor", "customer", "voice process", "international voice", "telecaller", "tele caller", "bpo",
                      "counsel", "business development", "data entry", "content writer", "hr executive", "hr generalist",
                      "hr manager", "audit", "legal", "support engineer", "technical support", "helpdesk", "desktop support"],
    "non_software_engineering": ["mechanical", "civil engineer", "electrical"],
}

# LinkedIn: valig/linkedin-jobs-scraper ($0.0004 per job, vs $0.0015 for the old one, which used 84%
# of the monthly Apify budget). One search for all titles (LinkedIn's OR syntax), full-time, India,
# and the excluded title words are dropped before they are charged.
LINKEDIN = {
    "actor": "valig/linkedin-jobs-scraper",
    "input": {
        "location": "India",
        # newest first, so a run stopped by its spending cap keeps the newest jobs
        "urlParam": [{"key": "f_JT", "value": "F"}, {"key": "f_TPR", "value": f"r{_LI_SECONDS}"}, {"key": "sortBy", "value": "DD"}],
        "titleExclude": sorted({w.strip() for words in TITLE_EXCLUDE.values() for w in words
                                if len(w.strip()) > 3 and w.strip() not in ("java", "head", "intern")}),
        "limit": 1000,
    },
    "max_charge_usd": 0.4,  # per search
}

INDEED = {
    "actor": "deltaspider/indeed-scraper",
    # Last 3 days, full-time, sorted by relevance. Tested: Indeed India ignores its entry-level URL filter,
    # and newest-first sorting returns unrelated senior roles; "<role> fresher" queries are on target.
    "search_url": "https://in.indeed.com/jobs?q={query}&l=India&fromage=" + str(POSTED_WITHIN_DAYS) + "&jt=fulltime",
    # Plain titles: "<role> fresher" searches missed ~95% of jobs (33 vs 571 in a test), including
    # "Associate Software Engineer" / "Software Trainee Engineer". Seniors are dropped by title, free.
    "queries": list(ROLES),
    "input": {"country": "IN", "maxItems": 1000},
    "max_charge_usd": 0.5,
}

WELLFOUND = {
    "actor": "abotapi/wellfound-jobs-scraper",
    "input": {
        "mode": "search",
        "locations": ["bangalore", "hyderabad", "pune", "chennai", "mumbai", "delhi", "gurgaon", "noida", "kolkata", "ahmedabad"],
        "roleKeywords": ["engineer", "developer", "scientist"],
        "keywords": ["python", "machine learning", "ml", "llm", "genai", "generative ai", "rag", "nlp", "data scientist",
                     "data science", "backend", "ai engineer", "artificial intelligence", "fastapi", "django"],
        "jobTypes": ["full-time"],
        "experienceLevel": "entry",  # up to 1 year required
        "includeNoExperience": True,
        "postedWithinDays": POSTED_WITHIN_DAYS,
        "maxItems": 500,
        "sortBy": "newest",
        "incrementalMode": True,  # later runs return only new / updated jobs
    },
    "max_charge_usd": 0.5,
}


# Google Jobs (litescrape/google-jobs, $0.25 per 1,000 searches, no start fee): a catch-all that finds
# jobs on sites we don't search (Glassdoor, Shine, company career sites...). Results have no full
# description. Apify's free plan allows 25 searches/day for this actor, so: one search per role,
# at most GOOGLE_RUNS_PER_DAY fetches a day, GOOGLE_MIN_GAP_HOURS apart.
GOOGLE_JOBS = {
    "actor": "litescrape/google-jobs",
    "input": {"gl": "in", "hl": "en", "maxPagesPerInput": 1, "maxConcurrency": 2, "datasetOutput": True},
    "max_charge_usd": 0.02,
}
GOOGLE_RUNS_PER_DAY = 2
GOOGLE_MIN_GAP_HOURS = 6


# Internshala's full-time "fresher jobs" section (not internships). Incremental mode: after the first
# run only new / updated listings are returned (and charged, ~$0.0004 each).
INTERNSHALA = {
    "actor": "blackfalcondata/internshala-scraper",
    "input": {
        "listingType": "job",
        "query": json.dumps(["python", "machine learning", "data science", "artificial intelligence",
                             "generative ai", "backend development", "software development"]),
        "country": "IN",
        "maxResults": 0,
        "maxPages": 3,
        "includeDetails": True,
        "descriptionFormat": "text",
        "excludeEmptyFields": True,
        "incrementalMode": True,
        "stateKey": "jobfinder-fresher-jobs",
    },
    "max_charge_usd": 0.3,
}

# Daily Apify budget: the credit left this billing cycle / days left (so the free $5 lasts the
# month), shared by all fetches of one day. Set APIFY_USD_PER_DAY to a number to fix it instead.
# Each board gets a share; every search is sorted newest first, so a board that reaches its share
# has already kept the newest jobs. Full coverage of all boards costs roughly $1/day.
APIFY_USD_PER_DAY: float | None = None
# LinkedIn and Indeed are fetched free first (free_boards.py) and use Apify only if that fails, so
# Naukri (no free source) gets most of the credit.
# Live poller (jobs/live_poller.py): these boards are read free, all the time, in the background,
# instead of in the scheduled fetch. LinkedIn takes turns through every title x city search (~1
# request / 2.5 s, so a new post shows up in 1-2 minutes); the others every LIVE_EVERY_MIN minutes.
LIVE_POLLER = True
# Nov 5 2026: the laptop can't stay on, so the 24/7 readers are the cloud (Telegram, Naukri on Apify, Internshala,
# Wellfound, career pages) and the GitHub reader (LinkedIn, Indeed). LIVE_AUTO = False: the laptop app reads only
# when you press "Fetch new jobs" (run_once), and while it's open it pulls everything the cloud found, with the
# cloud's AI verdicts (telegram_bot.cloud_sync). True = the old always-on laptop readers.
LIVE_AUTO = False
MANUAL_LI_HOURS = 2  # a manual LinkedIn round reads this far back (the cloud covers everything before)
LIVE_BOARDS = {"linkedin", "indeed", "internshala", "wellfound", "telegram", "careers"}
# Goal: every new job in front of you within 5-10 minutes (openings like IBM's close in ~30 min). LinkedIn runs
# all the time (a full round of searches takes ~2-4 min); the others every few minutes. Telegram = one cheap
# page per channel. Careers = the company pages (Greenhouse/Lever/Ashby/SmartRecruiters APIs, free).
LIVE_EVERY_MIN = {"indeed": 10, "internshala": 10, "wellfound": 15, "telegram": 2, "careers": 10}
LIVE_CHECK_MIN = 2  # new jobs wait at most this long (or until 10 pile up) before the AI check runs
# Boards that may use Apify credit ($5/month). Naukri has no free route that works without a browser
# (the extension sweep is the free backup). Add "google", "internshala", ... here to turn them back on.
APIFY_BOARDS = {"naukri"}
SCHEDULED_FETCH_HOURS = 0.5  # the regular fetch (Naukri on Apify) runs this often. Nov 5: 30 min fits the
# ~$0.155/day left ($0.001 per run + ~200 new jobs/day x $0.0004); LinkedIn/Indeed on Apify are off (GitHub reader).
# (Naukri is "only new jobs" + pay per job, so hourly costs about the same as 2-hourly; the daily cap holds)
# LinkedIn free only: searches the free route can't finish (rate limit / time) run first on the next
# fetch instead of going to Apify. Set False to let Apify finish them (costs credit).
LINKEDIN_FREE_ONLY = True
LINKEDIN_FREE_MINUTES = 25  # time per fetch for the free LinkedIn search (~1 request / 2.5 s)
BUDGET_SHARE = {"naukri": 0.95, "internshala": 0.12, "wellfound": 0.08, "linkedin": 0.10, "indeed": 0.05, "google": 0.02}
MIN_RUN_USD = 0.02

# ---------------------------------------------------------------- company career pages (free)
# Public job boards of companies (Greenhouse / Lever / Ashby). Only India-based openings are used.
# Add a company: ("greenhouse" | "lever" | "ashby" | "smartrecruiters", "<board name in its jobs URL>", "<Company>").
# e.g. jobs.lever.co/cred -> ("lever", "cred", "CRED"); jobs.ashbyhq.com/sarvam -> ("ashby", "sarvam", "Sarvam AI")
CAREERS = {"runner": "careers"}

# Public Telegram job channels (free, read from the t.me/s/<channel> web preview). Each user lists
# their own in Settings; private channels can't be read this way.
TELEGRAM = {"runner": "telegram"}
TELEGRAM_FIRST_DAYS = 3
CAREERS_FIRST_DAYS = 21  # the first fetch takes openings from the last 3 weeks; later fetches only new ones
CAREER_PAGES = [
    # AI-first
    ("ashby", "sarvam", "Sarvam AI"),
    ("ashby", "composio", "Composio"),
    ("ashby", "atlan", "Atlan"),
    ("greenhouse", "observeai", "Observe.AI"),
    ("lever", "netomi", "Netomi"),
    ("greenhouse", "sigmoid", "Sigmoid"),
    ("greenhouse", "turing", "Turing"),
    ("ashby", "openai", "OpenAI"),
    ("greenhouse", "anthropic", "Anthropic"),
    ("greenhouse", "scaleai", "Scale AI"),
    ("ashby", "writer", "Writer"),
    ("greenhouse", "databricks", "Databricks"),
    ("ashby", "snowflake", "Snowflake"),
    # Indian product companies
    ("greenhouse", "groww", "Groww"),
    ("lever", "cred", "CRED"),
    ("lever", "meesho", "Meesho"),
    ("lever", "zeta", "Zeta"),
    ("lever", "paytm", "Paytm"),
    ("greenhouse", "inmobi", "InMobi"),
    ("greenhouse", "highradius", "HighRadius"),
    ("lever", "mindtickle", "Mindtickle"),
    ("lever", "hevodata", "Hevo Data"),
    ("greenhouse", "druva", "Druva"),
    ("greenhouse", "hackerrank", "HackerRank"),
    ("lever", "pocketfm", "Pocket FM"),
    ("greenhouse", "zetaglobal", "Zeta Global"),
    # Global companies with India teams
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "elastic", "Elastic"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "airbnb", "Airbnb"),
    ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "figma", "Figma"),
    ("ashby", "notion", "Notion"),
    # SmartRecruiters (official Posting API; e.g. jobs.smartrecruiters.com/BoschGroup -> "BoschGroup")
    ("smartrecruiters", "BoschGroup", "Bosch"),
    ("smartrecruiters", "ServiceNow", "ServiceNow"),
    ("smartrecruiters", "NielsenIQ", "NielsenIQ"),
    ("smartrecruiters", "Freshworks", "Freshworks"),
    ("smartrecruiters", "Experian", "Experian"),
    ("smartrecruiters", "Continental", "Continental"),
    ("smartrecruiters", "WesternDigital", "Western Digital"),
    ("smartrecruiters", "Ubisoft2", "Ubisoft"),
]


# ---------------------------------------------------------------- per-user search preferences
# Defaults = the original owner's setup. Each user's own values (made from their resume during
# onboarding, editable in Settings) are stored in users/<id>/search_prefs.json and override these.
DEFAULT_PREFS = {
    "onboarded": True,
    "headline": "2026 B.E. Information Technology graduate",
    "summary": "Python developer: AI/ML, GenAI (LLMs, RAG, agents), data science, Python backend/REST APIs "
               "(FastAPI/Flask/Django), Docker, AWS/Azure.",
    "target_area": "AI/ML, GenAI/LLM, data science, Python or backend development",
    "experience_years": 0,
    "reject_from_years": 1,           # jobs needing this many years or more are rejected
    # Jobs asking for exactly one year more than that ("1-3 yrs", "1+") as "stretch" jobs. Off: rejected
    # like other experienced jobs (freshers get far fewer calls from them).
    "stretch_jobs": False,
    "roles": ROLES,                   # Naukri / Indeed searches
    "linkedin_titles": LINKEDIN_TITLES,
    # Extra LinkedIn-only searches (LinkedIn's OR / AND syntax), run like the titles in every city.
    "linkedin_queries": LINKEDIN_QUERIES,
    "off_stack": TITLE_EXCLUDE["off_stack"],     # stacks this person doesn't work with (title words)
    "on_stack_words": ["python", "ai", "ml", "machine learning", "data scien", "llm", "genai", "gen ai", "nlp"],
    "wellfound_keywords": WELLFOUND["input"]["keywords"],
    "internshala_queries": json.loads(INTERNSHALA["input"]["query"]),
    "naukri_departments": NAUKRI["input"]["departmentIds"],
    "skills": [],                     # empty = read from the resume's SKILLS section
    # Cities to search (e.g. ["Pune", "Bangalore", "Remote"]). Empty = all of India. Sent to every
    # board's search, so Apify only returns (and charges for) jobs there; "Remote" adds remote jobs.
    "locations": [],
    # Public Telegram channels with job posts (names or t.me links), e.g. ["FreshersJobsUpdates"].
    "telegram_channels": [],
    # Telegram alerts (bot token = key TELEGRAM_BOT_TOKEN): the chat that gets them, set by "Connect".
    "telegram_chat_id": "",
    "telegram_alerts": True,
}

# Other spellings of a city, used when checking a job's location text.
CITY_ALIASES = {
    "bangalore": ["bangalore", "bengaluru"], "bengaluru": ["bangalore", "bengaluru"],
    "gurgaon": ["gurgaon", "gurugram"], "gurugram": ["gurgaon", "gurugram"],
    "delhi": ["delhi", "ncr"], "new delhi": ["delhi", "ncr"], "ncr": ["delhi", "ncr", "noida", "gurgaon", "gurugram"],
    "mumbai": ["mumbai", "bombay", "thane"], "chennai": ["chennai", "madras"],
    "kolkata": ["kolkata", "calcutta"], "trivandrum": ["trivandrum", "thiruvananthapuram"],
}
