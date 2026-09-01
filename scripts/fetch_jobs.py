#!/usr/bin/env python3
"""
Daily job fetcher for job-scanner.

Pulls open positions from each company's public job-board API (see
config/companies.json) ONCE, then filters that same pool separately per
person profile (see PROFILES below) - e.g. entry-level SDE-1 for Nithin,
SDE-2 for Jaswanth - in South Indian tech hubs. Dedupes against
previously-seen postings per profile and writes one JSON file per person
for the dashboard to render.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "companies.json"
SEEN_PATH = ROOT / "data" / "seen.json"
DATA_DIR = ROOT / "docs" / "data"

HEADERS = {"User-Agent": "job-scanner/1.0 (personal job search tool)"}
TIMEOUT = 20

# --- shared filtering building blocks ---------------------------------------

LOCATION_INCLUDE = re.compile(
    r"\b(hyderabad|bengaluru|bangalore|india)\b", re.IGNORECASE
)

# The job families both profiles care about (SDE/SWE and close synonyms).
ROLE_FAMILY_INCLUDE = re.compile(
    r"\b(software\s*(development\s*)?engineer|system(s)?\s*(development\s*)?engineer|"
    r"software\s*developer|application\s*software\s*engineer|"
    r"sde|swe|backend\s*engineer|"
    r"frontend\s*engineer|full\s*stack\s*engineer|"
    r"platform\s*engineer)\b",
    re.IGNORECASE,
)

# Seniority words that disqualify a role for EITHER profile - nobody here is
# looking for Staff/Principal/Manager-track postings.
SENIORITY_EXCLUDE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|"
    r"intermediate|intern(ship)?|vp|head\s*of)\b",
    re.IGNORECASE,
)

# Level markers glued to "Engineer/Developer/SDE/SWE", e.g. "...Engineer II",
# "SDE-2", "SDE III", "Engineer-Test". Grouped by numeral so each profile can
# require/exclude the levels it cares about.
LEVEL1_MARKER = re.compile(r"(engineer|developer|sde|swe)\s*[-,]?\s*(i|1)\b", re.IGNORECASE)
LEVEL2_MARKER = re.compile(r"(engineer|developer|sde|swe)\s*[-,]?\s*(ii|2)\b", re.IGNORECASE)
LEVEL3PLUS_MARKER = re.compile(
    r"(engineer|developer|sde|swe)\s*[-,]?\s*(iii|iv|v|[3-9]|test)\b", re.IGNORECASE
)

# Catches explicit experience-range call-outs in the title itself, e.g.
# "(3-5 Years)" or "5+ Years".
YEARS_IN_TITLE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?\+?\s*years?", re.IGNORECASE)

# For Amazon specifically: a plain "SDE" / "Software Development Engineer"
# title does NOT reliably signal level - e.g. "SDE, Alexa For Shopping" asks
# for 3+ years while "Software Development Engineer I, FinOps FP&A" asks for
# 1+. Amazon's own basic_qualifications text is the real signal, so Amazon
# postings additionally carry a "min_years" field extracted from it.
YEARS_IN_TEXT = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?\s*years?", re.IGNORECASE)


def min_years_required(text: str):
    if not text:
        return None
    years = [int(m.group(1)) for m in YEARS_IN_TEXT.finditer(text)]
    return min(years) if years else None


def title_states_years_over(title: str, cap: int) -> bool:
    for m in YEARS_IN_TITLE.finditer(title):
        if int(m.group(1)) > cap:
            return True
    return False


def base_eligible(job: dict) -> bool:
    """Location + role-family + seniority checks shared by every profile."""
    title, location = job.get("title", ""), job.get("location", "")
    if not title or not location:
        return False
    if not LOCATION_INCLUDE.search(location):
        return False
    if SENIORITY_EXCLUDE.search(title):
        return False
    return bool(ROLE_FAMILY_INCLUDE.search(title))


def matches_entry_level(job: dict) -> bool:
    """Nithin's profile: SDE-1 / ~1 YOE."""
    if not base_eligible(job):
        return False
    title = job["title"]
    if LEVEL2_MARKER.search(title) or LEVEL3PLUS_MARKER.search(title):
        return False
    if title_states_years_over(title, 2):
        return False
    min_years = job.get("min_years")
    if min_years is not None and min_years > 1:
        return False
    return True


