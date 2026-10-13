import re
from urllib.parse import urlparse

ALIASES = {
    "prospect_id": ["prospect_id", "prospect id", "id", "_id"],
    "full_name": ["full_name", "full name", "name"],
    "linkedin_url": ["linkedin_url", "linkedin url", "profile url", "linkedin profile url",
                     "linkedin", "url", "profile", "link", "profile link", "linkedin link",
                     "linkedin_profile", "linkedin profile"],
    "current_company": ["current_company", "existing company", "company"],
    "current_job_title": ["current_job_title", "existing job title", "job title", "title"],
}


def clean_url(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in ("https", "http") or parsed.netloc.lower() not in {
        "linkedin.com", "www.linkedin.com", "m.linkedin.com"
    }:
        return None
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if not path.lower().startswith("/in/") or len(path.split("/")) < 3:
        return None
    return "https://www.linkedin.com" + path + "/"


def normalize_record(record):
    lowered = {str(k).strip().lower(): v for k, v in record.items()}
    out = {}
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if alias in lowered and lowered[alias] is not None:
                out[canonical] = str(lowered[alias]).strip()
                break
        out.setdefault(canonical, None)

    url = clean_url(out.get("linkedin_url"))
    if not url:  # fallback: any cell that looks like a LinkedIn profile URL
        for v in record.values():
            if v and "linkedin.com/in/" in str(v).lower():
                url = clean_url(str(v).strip())
                if url:
                    break
    out["linkedin_url"] = url

    for k in ("prospect_id", "full_name", "current_company", "current_job_title"):
        if out.get(k) in ("", "None", "nan"):
            out[k] = None
    return out
