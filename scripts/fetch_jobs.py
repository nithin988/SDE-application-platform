#!/usr/bin/env python3
"""
Daily job fetcher for job-scanner.

Pulls open positions from each company's public job-board API (see
config/companies.json), filters for entry-level SDE-type roles in
South Indian tech hubs, dedupes against previously-seen postings, and
writes the result to docs/data/jobs.json for the dashboard to render.
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
OUTPUT_PATH = ROOT / "docs" / "data" / "jobs.json"

# --- filtering rules -------------------------------------------------------

TITLE_INCLUDE = re.compile(
    r"\b(software\s*(development\s*)?engineer|system(s)?\s*(development\s*)?engineer|"
    r"software\s*developer|application\s*software\s*engineer|"
    r"sde|swe|backend\s*engineer|"
    r"frontend\s*engineer|full\s*stack\s*engineer|"
    r"platform\s*engineer)\b",
    re.IGNORECASE,
)
TITLE_EXCLUDE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|architect|"
    r"intermediate|intern(ship)?|vp|head\s*of)\b",
    re.IGNORECASE,
)
# Catches mid/senior level markers attached to an engineer title, e.g.
# "Software Development Engineer II", "SDE-2", "SDE III", "Engineer-Test".
LEVEL_EXCLUDE = re.compile(
    r"(engineer|developer|sde|swe)\s*[-,]?\s*(ii|iii|iv|v|[2-9]|test)\b", re.IGNORECASE
)
# Catches explicit experience-range call-outs in the title itself, e.g.
# "(3-5 Years)" or "5+ Years" - a real 1 YOE candidate doesn't clear these.
YEARS_EXCLUDE = re.compile(r"(\d+)\s*(?:-\s*\d+\s*)?\+?\s*years?", re.IGNORECASE)

# For Amazon specifically: a plain "SDE" or "Software Development Engineer"
# title does NOT reliably mean entry level - e.g. "SDE, Alexa For Shopping"
# asks for 3+ years while "Software Development Engineer I, FinOps FP&A"
# asks for 1+. Amazon's own basic_qualifications text is the real signal.
YEARS_IN_TEXT = re.compile(r"(\d+)\+?\s*(?:-\s*\d+\s*)?\s*years?", re.IGNORECASE)


def min_years_required(text: str):
    if not text:
        return None
    years = [int(m.group(1)) for m in YEARS_IN_TEXT.finditer(text)]
    return min(years) if years else None


def title_requires_too_much_experience(title: str) -> bool:
    for m in YEARS_EXCLUDE.finditer(title):
        if int(m.group(1)) >= 3:
            return True
    return False
LOCATION_INCLUDE = re.compile(
    r"\b(hyderabad|bengaluru|bangalore|india)\b", re.IGNORECASE
)

HEADERS = {"User-Agent": "job-scanner/1.0 (personal job search tool)"}
TIMEOUT = 20


def matches(title: str, location: str) -> bool:
    if not title or not location:
        return False
    if not LOCATION_INCLUDE.search(location):
        return False
    if TITLE_EXCLUDE.search(title) or LEVEL_EXCLUDE.search(title):
        return False
    if title_requires_too_much_experience(title):
        return False
    return bool(TITLE_INCLUDE.search(title))


# --- per-ATS fetchers --------------------------------------------------------
# Each fetcher returns a list of dicts: {raw_id, title, location, url}

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
    # A plain "SDE" / "Software Development Engineer" title at Amazon does
    # NOT reliably mean entry level - e.g. "SDE, Alexa For Shopping" asks
    # for 3+ years while "Software Development Engineer I, FinOps FP&A"
    # asks for 1+. So beyond querying both the software and systems tracks,
    # every candidate is gated on its actual basic_qualifications text.
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
                min_years = min_years_required(j.get("basic_qualifications", ""))
                if min_years is not None and min_years > 1:
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

    results = []
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
            errors.append(f"{company['name']}: {exc}")
            continue

        for j in raw_jobs:
            if not matches(j["title"], j["location"]):
                continue
            job_id = f"{company['name']}::{j['raw_id']}"
            is_new = job_id not in seen
            first_seen = seen.get(job_id, today)
            seen[job_id] = first_seen
            results.append(
                {
                    "id": job_id,
                    "company": company["name"],
                    "title": j["title"],
                    "location": j["location"],
                    "url": j["url"],
                    "first_seen": first_seen,
                    "is_new": is_new,
                }
            )

    # newest first, then company, then title
    results.sort(key=lambda r: (not r["is_new"], r["company"], r["title"]))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(
            {
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

    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Fetched {len(results)} matching jobs ({sum(1 for r in results if r['is_new'])} new).")
    if errors:
        print("Errors:", *errors, sep="\n  - ")


if __name__ == "__main__":
    sys.exit(main())
