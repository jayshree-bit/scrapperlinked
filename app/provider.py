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
    dur = _duration_text(start, end_ym)
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


def _from_chocodata(data, record):
    experience = data.get("experience") or []
    current = experience[0] if experience else {}
    previous = experience[1] if len(experience) > 1 else {}
    current_company = data.get("current_company") or current.get("company")
    return {
        "full_name": data.get("name"),
        "headline": data.get("headline"),
        "current_job_title": current.get("title"),
        "current_company": current_company,
        "employment_start_date": employment_dates(current),
        "previous_job_title": previous.get("title"),
        "previous_company": previous.get("company"),
        "industry": None,
        "location": data.get("location") or record.get("location"),
        "source": "chocodata",
        "verification_status": "verified" if current_company else "needs_review",
        "error_message": None,
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


def parse_linkedin_result(title, snippet):
    """Best-effort parse of a Google result for a LinkedIn profile.
    Title forms: 'Name - Title - Company | LinkedIn', 'Name - Title at Company | LinkedIn'.
    Snippet often has 'Experience: Company · Education: ... · Location: ...'."""
    out = {"full_name": None, "headline": None, "current_job_title": None,
           "current_company": None, "location": None}
    t = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", title or "", flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\s[-–]\s", t) if p.strip()]
    if parts:
        out["full_name"] = parts[0]
        rest = parts[1:]
        if len(rest) >= 2:
            out["current_job_title"], out["current_company"] = rest[0], rest[1]
            out["headline"] = " - ".join(rest)
        elif len(rest) == 1:
            out["headline"] = rest[0]
            m = re.match(r"(.+?)\s+(?:at|@)\s+(.+)$", rest[0], flags=re.I)
            if m:
                out["current_job_title"], out["current_company"] = m.group(1).strip(), m.group(2).strip()

    snip = snippet or ""
    m = re.search(r"Experience:\s*([^·|]+)", snip)
    if m and not out["current_company"]:
        out["current_company"] = m.group(1).strip()
    m = re.search(r"Location:\s*([^·|]+)", snip)
    if m:
        out["location"] = m.group(1).strip()
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
class EmploymentProvider:
    """PROVIDER_MODE: chocodata_then_serper (default) | serper_only | chocodata_only"""

    def lookup(self, record):
        mode = config.PROVIDER_MODE
        if mode == "serper_only":
            return lookup_serper(record)
        try:
            return _from_chocodata(fetch_raw(record.get("linkedin_url")), record)
        except ProviderError as exc:
            # Never fall back on auth/quota/config problems: surface them.
            if mode == "chocodata_only" or exc.status == "restricted":
                raise
            if not config.SERPER_API_KEY:
                raise
            try:
                return lookup_serper(record)
            except ProviderError as serper_exc:
                if serper_exc.status == "restricted":
                    raise
                raise ProviderError(
                    f"ChocoData: {exc} | Serper: {serper_exc}",
                    retryable=exc.retryable or serper_exc.retryable,
                    status=serper_exc.status if not exc.retryable else exc.status,
                )