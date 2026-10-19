import re
from datetime import datetime, timezone

import requests

from app import config


class ProviderError(Exception):
    def __init__(self, message, retryable=False, status="failed"):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


def username_from_url(url):
    """https://www.linkedin.com/in/some-slug/ -> some-slug"""
    m = re.search(r"/in/([^/?#]+)", url or "")
    if not m:
        raise ProviderError("No LinkedIn username in URL", retryable=False, status="needs_review")
    return m.group(1)



# --------------------------------------------------------------------------
# Employment date helpers
# --------------------------------------------------------------------------
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NUM = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}
_DATE_RANGE_KEYS = ("date_range", "dates", "date", "duration", "period", "time_period")
_START_KEYS = ("start_date", "starts_at", "start", "from", "start_year")
_END_KEYS = ("end_date", "ends_at", "end", "to", "end_year")


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", {}):
            return v
    return None


def _to_ym(value):
    """Return (year, month|None) from dict / 'Aug 2021' / '2021-08-01' / 2021."""
    if value is None:
        return None
    if isinstance(value, dict):
        y = value.get("year")
        m = value.get("month")
        if not y:
            return None
        try:
            return int(y), (int(m) if m else None)
        except (TypeError, ValueError):
            return None
    text = str(value).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})", text)          # 2021-08 / 2021-08-01
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{4})", text)  # Aug 2021
    if m and m.group(1)[:3].lower() in _MONTH_NUM:
        return int(m.group(2)), _MONTH_NUM[m.group(1)[:3].lower()]
    m = re.search(r"(\d{4})", text)                      # 2021
    if m:
        return int(m.group(1)), None
    return None


def _label(ym):
    y, m = ym
    return f"{_MONTHS[m - 1]} {y}" if m else str(y)


def _duration_text(start, end):
    """LinkedIn-style inclusive duration: Aug 2021 -> Oct 2026 = '5 yrs 3 mos'."""
    sy, sm = start[0], start[1] or 1
    ey, em = end[0], end[1] or 12
    months = (ey - sy) * 12 + (em - sm) + 1
    if months < 1:
        return None
    yrs, mos = divmod(months, 12)
    parts = []
    if yrs:
        parts.append(f"{yrs} yr{'s' if yrs > 1 else ''}")
    if mos:
        parts.append(f"{mos} mo{'s' if mos > 1 else ''}")
    return " ".join(parts)


def employment_dates(exp):
    """Build 'Aug 2021 - Present · 5 yrs 3 mos' from whatever the API gave us."""
    if not isinstance(exp, dict):
        return None
    raw = _first(exp, _DATE_RANGE_KEYS)
    now = datetime.now(timezone.utc)
    today = (now.year, now.month)

    if isinstance(raw, str):
        text = raw.strip()
        has_duration = bool(re.search(r"\b\d+\s*(yr|yrs|year|years|mo|mos|month|months)\b", text, re.I))
        if has_duration:
            return text
        is_present = bool(re.search(r"present|current|now", text, re.I))
        years = re.findall(r"[A-Za-z]{3,9}\.?\s+\d{4}|\d{4}", text)
        start = _to_ym(years[0]) if years else None
        end = today if is_present else (_to_ym(years[1]) if len(years) > 1 else None)
        if start and end:
            dur = _duration_text(start, end)
            return f"{text} · {dur}" if dur else text
        return text

    start_raw = _first(exp, _START_KEYS)
    end_raw = _first(exp, _END_KEYS)
    if isinstance(raw, dict):  # {"start": {...}, "end": {...}}
        start_raw = start_raw or _first(raw, _START_KEYS)
        end_raw = end_raw or _first(raw, _END_KEYS)
    start = _to_ym(start_raw)
    if not start:
        return str(raw) if raw else None
    end = _to_ym(end_raw)
    is_present = end is None or bool(exp.get("is_current") or exp.get("current"))
    end_ym = today if is_present else end
    # Year-only start ('2025') would give a made-up month count: leave duration out.
    dur = _duration_text(start, end_ym) if start[1] else None
    label = f"{_label(start)} - {'Present' if is_present else _label(end)}"
    return f"{label} · {dur}" if dur else label