def matches_sde2(job: dict) -> bool:
    """Jaswanth's profile: SDE-2 / "Software Development Engineer II"."""
    if not base_eligible(job):
        return False
    title = job["title"]
    if LEVEL3PLUS_MARKER.search(title):
        return False
    if not LEVEL2_MARKER.search(title):
        return False  # require an explicit "II"/"2" marker - that's the ask
    min_years = job.get("min_years")
    if min_years is not None and (min_years < 2 or min_years > 5):
        return False
    return True


PROFILES = {
    "nithin": {
        "label": "Nithin — SDE 1 (entry level, ~1 YOE)",
        "matches": matches_entry_level,
    },
    "jaswanth": {
        "label": "Jaswanth — SDE 2 / Software Development Engineer II",
        "matches": matches_sde2,
    },
}


# --- per-ATS fetchers --------------------------------------------------------
# Each fetcher returns a list of dicts: {raw_id, title, location, url}
# (Amazon additionally sets "min_years" when its own JD states a floor.)

def fetch_greenhouse(cfg):
    token = cfg["token"]
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(
            {
                "raw_id": str(j.get("id")),
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name", ""),
                "url": j.get("absolute_url", ""),
            }
        )
    return out


def fetch_workday(cfg):
    tenant, host, site = cfg["tenant"], cfg["wdHost"], cfg["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    api_url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out = []
    offset = 0
    page_size = 20
    max_pages = 15  # safety cap
    for _ in range(max_pages):
        body = {
            "limit": page_size,
            "offset": offset,
            "searchText": "engineer",
        }
        r = requests.post(api_url, json=body, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        postings = data.get("jobPostings", [])
        if not postings:
            break
        for p in postings:
            path = p.get("externalPath", "")
            out.append(
                {
                    "raw_id": path,
                    "title": p.get("title", ""),
                    "location": p.get("locationsText", ""),
                    "url": f"{base}/{site}{path}",
                }
            )
        offset += page_size
        if offset >= data.get("total", 0):
            break
    return out


def fetch_lever(cfg):
    token = cfg["token"]
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        categories = j.get("categories", {}) or {}
        out.append(
            {
                "raw_id": j.get("id", ""),
                "title": j.get("text", ""),
                "location": categories.get("location", ""),
                "url": j.get("hostedUrl", ""),
            }
        )
    return out


def fetch_oracle_fusion(cfg):
    # Oracle's own careers site (careers.oracle.com) runs on Oracle Fusion
    # Cloud Recruiting. host/site_number are per-company Fusion tenant info.
    host, site_number, keyword = cfg["host"], cfg["siteNumber"], cfg["keyword"]
    base = f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    out = []
    offset = 0
    page_size = 25
    max_pages = 10
    for _ in range(max_pages):
        finder = (
            f"findReqs;siteNumber={site_number},limit={page_size},"
            f"offset={offset},keyword=%22{keyword}%22"
        )
        url = f"{base}?onlyData=true&expand=requisitionList.secondaryLocations&finder={finder}"
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        item = r.json()["items"][0]
        reqs = item.get("requisitionList", [])
        if not reqs:
            break
        for j in reqs:
            out.append(
                {
                    "raw_id": str(j.get("Id", "")),
                    "title": j.get("Title", ""),
                    "location": j.get("PrimaryLocation", ""),
                    "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site_number}/job/{j.get('Id', '')}",
                }
            )
        offset += page_size
        if offset >= item.get("TotalJobsCount", 0):
            break
    return out


def fetch_smartrecruiters(cfg):
    company = cfg["company"]
    url = f"https://api.smartrecruiters.com/v1/companies/{company}/postings?limit=100"
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json().get("content", []):
        loc = p.get("location", {}) or {}
        location = ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
        out.append(
            {
                "raw_id": p.get("id", ""),
                "title": p.get("name", ""),
                "location": location,
                "url": p.get("applyUrl") or p.get("ref", ""),
            }
        )
    return out


def fetch_amazon(cfg):
    # Query both the software and systems development engineer tracks, and
    # attach each posting's real experience floor (from basic_qualifications)
    # so downstream per-profile matching can gate on it - title alone lies
    # about level at Amazon often enough to matter (see module docstring).
    queries = [
        "software+development+engineer",
        "systems+development+engineer",
    ]
    seen_ids = set()
    out = []
    for base_query in queries:
        offset = 0
        page_size = 100
        max_pages = 10
        for _ in range(max_pages):
            url = (
                "https://www.amazon.jobs/en/search.json"
                f"?base_query={base_query}&loc_query=India"
                f"&offset={offset}&result_limit={page_size}"
            )
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            jobs = data.get("jobs", [])
            if not jobs:
                break
            for j in jobs:
                raw_id = str(j.get("id_icims") or j.get("job_path", ""))
                if raw_id in seen_ids:
                    continue
                seen_ids.add(raw_id)
                city = j.get("city", "")
                country = j.get("country_code", "")
                out.append(
                    {
                        "raw_id": raw_id,
                        "title": j.get("title", ""),
                        "location": f"{city}, {country}",
                        "url": "https://www.amazon.jobs" + j.get("job_path", ""),
                        "min_years": min_years_required(j.get("basic_qualifications", "")),
                    }
                )
            offset += page_size
            if offset >= data.get("hits", 0):
                break
    return out


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "workday": fetch_workday,
    "oracle_fusion": fetch_oracle_fusion,
    "smartrecruiters": fetch_smartrecruiters,
    "amazon": fetch_amazon,
}


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def main():
    config = load_json(CONFIG_PATH, {"companies": []})
    seen = load_json(SEEN_PATH, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    raw_by_company = []  # [(company_name, [raw_job, ...]), ...]
    errors = []

    for company in config["companies"]:
        name = company.get("name")
        if not name:
            continue  # skip stray/comment entries in the config
        ctype = company.get("type")
        if ctype == "manual" or not company.get("verified", True):
            continue
        fetcher = FETCHERS.get(ctype)
        if not fetcher:
            errors.append(f"{name}: no fetcher for type '{ctype}'")
            continue
        try:
            raw_jobs = fetcher(company)
        except Exception as exc:  # noqa: BLE001 - keep the run going for other companies
            errors.append(f"{name}: {exc}")
            continue
        raw_by_company.append((name, raw_jobs))

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # A sidebar list for the dashboard: which companies are auto-tracked
    # right now, vs which ones need a manual check (no simple public API,
    # or a previously-working one that broke) - so nothing gets missed.
    tracked_names = sorted(name for name, _ in raw_by_company)
    manual_companies = []
    for company in config["companies"]:
        name = company.get("name")
        if not name or name in tracked_names:
            continue
        url = company.get("url") or (
            "https://www.google.com/search?q="
            + requests.utils.quote(f"{name} careers software engineer India")
        )
        manual_companies.append({"name": name, "url": url})
    manual_companies.sort(key=lambda c: c["name"])

    (DATA_DIR / "companies.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "tracked": tracked_names,
                "manual": manual_companies,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    summary_lines = []

    for profile_key, profile in PROFILES.items():
        results = []
        for company_name, raw_jobs in raw_by_company:
            for j in raw_jobs:
                if not profile["matches"](j):
                    continue
                job_id = f"{profile_key}::{company_name}::{j['raw_id']}"
                is_new = job_id not in seen
                first_seen = seen.get(job_id, today)
                seen[job_id] = first_seen
                results.append(
                    {
                        "id": job_id,
                        "company": company_name,
                        "title": j["title"],
                        "location": j["location"],
                        "url": j["url"],
                        "first_seen": first_seen,
                        "is_new": is_new,
                    }
                )

        # newest first, then company, then title
        results.sort(key=lambda r: (not r["is_new"], r["company"], r["title"]))

        output_path = DATA_DIR / f"jobs-{profile_key}.json"
        output_path.write_text(
            json.dumps(
                {
                    "profile": profile_key,
                    "label": profile["label"],
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "total": len(results),
                    "new_today": sum(1 for r in results if r["is_new"]),
                    "errors": errors,
                    "jobs": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        new_count = sum(1 for r in results if r["is_new"])
        summary_lines.append(f"{profile_key}: {len(results)} matching jobs ({new_count} new)")

    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")

    print("Fetched -", " | ".join(summary_lines))
    if errors:
        print("Errors:", *errors, sep="\n  - ")


if __name__ == "__main__":
    sys.exit(main())
