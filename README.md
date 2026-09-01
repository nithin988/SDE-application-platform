# SDE Job Radar

A daily-refreshing dashboard of SDE / SWE openings in Hyderabad, Bengaluru,
and India, pulled straight from company career-site APIs (Greenhouse,
Workday, Oracle Fusion, SmartRecruiters, Amazon Jobs). No LinkedIn/Naukri
scraping, no login required — just the same data the companies' own job
pages use, filtered and deduped for you.

It serves **two people from the same company pool**, each with their own
tab on the dashboard and their own filter:

- **Nithin** — SDE-1 / entry level, ~1 year of experience.
- **Jaswanth** — SDE-2 / "Software Development Engineer II", ~2-5 years.

## What it does

1. `scripts/fetch_jobs.py` hits each configured company's public jobs API
   **once** to pull every open posting, then runs that same raw pool through
   two separate profile filters (`PROFILES` in the script):
   - **Nithin's filter** keeps postings whose **title** looks entry-level
     (excludes Senior/Staff/Lead/Manager/Intermediate/Intern and any
     SDE-II+/level-2+ marker) with **location** in Hyderabad, Bengaluru, or
     India. For Amazon specifically, title alone isn't reliable — a plain
     "SDE" posting there can ask for 3+ years just as often as 1+ — so
     Amazon postings are additionally gated on their own
     `basic_qualifications` text, dropping anything stating more than 1
     year required, regardless of what the title says. That text lists
     several bullets (e.g. "3+ years of ... development experience" then
     later "1+ years of Object Oriented Design"), so the check reads the
     **first** number mentioned - Amazon consistently leads with the real,
     overall experience bar; taking the minimum across every bullet let
     genuinely 3+ year roles slip through on an unrelated sub-skill bullet.
   - **Jaswanth's filter** requires an explicit **"II"** or **"2"** level
     marker attached to the role (e.g. "Software Development Engineer II",
     "SDE 2", "Software Engineer II") and the same India location check.
     For Amazon, it additionally checks that the stated experience floor
     is in the 2-5 year range, so a mislabeled posting can't sneak in either
     direction.
2. It compares against `data/seen.json` (a memory of every job ID it has
   ever matched, namespaced per profile) so each run tags brand-new
   postings with `is_new: true` independently for each person.
3. It writes `docs/data/jobs-nithin.json` and `docs/data/jobs-jaswanth.json`.
4. `docs/index.html` is a static dashboard with a tab for each person; it
   reads the matching JSON file and lets you search/filter and click
   straight through to the real "Apply" page. Within a tab, Amazon is kept
   in its own section since it reliably posts far more openings than every
   other company combined — mixing them in would bury the smaller companies.
5. A GitHub Actions workflow (`.github/workflows/daily-jobs.yml`) runs step 1
   every day at 09:30 IST and commits the updated JSON back to the repo —
   which GitHub Pages then serves automatically. No server to maintain.
   (GitHub's scheduled cron is best-effort, not to-the-second — during
   high load on their shared runners it can fire up to 10-20 minutes late;
   that's normal and not a sign anything is broken.)

**This runs on GitHub's servers, not your laptop.** Once deployed (see
below), the whole pipeline — fetch, filter, dedupe, publish — executes
inside a temporary Ubuntu VM that GitHub spins up on its own cron schedule.
Your computer being off, asleep, or closed has zero effect on whether it
runs; you never need to `python scripts/fetch_jobs.py` yourself again after
today. Running it locally (as we did above) is only for testing/debugging —
production is entirely the GitHub Actions + Pages combo.

## Companies currently wired up (verified working, 27 total)

| Company | Source | Company | Source |
|---|---|---|---|
| Stripe | Greenhouse | Datadog | Greenhouse |
| Databricks | Greenhouse | Elastic | Greenhouse |
| Coinbase | Greenhouse | PagerDuty | Greenhouse |
| Airbnb | Greenhouse | Robinhood | Greenhouse |
| Rubrik | Greenhouse | Twilio | Greenhouse |
| Okta | Greenhouse | Affirm | Greenhouse |
| MongoDB | Greenhouse | Gusto | Greenhouse |
| Cloudflare | Greenhouse | Brex | Greenhouse |
| Dropbox | Greenhouse | Scale AI | Greenhouse |
| GitLab | Greenhouse | Anthropic | Greenhouse |
| Asana | Greenhouse | Freshworks | Lever |
| Figma | Greenhouse | Palantir | Lever |
| Zscaler | Greenhouse | Salesforce | Workday |
| Samsara | Greenhouse | Nvidia | Workday |
| Amazon | Amazon Jobs API | Mastercard | Workday |
| | | Oracle | Oracle Fusion Recruiting |

