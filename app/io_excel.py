from io import StringIO

import pandas as pd
from app.normalize import normalize_record, clean_url


def read_records(path):
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        frame = pd.read_excel(path, dtype=str).fillna("")
    elif path.lower().endswith(".csv"):
        frame = pd.read_csv(path, dtype=str).fillna("")
    else:
        raise ValueError("Input must be .csv, .xlsx, .xlsm, or .xls")
    records, seen = [], set()
    for row in frame.to_dict(orient="records"):
        rec = normalize_record(row)
        dedupe_key = rec.get("prospect_id") or rec.get("linkedin_url")
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        records.append(rec)
    return records


def read_connections(path):
    """Read LinkedIn's own 'Connections.csv' data export."""
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    start = next((i for i, l in enumerate(lines) if "URL" in l and "First Name" in l), 0)
    frame = pd.read_csv(StringIO("\n".join(lines[start:])), dtype=str).fillna("")
    out = {}
    for r in frame.to_dict("records"):
        url = clean_url(r.get("URL"))
        if not url:
            continue
        out[url] = {
            "full_name": f"{r.get('First Name', '')} {r.get('Last Name', '')}".strip() or None,
            "current_company": r.get("Company") or None,
            "current_job_title": r.get("Position") or None,
        }
    return out


def _position_summary(row):
    """One-cell summary, e.g.
    'Co-Founder & CTO · Bytestrone · Aug 2021 - Present · 5 yrs 3 mos'"""
    parts = [
        row.get("current_job_title"),
        row.get("current_company"),
        row.get("employment_start_date"),
    ]
    cleaned = [str(p).strip() for p in parts if not pd.isna(p) and str(p).strip()]
    return " · ".join(cleaned)


def export_report(collection, output_path):
    rows = list(collection.find({}, {"_id": 0}).sort("updated_at", -1))
    df = pd.DataFrame(rows)
    columns = [
        "linkedin_url", "full_name", "current_job_title", "current_company",
        "employment_start_date", "previous_job_title", "previous_company",
        "industry", "location", "source", "verification_status",
        "checked_at", "task_status", "error_message",
    ]
    for col in columns:
        if col not in df.columns:
            df[col] = None
    report = df[columns].copy()

    # Combined column: title · company · dates/duration
    report.insert(
        report.columns.get_loc("current_job_title"),
        "current_position",
        report.apply(_position_summary, axis=1),
    )
    # To keep ONLY the combined column, uncomment:
    # report = report.drop(columns=["current_job_title", "current_company", "employment_start_date"])

    report["checked_at"] = (
        pd.to_datetime(report["checked_at"], errors="coerce", utc=True)
        .dt.strftime("%Y-%m-%d %H:%M")
        .fillna("")
    )

    summary = [
        ("Total records", len(report)),
        ("Verified (manual review)", int((report["verification_status"] == "verified").sum())),
        ("Failed records", int((report["task_status"] == "failed").sum())),
        ("Needs review", int((report["verification_status"] == "needs_review").sum())),
        ("Pending / processing / retry", int(report["task_status"].isin(["pending", "processing", "retry"]).sum())),
    ]
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        report.to_excel(writer, sheet_name="Prospect Details", index=False)
        pd.DataFrame(summary, columns=["Metric", "Count"]).to_excel(writer, sheet_name="Summary", index=False)
        for ws in writer.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 12), 60)
    return len(report)