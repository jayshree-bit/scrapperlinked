"""Parse an Experience entry copied from a LinkedIn profile page.

Handles the single-role form:

    Engineer - Cloud & Infra Management
    LTM · Full-time
    Sep 2025 - Present · 1 yr 2 mos
    Hybrid

and the grouped form (one company, several roles):

    LTM
    1 yr 6 mos
    Cloud & Infrastructure Management
    Full-time
    May 2025 - Present · 1 yr 6 mos
    Bengaluru, Karnataka, India · Hybrid

Only the first role that has a date line is used (the top one, which is the
current role on LinkedIn). Pasting the whole Experience section is fine.
"""
import re

from app.provider import _EMP_TYPES, _WORK_MODES, _duration_text, _to_ym

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DATE_LINE = re.compile(
    rf"^((?:{_MONTH}\s+)?\d{{4}})\s*[-–]\s*(Present|(?:{_MONTH}\s+)?\d{{4}})\s*(?:·\s*(.+))?$", re.I)
_DURATION_ONLY = re.compile(r"^(?:\d+\s*yrs?|\d+\s*mos?|less than a year)(?:\s+\d+\s*mos?)?$", re.I)
# 'Bengaluru, Karnataka, India' / 'Mumbai Metropolitan Region' / 'Remote'
_PLACE = re.compile(r"^[A-Z][A-Za-z .'()-]*(?:,\s*[A-Za-z][A-Za-z .'()-]*)*$")
_SKIP = re.compile(r"^(experience|.* logo|show all.*|…?\s*(see )?more|\d+ endorsements?)$", re.I)


def _emp_type(text):
    return next((t for t in _EMP_TYPES if t.casefold() == text.strip().casefold()), None)


def parse_experience_text(text):
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    lines = [l for l in lines if l and not _SKIP.match(l)]
    # LinkedIn sometimes repeats each line for screen readers: 'LTM\nLTM'
    deduped = []
    for l in lines:
        if not deduped or deduped[-1] != l:
            deduped.append(l)
    lines = deduped

    d = next((i for i, l in enumerate(lines) if _DATE_LINE.match(l)), None)
    if d is None:
        raise ValueError("No date line found (expected something like 'Sep 2025 - Present · 1 yr 2 mos').")

    m = _DATE_LINE.match(lines[d])
    start_txt, end_txt, duration = m.group(1), m.group(2), (m.group(3) or "").strip() or None
    is_present = end_txt.lower() == "present"
    if not duration:
        start, end = _to_ym(start_txt), (None if is_present else _to_ym(end_txt))
        if start:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            duration = _duration_text(start, end or (now.year, now.month))

    # Lines above the date line belong to this role (and its company header).
    before = lines[:d]
    title = company = emp_type = company_duration = None
    rest = []
    for l in before:
        if _DURATION_ONLY.match(l):
            company_duration = l
            continue
        parts = [p.strip() for p in l.split("·")]
        if len(parts) > 1 and _emp_type(parts[-1]):
            emp_type = _emp_type(parts[-1])
            rest.append(("company", " · ".join(parts[:-1])))
            continue
        if len(parts) > 1 and _DURATION_ONLY.match(parts[-1]):   # 'LTM · 1 yr 6 mos'
            company_duration = parts[-1]
            head = " · ".join(parts[:-1])
            if _emp_type(head):                                   # 'Full-time · 1 yr 5 mos'
                emp_type = _emp_type(head)
            else:
                rest.append(("header", head))
            continue
        if _emp_type(l):
            emp_type = _emp_type(l)
            continue
        rest.append(("text", l))

    if company_duration is not None:
        # Grouped: company header first, then the role title (closest to dates).
        header = next((v for k, v in rest if k in ("header", "text", "company")), None)
        company = header
        title = next((v for k, v in reversed(rest) if k == "text" and v != header), None)
    else:
        company = next((v for k, v in rest if k == "company"), None)
        texts = [v for k, v in rest if k == "text"]
        if company:
            title = texts[-1] if texts else None
        elif len(texts) >= 2:
            title, company = texts[-2], texts[-1]
        elif texts:
            title = texts[0]

    # Lines right after the date line: 'Bengaluru, Karnataka, India · Hybrid' / 'Hybrid'
    location = work_mode = None
    for l in lines[d + 1:d + 3]:
        if _DATE_LINE.match(l) or len(l) > 80:
            break
        kept = []
        for p in [p.strip() for p in l.split("·") if p.strip()]:
            mode = _WORK_MODES.get(p.lower())
            if mode:
                work_mode = work_mode or mode
            elif _PLACE.match(p) and len(p) <= 80:
                kept.append(p)
        if kept and not location:
            location = ", ".join(kept)
        if not kept and not _WORK_MODES.get(l.lower()):
            break

    dates = f"{start_txt} - {'Present' if is_present else end_txt}"
    return {
        "current_job_title": title,
        "current_company": company,
        "employment_type": emp_type,
        "employment_dates": dates,
        "employment_duration": duration,
        "employment_start_date": f"{dates} · {duration}" if duration else dates,
        "company_duration": company_duration,
        "job_location": location,
        "work_mode": work_mode,
        "is_current": is_present,
    }