Not every one of these hires SDE-1s/SDE-2s in Hyderabad/Bengaluru every
day — a company showing 0 jobs on a given run just means nothing matching
is open right now, the fetcher is still working correctly.

**PhonePe (Greenhouse) and Plaid (Lever)** worked when first added but
started 404ing shortly after — their public board likely moved or got
disabled, not a bug in this project. Marked `"verified": false` in
`config/companies.json` with a note to recheck later; flip it back to
`true` if their board comes back.

Rippling, Glean, Atlassian, ServiceNow, Adobe, Uber, Flipkart, Swiggy, and
Razorpay are listed in `config/companies.json` but marked
`"verified": false` — their career sites either use a harder-to-guess
Workday tenant/site pair or a fully custom system that resisted a quick
brute-force check. See **Adding a company** below; most take 5–10 minutes
with the DevTools method once you're on the actual careers page.

### Companies with no simple public API

A few enterprises were specifically checked and don't have a
straightforward REST endpoint to poll — each uses a platform that requires
a live browser session, not just a URL:

- **Cisco** — runs on Phenom People; job search happens via an obfuscated,
  session-scoped `POST /widgets` call, not a stable public endpoint.
- **Qualcomm** — runs on Eightfold.ai, but the public search API rejected
  requests with `"Not authorized for PCSX"` — it's gated behind a
  session/referrer check.
