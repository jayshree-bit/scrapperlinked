"""Web UI: upload an Excel/CSV of LinkedIn URLs, run the lookup, view and
download the current-employment results.

    python -m app.web          ->  http://127.0.0.1:5000

All lookups go through the ChocoData / Serper APIs (their servers fetch the
data, not this machine), one record at a time with jittered delays, and the
run stops on its own on rate limits or repeated errors.
"""
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from io import BytesIO

import requests
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from app import config
from app.cli import process, process_apify, process_mindcase
from app.db import Database, utcnow
from app.io_excel import export_report, read_records
from app.logging_setup import setup_logging
from app.paste_parse import parse_experience_text
from app.provider import (ProviderError, lookup_socialcrawl, apify_fetch_many, from_apify,
                          mindcase_fetch_many, from_mindcase)

log = logging.getLogger("verifier")

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")
ALLOWED = (".xlsx", ".xlsm", ".xls", ".csv")
ROW_FIELDS = [
    "linkedin_url", "full_name", "current_job_title", "current_company",
    "employment_type", "employment_dates", "employment_duration", "employment_start_date",
    "job_location", "work_mode", "location", "headline", "previous_job_title",
    "previous_company", "source", "verification_status", "task_status",
    "error_message", "checked_at", "industry", "company_duration", "updated_at",
]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB
db = None