# --------------------------------------------------------------------------
# ChocoData
# --------------------------------------------------------------------------
def fetch_raw(url):
    if not config.CHOCODATA_API_KEY:
        raise ProviderError("CHOCODATA_API_KEY is not set", retryable=False, status="restricted")
    username = username_from_url(url)
    try:
        resp = requests.get(
            config.CHOCODATA_PROFILE_URL,
            params={"api_key": config.CHOCODATA_API_KEY, "username": username},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ProviderError(f"Network error: {exc}", retryable=True, status="failed")

    code = resp.status_code
    if code == 200:
        try:
            return resp.json()
        except ValueError:
            raise ProviderError("Provider returned invalid JSON", retryable=True)
    body = resp.text[:500]
    if code == 429:
        raise ProviderError(f"Provider rate limit (429): {body}", retryable=True, status="failed")
    if code in (401, 403):
        raise ProviderError(f"Provider auth/quota error {code}: {body}", retryable=False, status="restricted")
    if code == 404:
        if "json" not in resp.headers.get("content-type", "").lower():
            raise ProviderError(
                f"Got a web page, not API JSON (HTTP 404) from {resp.url.split('?')[0]}. "
                "Check CHOCODATA_BASE_URL.", retryable=False, status="restricted")
        raise ProviderError(f"Profile not found: {body}", retryable=False, status="needs_review")
    if code >= 500:
        raise ProviderError(f"Provider server error {code}: {body}", retryable=True, status="failed")
    raise ProviderError(f"Provider error {code}: {body}", retryable=False, status="failed")


# --------------------------------------------------------------------------
# Experience parsing. Handles flat entries and LinkedIn's grouped form:
#   LTM · 1 yr 6 mos
#     Cloud & Infrastructure Management · Full-time
#     May 2025 - Present · 1 yr 6 mos
#     Bengaluru, Karnataka, India · Hybrid
# --------------------------------------------------------------------------
_TITLE_KEYS = ("title", "position", "job_title", "role")
_COMPANY_KEYS = ("company", "company_name", "companyName", "organization", "org")
_LOCATION_KEYS = ("location", "job_location", "locality", "geo")
_EMP_TYPE_KEYS = ("employment_type", "employmentType", "job_type", "type")
_WORK_MODE_KEYS = ("work_mode", "workplace_type", "location_type", "workplaceType", "work_type")
_ROLE_LIST_KEYS = ("positions", "roles", "sub_positions", "subPositions", "titles")
_EMP_TYPES = ["Full-time", "Part-time", "Self-employed", "Freelance", "Contract",
              "Internship", "Apprenticeship", "Seasonal", "Trainee"]
_WORK_MODES = {"hybrid": "Hybrid", "remote": "Remote", "on-site": "On-site", "onsite": "On-site"}


def _text(value):
    if value is None:
        return None
    if isinstance(value, dict):
        value = _first(value, ("name", "title", "text", "value"))
    value = str(value).strip() if value is not None else ""
    if value.count("*") >= 3 and value.count("*") >= len(value.replace(" ", "")) / 2:
        return None  # masked text from the logged-out profile view: '******* **** ****'
    return value or None


def _flatten_experience(experience):
    """One row per role; grouped company entries pass their company down."""
    rows = []
    for exp in experience or []:
        if not isinstance(exp, dict):
            continue
        roles = _first(exp, _ROLE_LIST_KEYS)
        if isinstance(roles, list) and roles:
            for role in roles:
                if not isinstance(role, dict):
                    continue
                row = dict(role)
                row["_company"] = _text(_first(exp, _COMPANY_KEYS)) or _text(exp.get("name"))
                row["_company_duration"] = _text(_first(exp, ("duration", "total_duration")))
                if not _first(row, _LOCATION_KEYS):
                    row["location"] = exp.get("location")
                rows.append(row)
        else:
            rows.append(exp)
    return rows


def _is_current(exp):
    if exp.get("is_current") or exp.get("current"):
        return True
    raw = _first(exp, _DATE_RANGE_KEYS)
    if isinstance(raw, str) and re.search(r"present|current|now", raw, re.I):
        return True
    if isinstance(raw, dict):  # {"start": "Sep 2025", "end": "Present"}
        end = _first(raw, _END_KEYS)
        return bool(_first(raw, _START_KEYS)) and (
            not end or bool(re.search(r"present|current|now", str(end), re.I)))
    end = _first(exp, _END_KEYS)
    if isinstance(end, str) and re.search(r"present|current|now", end, re.I):
        return True
    return bool(_first(exp, _START_KEYS)) and not end and not isinstance(raw, str)


def _split_dot(value):
    return [p.strip() for p in re.split(r"\s*[·•|]\s*", value or "") if p.strip()]


def _role_details(exp):
    """Pull title / company / type / dates / location / work mode from one role."""
    title = _text(_first(exp, _TITLE_KEYS))
    company = exp.get("_company") or _text(_first(exp, _COMPANY_KEYS))
    emp_type = _text(_first(exp, _EMP_TYPE_KEYS))
    work_mode = _text(_first(exp, _WORK_MODE_KEYS))
    location = _text(_first(exp, _LOCATION_KEYS))

    # 'LTM · Full-time' / 'Bengaluru, Karnataka, India · Hybrid'
    if company and "·" in company:
        parts = _split_dot(company)
        company = parts[0]
        for p in parts[1:]:
            emp_type = emp_type or next((t for t in _EMP_TYPES if t.lower() == p.lower()), None)
    if title and "·" in title:
        parts = _split_dot(title)
        title = parts[0]
        for p in parts[1:]:
            emp_type = emp_type or next((t for t in _EMP_TYPES if t.lower() == p.lower()), None)
    if location:
        kept = []
        for p in _split_dot(location):
            mode = _WORK_MODES.get(p.lower())
            if mode:
                work_mode = work_mode or mode
            else:
                kept.append(p)
        location = ", ".join(kept) or None
    if work_mode:
        work_mode = _WORK_MODES.get(work_mode.lower().replace("_", "-"), work_mode)
    if emp_type:
        emp_type = next((t for t in _EMP_TYPES
                         if t.lower().replace("-", "") == emp_type.lower().replace("-", "").replace("_", "")),
                        emp_type)

    dates_full = employment_dates(exp)          # 'May 2025 - Present · 1 yr 6 mos'
    dates, duration = dates_full, None
    if dates_full and "·" in dates_full:
        dates, duration = [p.strip() for p in dates_full.split("·", 1)]
    return {
        "title": title, "company": company, "employment_type": emp_type,
        "dates_full": dates_full, "dates": dates, "duration": duration,
        "company_duration": exp.get("_company_duration"),
        "location": location, "work_mode": work_mode,
    }


def _from_chocodata(data, record):
    roles = _flatten_experience(data.get("experience") or data.get("experiences") or [])
    current_idx = next((i for i, r in enumerate(roles) if _is_current(r)), 0 if roles else None)
    current = _role_details(roles[current_idx]) if current_idx is not None else {}
    previous_raw = next((r for i, r in enumerate(roles) if i != current_idx), None)
    previous = _role_details(previous_raw) if previous_raw else {}
    current_company = current.get("company") or _text(data.get("current_company"))
    profile_location = _text(data.get("location"))
    return {
        "full_name": _text(data.get("name") or data.get("full_name")),
        "headline": _text(data.get("headline")),
        "current_job_title": current.get("title"),
        "current_company": current_company,
        "employment_type": current.get("employment_type"),
        "employment_start_date": current.get("dates_full"),
        "employment_dates": current.get("dates"),
        "employment_duration": current.get("duration"),
        "company_duration": current.get("company_duration"),
        "job_location": current.get("location"),
        "work_mode": current.get("work_mode"),
        "previous_job_title": previous.get("title"),
        "previous_company": previous.get("company"),
        "industry": _text(data.get("industry")),
        "location": profile_location or current.get("location") or record.get("location"),
        "raw_experience": (data.get("experience") or data.get("experiences") or [])[:3],
        "source": "chocodata",
        "verification_status": "verified" if current_company else "needs_review",
        "error_message": None if current_company else "No current company in provider data",
    }


# --------------------------------------------------------------------------
# Serper (Google search results). Only what Google shows publicly in the
# result title/snippet: name, headline, usually title/company, location.
# No start dates or previous roles.
# --------------------------------------------------------------------------
def serper_search(query, num=5):
    if not config.SERPER_API_KEY:
        raise ProviderError("SERPER_API_KEY is not set", retryable=False, status="restricted")
    try:
        resp = requests.post(
            config.SERPER_URL,
            headers={"X-API-KEY": config.SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ProviderError(f"Serper network error: {exc}", retryable=True)
    code = resp.status_code
    if code == 200:
        try:
            return resp.json()
        except ValueError:
            raise ProviderError("Serper returned invalid JSON", retryable=True)
    body = resp.text[:300]
    if code == 429:
        raise ProviderError(f"Serper rate limit (429): {body}", retryable=True)
    if code in (400, 401, 403):
        raise ProviderError(f"Serper auth/credit error {code}: {body}", retryable=False, status="restricted")
    if code >= 500:
        raise ProviderError(f"Serper server error {code}: {body}", retryable=True)
    raise ProviderError(f"Serper error {code}: {body}", retryable=False)


def _snippet_company(snippet):
    """'... Engineer at LTM. ...' / 'Experience: LTM · ...' -> 'LTM'"""
    snip = snippet or ""
    m = re.search(r"Experience:\s*([^·|]+)", snip)
    if m:
        return m.group(1).strip(" .")
    m = re.search(r"\b(?:at|@|with|joined)\s+([A-Z][\w&.'()\- ]{1,50}?)\s*(?:[.,;·|!]|\s-\s|\s(?:as|in|for|where|and|since|from)\s|$)", snip)
    if m:
        return m.group(1).strip(" .")
    return None


def parse_linkedin_result(title, snippet):
    """Best-effort parse of a Google result for a LinkedIn profile.
    Title forms: 'Name - Company', 'Name - Title at Company', or 'Name - <headline>'
    (a headline may itself contain ' - ', e.g. 'Engineer - Cloud & Infra Management',
    so a trailing part is only taken as the company when the snippet backs it up)."""
    out = {"full_name": None, "headline": None, "current_job_title": None,
           "current_company": None, "location": None}
    t = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title or "", flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\s[-–]\s", t) if p.strip()]
    snip = snippet or ""
    snip_company = _snippet_company(snip)
    if parts:
        out["full_name"] = parts[0]
        rest = parts[1:]
        if rest:
            out["headline"] = " - ".join(rest)
            m = re.match(r"(.+?)\s+(?:at|@)\s+(.+)$", out["headline"], flags=re.I)
            if m:
                out["current_job_title"], out["current_company"] = m.group(1).strip(), m.group(2).strip()
            elif len(rest) >= 2 and snip_company and snip_company.casefold() == rest[-1].casefold():
                out["current_job_title"], out["current_company"] = " - ".join(rest[:-1]), rest[-1]
            else:
                out["current_job_title"] = out["headline"]
    if not out["current_company"] and snip_company:
        out["current_company"] = snip_company
    if (out["current_job_title"] and out["current_company"]
            and out["current_job_title"].casefold() == out["current_company"].casefold()):
        out["current_job_title"] = None  # 'Name - Company' form: no title given
    m = re.search(r"Location:\s*([^·|]+)", snip)
    if m:
        # 'Hyderabad. View Riju KP's profile on LinkedIn...' -> 'Hyderabad'
        out["location"] = re.split(r"\.\s+View\b|\s+View\s+\S+'s\s+profile", m.group(1))[0].strip(" .")
    return out


def lookup_serper(record):
    url = record.get("linkedin_url")
    slug = username_from_url(url)
    data = serper_search(f'site:linkedin.com/in/{slug}')
    hit = None
    for item in data.get("organic", []) or []:
        link = (item.get("link") or "").lower()
        if f"/in/{slug.lower()}" in link:
            hit = item
            break
    if not hit:
        raise ProviderError("No Google result for this profile", retryable=False, status="needs_review")

    parsed = parse_linkedin_result(hit.get("title"), hit.get("snippet"))
    return {
        "full_name": parsed["full_name"],
        "headline": parsed["headline"],
        "current_job_title": parsed["current_job_title"],
        "current_company": parsed["current_company"],
        "employment_start_date": None,
        "previous_job_title": None,
        "previous_company": None,
        "industry": None,
        "location": parsed["location"] or record.get("location"),
        "source": "serper_google_snippet",
        "verification_status": "verified" if parsed["current_company"] else "needs_review",
        "error_message": None if parsed["current_company"] else "Company not found in search snippet",
    }


# --------------------------------------------------------------------------
# SocialCrawl: experience items with title, company, employment_type,
# location ('Bengaluru · Hybrid') and date {start, end}. 5 credits per call.
# --------------------------------------------------------------------------
def lookup_socialcrawl(record):
    if not config.SOCIALCRAWL_API_KEY:
        raise ProviderError("SOCIALCRAWL_API_KEY is not set", retryable=False, status="restricted")
    url = record.get("linkedin_url")
    username_from_url(url)  # validates the URL
    try:
        resp = requests.get(config.SOCIALCRAWL_URL, headers={"x-api-key": config.SOCIALCRAWL_API_KEY},
                            params={"url": url}, timeout=max(config.REQUEST_TIMEOUT_SECONDS, 60))
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ProviderError(f"SocialCrawl network error: {exc}", retryable=True)
    code = resp.status_code
    body = resp.text[:300]
    if code == 429:
        raise ProviderError(f"SocialCrawl rate limit (429): {body}", retryable=True)
    if code in (401, 402, 403):
        raise ProviderError(f"SocialCrawl auth/credit error {code}: {body}", retryable=False, status="restricted")
    if code == 404:
        raise ProviderError(f"SocialCrawl: profile not found: {body}", retryable=False, status="needs_review")
    if code >= 500:
        raise ProviderError(f"SocialCrawl server error {code}: {body}", retryable=True)
    if code != 200:
        raise ProviderError(f"SocialCrawl error {code}: {body}", retryable=False)
    try:
        payload = resp.json()
    except ValueError:
        raise ProviderError("SocialCrawl returned invalid JSON", retryable=True)
    if not payload.get("success", True):
        raise ProviderError(f"SocialCrawl error: {body}", retryable=False, status="needs_review")
    items = (payload.get("data") or {}).get("items") or []
    if not items:
        raise ProviderError("SocialCrawl: no experience listed on this profile",
                            retryable=False, status="needs_review")
    fields = _from_chocodata({"experience": items}, record)
    for k in ("full_name", "headline"):  # the experiences endpoint has no name/headline
        fields.pop(k, None)
    if not fields.get("location"):
        fields.pop("location", None)
    fields["source"] = "socialcrawl"
    fields["credits_remaining"] = payload.get("credits_remaining")
    return fields


# --------------------------------------------------------------------------
# Apify actor anchor/linkedin-profile-enrichment. Public profile data with
# experiences [{title, company, starts_at: '2025', ends_at}]: dates are often
# year-only and there is no employment type / work mode. Many URLs per run.
# --------------------------------------------------------------------------
def apify_fetch_many(urls):
    """Return {url: item-or-None} for a list of profile URLs (one actor run)."""
    if not config.APIFY_API_TOKEN:
        raise ProviderError("APIFY_API_TOKEN is not set", retryable=False, status="restricted")
    try:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{config.APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"token": config.APIFY_API_TOKEN, "timeout": 290},
            json={"startUrls": [{"url": u, "id": str(i)} for i, u in enumerate(urls)]},
            timeout=310,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise ProviderError(f"Apify network error: {exc}", retryable=True)
    code = resp.status_code
    body = resp.text[:300]
    if code in (401, 402, 403):
        raise ProviderError(f"Apify auth/credit error {code}: {body}", retryable=False, status="restricted")
    if code == 429:
        raise ProviderError(f"Apify rate limit (429): {body}", retryable=True)
    if code == 408:
        raise ProviderError(f"Apify run timed out: {body}", retryable=True)
    if code >= 400:
        raise ProviderError(f"Apify error {code}: {body}", retryable=code >= 500)
    try:
        items = resp.json()
    except ValueError:
        raise ProviderError("Apify returned invalid JSON", retryable=True)
    out = {u: None for u in urls}
    for it in items if isinstance(items, list) else []:
        try:
            out[urls[int(it.get("id"))]] = it
        except (TypeError, ValueError, IndexError):
            continue
    return out


def from_apify(item, record):
    if not item or not (item.get("experiences") or item.get("full_name")):
        raise ProviderError("Apify: no public data for this profile", retryable=False, status="needs_review")
    fields = _from_chocodata({"experience": item.get("experiences") or []}, record)
    place = ", ".join(p for p in (item.get("city"), item.get("country")) if p)
    headline = re.sub(r"\s{2,}", " | ", (item.get("headline") or "").strip()) or None
    fields.update({
        "full_name": _text(item.get("full_name")),
        "headline": headline,
        "current_company": fields.get("current_company") or _text(item.get("company_name")),
        "location": place or fields.get("location"),
        "job_location": fields.get("job_location"),
        "source": "apify",
    })
    fields["verification_status"] = "verified" if fields["current_company"] else "needs_review"
    fields["error_message"] = None if fields["current_company"] else "No current company in public data"
    return fields


# --------------------------------------------------------------------------
# Mindcase 'linkedin/profiles' (SDK: pip install mindcase). Public profile data
# fetched on Mindcase's servers, no LinkedIn login. Each experience entry has
# readable start/end dates and a duration in whole months. Field names are not
# documented on the public page, so every lookup below is tolerant: run
# `python -m app.cli probe-mindcase <url>` and check the raw JSON if a column
# comes back empty.
# --------------------------------------------------------------------------
def _norm_key(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _mc_get(d, *names):
    """Case/underscore-insensitive lookup: 'startDate' == 'start_date'."""
    if not isinstance(d, dict):
        return None
    index = {_norm_key(k): v for k, v in d.items()}
    for n in names:
        v = index.get(_norm_key(n))
        if v not in (None, "", [], {}):
            return v
    return None


def _months_text(months):
    try:
        months = int(round(float(months)))
    except (TypeError, ValueError):
        return None
    if months < 1:
        return None
    yrs, mos = divmod(months, 12)
    parts = []
    if yrs:
        parts.append(f"{yrs} yr{'s' if yrs > 1 else ''}")
    if mos:
        parts.append(f"{mos} mo{'s' if mos > 1 else ''}")
    return " ".join(parts)


def _mc_experience(item):
    """Map Mindcase experience entries onto the keys _role_details understands."""
    raw = _mc_get(item, "experience", "experiences", "positions") or []
    rows = []
    for exp in raw if isinstance(raw, list) else []:
        if not isinstance(exp, dict):
            continue
        end = _mc_get(exp, "end_date", "endDate", "end", "to", "ends_at")
        end_text = _text(end)
        current = bool(_mc_get(exp, "is_current", "current")) or (
            end_text is None or bool(re.search(r"present|current|now", end_text, re.I)))
        months = _mc_get(exp, "duration_months", "durationMonths", "months", "duration")
        if isinstance(months, str):
            m = re.fullmatch(r"\s*(\d+)\s*", months)
            months = int(m.group(1)) if m else None
        rows.append({
            "title": _text(_mc_get(exp, "title", "position", "job_title", "role")),
            "company": _text(_mc_get(exp, "company", "company_name", "companyName", "organization")),
            "employment_type": _text(_mc_get(exp, "employment_type", "employmentType", "job_type", "type")),
            "work_mode": _text(_mc_get(exp, "work_mode", "workplace_type", "workplaceType", "location_type")),
            "location": _text(_mc_get(exp, "location", "job_location")),
            "start_date": _mc_get(exp, "start_date", "startDate", "start", "from", "starts_at"),
            "end_date": None if current else end,
            "current": current,
            "_months": months if isinstance(months, (int, float)) else None,
        })
    return rows


def mindcase_fetch_many(urls):
    """Return {url: row-or-None}. One SDK call (Mindcase polls the job for us)."""
    if not config.MINDCASE_API_KEY:
        raise ProviderError("MINDCASE_API_KEY is not set", retryable=False, status="restricted")
    try:
        from mindcase import Mindcase
    except ImportError:
        raise ProviderError("Mindcase SDK missing: run `pip install mindcase`", retryable=False, status="restricted")
    try:
        client = Mindcase(api_key=config.MINDCASE_API_KEY)
        result = client.run("linkedin/profiles", params={"queries": list(urls)})
    except Exception as exc:  # SDK error classes differ between versions
        text = str(exc)[:300]
        low = text.lower()
        if any(w in low for w in ("api key", "unauthor", "credit", "401", "402", "403")):
            raise ProviderError(f"Mindcase auth/credit error: {text}", retryable=False, status="restricted")
        if "429" in low or "rate" in low:
            raise ProviderError(f"Mindcase rate limit: {text}", retryable=True)
        raise ProviderError(f"Mindcase error: {text}", retryable=True)
    rows = []
    for r in (result if isinstance(result, (list, tuple)) else list(result or [])):
        if hasattr(r, "model_dump"):
            r = r.model_dump()
        elif not isinstance(r, dict) and hasattr(r, "__dict__"):
            r = dict(vars(r))
        if isinstance(r, dict):
            rows.append(r)
    out = {u: None for u in urls}
    if len(urls) == 1 and len(rows) == 1:
        out[urls[0]] = rows[0]
        return out
    for row in rows:
        blob = " ".join(str(v) for v in row.values() if isinstance(v, (str, int)))
        for u in urls:
            slug = username_from_url(u)
            if slug and re.search(rf"/in/{re.escape(slug)}/?(\b|$)", blob, re.I) or (
                    slug and str(_mc_get(row, "handle", "public_identifier", "publicIdentifier") or "").lower() == slug.lower()):
                out[u] = row
    return out


def from_mindcase(item, record):
    roles = _mc_experience(item) if item else []
    if not item or not (roles or _mc_get(item, "headline", "first_name", "firstName")):
        raise ProviderError("Mindcase: no public data for this profile", retryable=False, status="needs_review")
    fields = _from_chocodata({"experience": roles}, record)
    idx = next((i for i, r in enumerate(roles) if r.get("current")), 0 if roles else None)
    cur = roles[idx] if idx is not None else {}
    # Prefer Mindcase's own month count (works even when the start is year-only).
    dur = _months_text(cur.get("_months")) if cur else None
    if dur:
        fields["employment_duration"] = dur
        if fields.get("employment_dates"):
            fields["employment_start_date"] = f"{fields['employment_dates']} · {dur}"
    first, last = _text(_mc_get(item, "first_name", "firstName")), _text(_mc_get(item, "last_name", "lastName"))
    place = ", ".join(p for p in (_text(_mc_get(item, "city")), _text(_mc_get(item, "country"))) if p)
    headline = re.sub(r"\s{2,}", " | ", (_text(_mc_get(item, "headline")) or "")) or None
    fields.update({
        "full_name": " ".join(p for p in (first, last) if p) or None,
        "headline": headline,
        "location": place or fields.get("location"),
        "source": "mindcase",
    })
    fields["verification_status"] = "verified" if fields.get("current_company") else "needs_review"
    fields["error_message"] = None if fields.get("current_company") else "No current company in public data"
    return fields


# --------------------------------------------------------------------------
class EmploymentProvider:
    """PROVIDER_MODE: chocodata_then_serper (default) | serper_only | chocodata_only

    In the default mode both sources are combined: ChocoData's public profile
    card is the most reliable for the current company, Google's result for the
    title/headline. Fields one source leaves empty are filled from the other."""

    _FILL = ("full_name", "headline", "current_job_title", "current_company", "location")

    def __init__(self, use_socialcrawl=False):
        self.use_socialcrawl = use_socialcrawl

    def lookup(self, record):
        if self.use_socialcrawl:
            try:
                return lookup_socialcrawl(record)
            except ProviderError as exc:
                if exc.status == "restricted" or exc.retryable:
                    raise  # out of credits / bad key / rate limit: stop, don't burn the fallbacks
                # otherwise (e.g. no experience listed): fall back to the free sources
        return self._lookup_free(record)

    def _lookup_free(self, record):
        mode = config.PROVIDER_MODE
        if mode == "serper_only":
            return lookup_serper(record)

        choco, choco_exc = None, None
        try:
            choco = _from_chocodata(fetch_raw(record.get("linkedin_url")), record)
        except ProviderError as exc:
            # Never fall back on auth/quota/config problems: surface them.
            if mode == "chocodata_only" or exc.status == "restricted":
                raise
            choco_exc = exc
        if mode == "chocodata_only":
            return choco
        if choco and choco.get("current_job_title") and choco.get("employment_dates"):
            return choco  # already complete
        if not config.SERPER_API_KEY:
            if choco:
                return choco
            raise choco_exc

        try:
            serper = lookup_serper(record)
        except ProviderError as serper_exc:
            if serper_exc.status == "restricted":
                raise
            if choco:
                return choco
            raise ProviderError(
                f"ChocoData: {choco_exc} | Serper: {serper_exc}",
                retryable=choco_exc.retryable or serper_exc.retryable,
                status=serper_exc.status if not choco_exc.retryable else choco_exc.status,
            )
        if not choco:
            return serper

        merged = dict(choco)
        for k in self._FILL:
            if not merged.get(k) and serper.get(k):
                merged[k] = serper[k]
        title, company = merged.get("current_job_title"), merged.get("current_company")
        if title and company and title.casefold() == company.casefold():
            merged["current_job_title"] = None  # Google title was just 'Name - Company'
        merged["source"] = "chocodata+serper"
        company = merged.get("current_company")
        merged["verification_status"] = "verified" if company else "needs_review"
        merged["error_message"] = None if company else "No current company in public data"
        return merged