- **Intuit** — runs a Radancy-style stateful search widget (same family as
  Adobe's careers site); results depend on a server-assigned session,
  geolocation state, and cookies, not a plain query string.
- **Visa** — the ATS behind their careers page wasn't identifiable at all;
  the obvious URL guesses 404 in the current site structure.

Google, Apple, and Microsoft are in the same boat — they either sign their
requests or render everything client-side. All seven are marked `"type":
"manual"` in the config with a direct search URL, so the dashboard's
"errors" panel won't nag about them, but they won't auto-populate. Check
them by hand every few days, or contribute a browser-automation-based
fetcher (e.g. Playwright) if you want to push further — that's a bigger
lift than the rest of this project and wasn't worth it for a handful of
companies.

**Oracle** looked like it would be in this bucket too (career page is a
client-rendered SPA), but its underlying "Oracle Fusion Cloud Recruiting"
system exposes a plain REST endpoint
(`eeho.fa.us2.oraclecloud.com/hcmRestApi/...`) that returns full job data
with no auth — found by inspecting where the SPA's own network calls went.

## Deploying it (one-time setup, ~10 minutes)

1. **Create a GitHub repo** (public — GitHub Pages' free tier requires it
   unless you're on a paid plan) and push this folder to it:
   ```bash
   cd job-scanner
   git init
   git add .
   git commit -m "Initial job scanner"
   git branch -M main
   git remote add origin https://github.com/<you>/job-scanner.git
   git push -u origin main
   ```
2. **Enable Actions write access**: repo → Settings → Actions → General →
   "Workflow permissions" → select **Read and write permissions** → Save.
   (The daily workflow commits `docs/data/jobs.json` back to the repo, so it
   needs this.)
3. **Enable GitHub Pages**: repo → Settings → Pages → "Build and
   deployment" → Source: **Deploy from a branch** → Branch: `main`,
   folder: `/docs` → Save.
4. **Run it once manually** to generate real data instead of waiting for
   tomorrow's cron: repo → Actions tab → "Daily job scan" → **Run workflow**.
5. After it finishes (~30s) and Pages finishes deploying (~1 min), your
   dashboard is live at:
   ```
   https://<you>.github.io/job-scanner/
   ```

From here it updates itself daily — nothing to run locally.

## Using it effectively day-to-day

- **Bookmark the Pages URL** and check it once a day (e.g. with morning
  coffee). Toggle **"New today only"** to see just what changed since
  yesterday — that's your actual to-do list.
- **Apply the same day a role appears.** Entry-level SDE postings at these
  companies get hundreds of applicants within 48 hours; being early in the
  queue measurably helps at Greenhouse/Workday-based ATSs where recruiters
  often work top-down by submission time.
- **Use the search box** to quickly jump to a specific company or keyword
  (e.g. "backend", "platform") if you want to prioritize.
- **Don't just spray-apply** — for each "Apply", spend 2 minutes tailoring
  your resume bullet order to the specific team named in the title (e.g.
  "FinTech", "Prime Video", "Alexa") before submitting; it's a five-second
  scan for the ATS, but a strong contextual match to the JD wording helps if
  the company runs resume filtering.
- **Track what you've applied to** separately (a simple Google Sheet: date,
  company, title, link, status) — this tool tells you *what's open*, it
  doesn't track *your* application status.
- **Widen the filter if the list feels thin.** Filtering is intentionally
  strict about level (SDE-1 only) to reduce noise. If you're open to SDE-2
  roles too, loosen `TITLE_EXCLUDE`/`LEVEL_EXCLUDE` in
  `scripts/fetch_jobs.py` (see below).

## Adding a company

Open `config/companies.json`. Each entry needs a `type`:

- **`greenhouse`** — needs a `token`. Find it by opening the company's
  careers page, searching page source / Network tab for
  `boards-api.greenhouse.io/v1/boards/<token>/jobs`, or just try the
  company's name/slug directly:
  `https://boards-api.greenhouse.io/v1/boards/<guess>/jobs?content=false`
  (200 = correct, 404 = wrong).
- **`lever`** — same idea with
  `https://api.lever.co/v0/postings/<token>?mode=json`.
- **`workday`** — needs `tenant`, `wdHost` (e.g. `wd1`, `wd5`, `wd12` — try
  each), and `site`. Open the company's careers page, open DevTools →
  Network → XHR, reload, and look for a POST request to
  `.../wday/cxs/<tenant>/<site>/jobs` — the URL gives you all three values.
- **`smartrecruiters`** — needs `company` (their SmartRecruiters company
  slug), used against
  `https://api.smartrecruiters.com/v1/companies/<company>/postings`.
- **`manual`** — for anything with no public API (Google, Apple, Microsoft
  fall here today). These are listed in the dashboard's "errors" as a
  reminder but aren't auto-fetched; check them manually or contribute a
  fetcher in `scripts/fetch_jobs.py` if you reverse-engineer their endpoint.

Set `"verified": true` once you confirm it returns real jobs (run
`python scripts/fetch_jobs.py` locally and check for it in the output/errors).

## Adjusting the filter

Both filters live in `scripts/fetch_jobs.py`:

- `LOCATION_INCLUDE` — regex for which locations count (add cities here).
- `TITLE_INCLUDE` — regex for which job families count (Software/System
  Development Engineer, SDE, SWE, backend/frontend/full-stack/platform
  engineer, etc). "System(s) Development Engineer" is included alongside
  "Software Development Engineer" since some companies (AWS in particular)
  use it as an equivalent SDE track.
- `TITLE_EXCLUDE` — seniority words (Senior, Staff, Lead, Manager,
  Intermediate, Intern, etc).
- `LEVEL_EXCLUDE` — level markers glued to "Engineer/SDE/SWE" (II, III, IV,
  2, 3, 4) so "Software Development Engineer II/III" are dropped even
  though they don't say "Senior" anywhere in the title.
- `YEARS_EXCLUDE` (via `title_requires_too_much_experience`) — drops
  postings whose title itself states an experience bar of 3+ years, e.g.
  "(3-5 Years)" or "5+ Years".

## Running locally

```bash
pip install -r requirements.txt
python scripts/fetch_jobs.py
# then open docs/index.html via a local server, e.g.:
python -m http.server 8000 --directory docs
```
(Opening `docs/index.html` directly as a `file://` URL won't work — the
dashboard fetches `data/jobs.json`, which browsers block over `file://`.)