class Worker:
    """One background run at a time, so requests never go out in parallel."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.state = {"running": False, "batch_id": None, "started_at": None,
                      "processed": 0, "message": "Idle"}

    def start(self, batch_id, limit=None, source="free"):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            # Records left 'processing' by a crash/restart go back in the queue.
            db.output.update_many({"batch_ids": batch_id, "task_status": "processing"},
                                  {"$set": {"task_status": "pending"}})
            self.stop_event.clear()
            self.state = {"running": True, "batch_id": batch_id,
                          "started_at": utcnow().isoformat(), "processed": 0,
                          "message": "Running..."}
            self.thread = threading.Thread(target=self._run, args=(batch_id, limit, source),
                                           daemon=True)
            self.thread.start()
            return True

    def _run(self, batch_id, limit, source):
        try:
            if source == "mindcase":
                result = process_mindcase(db, limit=limit, batch_id=batch_id, stop_event=self.stop_event)
            elif source == "apify":
                result = process_apify(db, limit=limit, batch_id=batch_id, stop_event=self.stop_event)
            else:
                result = process(db, limit=limit, batch_id=batch_id, stop_event=self.stop_event,
                                 use_socialcrawl=source == "socialcrawl")
            self.state.update(processed=result["processed"], message=result["reason"])
        except Exception as exc:  # surface in the UI instead of dying silently
            log.exception("Worker crashed")
            self.state["message"] = f"Error: {exc}"
        finally:
            self.state["running"] = False

    def stop(self):
        self.stop_event.set()


worker = Worker()


def _batch_counts(batch_id):
    q = {"batch_ids": batch_id}
    counts = {"total": db.output.count_documents(q)}
    for st in ("pending", "processing", "retry", "completed", "failed"):
        counts[st] = db.output.count_documents({**q, "task_status": st})
    counts["verified"] = db.output.count_documents({**q, "verification_status": "verified"})
    return counts


def _jsonable(doc):
    out = {}
    for k in ROW_FIELDS:
        v = doc.get(k)
        if isinstance(v, datetime):
            v = v.replace(tzinfo=v.tzinfo or timezone.utc).isoformat()
        out[k] = v
    return out


@app.before_request
def _require_login():
    """Optional HTTP Basic auth. Set WEB_PASSWORD in .env before exposing the
    app on the network (any user: the username is ignored)."""
    password = os.getenv("WEB_PASSWORD")
    if not password:
        return None
    auth = request.authorization
    if auth and auth.password == password:
        return None
    return ("Login required", 401, {"WWW-Authenticate": 'Basic realm="Prospect verifier"'})


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/upload")
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(error="Choose an Excel or CSV file first."), 400
    name = secure_filename(f.filename) or "upload.xlsx"
    if not name.lower().endswith(ALLOWED):
        return jsonify(error="File must be .xlsx, .xls, .xlsm or .csv"), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    batch_id = uuid.uuid4().hex[:12]
    path = os.path.join(UPLOAD_DIR, f"{batch_id}_{name}")
    f.save(path)
    try:
        records = read_records(path)
    except Exception as exc:
        return jsonify(error=f"Could not read the file: {exc}"), 400
    if not records:
        return jsonify(error="No LinkedIn profile URLs (linkedin.com/in/...) found in the file."), 400

    counts = {"inserted": 0, "existing": 0, "invalid": 0}
    for rec in records:
        counts[db.enqueue(rec, batch_id=batch_id)] += 1
    db.batches.insert_one({"_id": batch_id, "filename": f.filename,
                           "uploaded_at": utcnow(), "import": counts})
    log.info("Uploaded %s as batch %s: %s", f.filename, batch_id, counts)
    return jsonify(batch_id=batch_id, filename=f.filename, imported=counts,
                   counts=_batch_counts(batch_id))


@app.get("/api/batches")
def batches():
    out = []
    for b in db.batches.find().sort("uploaded_at", -1).limit(50):
        out.append({"batch_id": b["_id"], "filename": b["filename"],
                    "uploaded_at": b["uploaded_at"].replace(tzinfo=timezone.utc).isoformat(),
                    "counts": _batch_counts(b["_id"])})
    return jsonify(out)


@app.get("/api/batches/<batch_id>")
def batch_detail(batch_id):
    q = {"batch_ids": batch_id}
    status = request.args.get("status")
    if status == "verified":
        q["verification_status"] = "verified"
    elif status:
        q["task_status"] = status
    page = max(1, int(request.args.get("page", 1)))
    size = min(500, max(10, int(request.args.get("size", 100))))
    cursor = (db.output.find(q, {"_id": 0, "raw_experience": 0})
              .sort([("checked_at", -1), ("linkedin_url", 1)])
              .skip((page - 1) * size).limit(size))
    return jsonify(counts=_batch_counts(batch_id), worker=worker.state,
                   rows=[_jsonable(d) for d in cursor],
                   matching=db.output.count_documents(q), page=page, size=size)


@app.post("/api/batches/<batch_id>/start")
def start(batch_id):
    body = request.get_json(silent=True) or {}
    limit = body.get("limit")
    limit = int(limit) if limit not in (None, "", 0, "0") else None
    source = body.get("source") or "free"
    if source not in ("free", "apify", "socialcrawl", "mindcase"):
        return jsonify(error="Unknown source"), 400
    if source == "apify" and not config.APIFY_API_TOKEN:
        return jsonify(error="APIFY_API_TOKEN is not set in .env"), 400
    if source == "mindcase" and not config.MINDCASE_API_KEY:
        return jsonify(error="MINDCASE_API_KEY is not set in .env"), 400
    if source == "socialcrawl" and not config.SOCIALCRAWL_API_KEY:
        return jsonify(error="SOCIALCRAWL_API_KEY is not set in .env"), 400
    if not worker.start(batch_id, limit, source=source):
        return jsonify(error="A run is already in progress. Stop it first."), 409
    return jsonify(worker.state)


@app.post("/api/stop")
def stop():
    worker.stop()
    return jsonify(ok=True)


@app.post("/api/batches/<batch_id>/retry-failed")
def retry_failed(batch_id):
    res = db.output.update_many(
        {"batch_ids": batch_id, "$or": [
            {"task_status": {"$in": ["failed", "retry"]}},
            {"task_status": "completed", "verification_status": "needs_review"},
            {"task_status": "completed", "current_job_title": None},
        ]},
        {"$set": {"task_status": "pending", "attempts": 0, "updated_at": utcnow()}})
    return jsonify(requeued=res.modified_count)


@app.post("/api/batches/<batch_id>/requeue-no-dates")
def requeue_no_dates(batch_id):
    """Put finished rows that have no start date back in the queue, so a
    run with Apify / SocialCrawl can fill them. Rows filled by hand are kept."""
    res = db.output.update_many(
        {"batch_ids": batch_id, "task_status": "completed", "employment_dates": None,
         "source": {"$ne": "manual_paste"}},
        {"$set": {"task_status": "pending", "attempts": 0, "updated_at": utcnow()}})
    return jsonify(requeued=res.modified_count)


@app.post("/api/records/paste")
def paste_details():
    """Save details the user copied from the profile's Experience section."""
    body = request.get_json(silent=True) or {}
    url = body.get("linkedin_url")
    doc = db.output.find_one({"linkedin_url": url}) if url else None
    if not doc:
        return jsonify(error="Record not found."), 404
    try:
        fields = parse_experience_text(body.get("text", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    fields.pop("is_current", None)
    fields = {k: v for k, v in fields.items() if v}
    fields.update(source="manual_paste", error_message=None,
                  verification_status="verified" if fields.get("current_company") else "needs_review")
    db.finish(doc.get("prospect_id"), url, fields)
    return jsonify(fields)


def _socialcrawl_credits():
    doc = db.output.find_one({"credits_remaining": {"$ne": None}, "source": "socialcrawl"},
                             {"credits_remaining": 1}, sort=[("checked_at", -1)])
    return (doc or {}).get("credits_remaining")


@app.get("/api/socialcrawl")
def socialcrawl_info():
    apify_usage = apify_limit = None
    if config.APIFY_API_TOKEN:
        try:
            lim = requests.get("https://api.apify.com/v2/users/me/limits",
                               params={"token": config.APIFY_API_TOKEN}, timeout=10).json().get("data", {})
            apify_usage = (lim.get("current") or {}).get("monthlyUsageUsd")
            apify_limit = (lim.get("limits") or {}).get("maxMonthlyUsageUsd")
        except Exception:
            pass
    return jsonify(enabled=bool(config.SOCIALCRAWL_API_KEY), credits_remaining=_socialcrawl_credits(),
                   apify_enabled=bool(config.APIFY_API_TOKEN),
                   mindcase_enabled=bool(config.MINDCASE_API_KEY),
                   apify_usage_usd=apify_usage, apify_limit_usd=apify_limit)


@app.post("/api/records/socialcrawl")
def socialcrawl_one():
    """Fetch full experience (dates, type, work mode) for one row. 5 credits."""
    body = request.get_json(silent=True) or {}
    url = body.get("linkedin_url")
    doc = db.output.find_one({"linkedin_url": url}) if url else None
    if not doc:
        return jsonify(error="Record not found."), 404
    try:
        fields = lookup_socialcrawl(doc)
    except ProviderError as exc:
        return jsonify(error=str(exc)), 400
    db.finish(doc.get("prospect_id"), url, fields)
    return jsonify(fields)


@app.post("/api/records/apify")
def apify_one():
    """Fetch experience (type, start date, duration) for one row via Apify.
    Costs one Apify run (~$0.006 + start fee). Only fills fields Apify returns,
    so existing values (e.g. from SocialCrawl or Paste) are never blanked."""
    body = request.get_json(silent=True) or {}
    url = body.get("linkedin_url")
    doc = db.output.find_one({"linkedin_url": url}) if url else None
    if not doc:
        return jsonify(error="Record not found."), 404
    if not config.APIFY_API_TOKEN:
        return jsonify(error="APIFY_API_TOKEN is not set in .env"), 400
    try:
        item = apify_fetch_many([url]).get(url)
        fields = from_apify(item, doc)
    except ProviderError as exc:
        return jsonify(error=str(exc)), 400
    fields = {k: v for k, v in fields.items() if v is not None}
    db.finish(doc.get("prospect_id"), url, fields)
    return jsonify(fields)


@app.post("/api/records/mindcase")
def mindcase_one():
    """Fetch experience (type, start date, duration) for one row via Mindcase
    (~0.4 cents). Only fills fields Mindcase returns, so existing values from
    SocialCrawl / Apify / Paste are never blanked."""
    body = request.get_json(silent=True) or {}
    url = body.get("linkedin_url")
    doc = db.output.find_one({"linkedin_url": url}) if url else None
    if not doc:
        return jsonify(error="Record not found."), 404
    try:
        fields = from_mindcase(mindcase_fetch_many([url]).get(url), doc)
    except ProviderError as exc:
        return jsonify(error=str(exc)), 400
    fields = {k: v for k, v in fields.items() if v is not None}
    db.finish(doc.get("prospect_id"), url, fields)
    return jsonify(fields)


@app.get("/api/batches/<batch_id>/export")
def export(batch_id):
    b = db.batches.find_one({"_id": batch_id}) or {}
    base = os.path.splitext(b.get("filename") or "prospects")[0]
    buf = BytesIO()
    export_report(db.output, buf, query={"batch_ids": batch_id})
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=f"{base}_results.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/worker")
def worker_state():
    return jsonify(worker.state)


def main():
    global db
    setup_logging()
    db = Database()
    port = int(os.getenv("WEB_PORT", "3000"))
    host = os.getenv("WEB_HOST", "0.0.0.0")  # 0.0.0.0 = reachable from other machines
    if host not in ("127.0.0.1", "localhost") and not os.getenv("WEB_PASSWORD"):
        log.warning("Web UI is open to the network WITHOUT a password. Set WEB_PASSWORD in .env.")
    log.info("Web UI on http://%s:%d (provider mode: %s)", host, port, config.PROVIDER_MODE)
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()