import argparse
import json
import logging
import random
import time

from app import config
from app.db import Database
from app.io_excel import read_records, export_report
from app.logging_setup import setup_logging
from app.normalize import normalize_record
from app.provider import EmploymentProvider, ProviderError, fetch_raw

log = logging.getLogger("verifier")


def import_file(db, path):
    records = read_records(path)
    counts = {"inserted": 0, "existing": 0, "invalid": 0}
    for record in records:
        counts[db.enqueue(record)] += 1
    log.info("Import complete: %s", counts)


def import_mongo(db):
    count = 0
    for raw in db.input.find({}):
        raw.pop("_id", None)
        record = normalize_record(raw)
        if record.get("prospect_id") or record.get("linkedin_url"):
            db.enqueue(record)
            count += 1
    log.info("Enqueued %s records from MongoDB input collection", count)


def _pause(seconds, stop_event=None):
    """Sleep with +/-30% jitter so requests don't go out on a fixed beat."""
    seconds = max(0.0, seconds * random.uniform(0.7, 1.3))
    if stop_event is not None:
        return stop_event.wait(seconds)
    time.sleep(seconds)
    return False


def process(db, limit=None, batch_id=None, stop_event=None, use_socialcrawl=False):
    """Work through the queue one record at a time. Returns why it stopped."""
    provider = EmploymentProvider(use_socialcrawl=use_socialcrawl)
    processed = 0
    consecutive_errors = 0
    reason = "Queue empty: all records processed"
    while limit is None or processed < limit:
        if stop_event is not None and stop_event.is_set():
            reason = "Stopped by user"
            break
        record = db.claim_one(batch_id)
        if not record:
            break
        try:
            fields = provider.lookup(record)
            old_company = (record.get("input_company") or "").strip().casefold()
            old_title = (record.get("input_job_title") or "").strip().casefold()
            new_company = (fields.get("current_company") or "").strip().casefold()
            new_title = (fields.get("current_job_title") or "").strip().casefold()
            fields["employment_changed"] = bool(
                (new_company and old_company and new_company != old_company)
                or (new_title and old_title and new_title != old_title)
            )
            db.finish(record.get("prospect_id"), record.get("linkedin_url"), fields)
            consecutive_errors = 0
            log.info("Processed %s status=%s", record.get("linkedin_url"), fields["verification_status"])
        except ProviderError as exc:
            will_retry = db.fail(record, exc.status, str(exc), retry=exc.retryable)
            log.warning("Provider error %s retryable=%s will_retry=%s: %s",
                        record.get("linkedin_url"), exc.retryable, will_retry, exc)
            if exc.status == "restricted" or "rate limit" in str(exc).lower():
                log.error("Stopping batch: restricted / rate-limited / auth problem.")
                reason = f"Stopped to protect the account/API: {exc}"[:500]
                break
            consecutive_errors += 1
            if consecutive_errors >= config.MAX_CONSECUTIVE_ERRORS:
                log.error("Stopping batch: %d provider errors in a row. Check the provider "
                          "(try `probe <url>`), then run `requeue` and `process` again.",
                          consecutive_errors)
                reason = f"Stopped after {consecutive_errors} provider errors in a row: {exc}"[:500]
                break
            if exc.retryable:
                attempts = record.get("attempts", 0)
                delay = min(config.RETRY_BASE_SECONDS * (2 ** attempts), config.RETRY_MAX_SECONDS)
                _pause(delay, stop_event)
        except Exception as exc:
            db.fail(record, "needs_review", f"Unexpected error: {exc}", retry=False)
            log.exception("Unexpected processing error")
        processed += 1
        if _pause(config.REQUEST_DELAY_SECONDS, stop_event):
            reason = "Stopped by user"
            break
    else:
        reason = f"Reached limit of {limit} records"
    log.info("Processing run finished. Processed this run: %d (%s)", processed, reason)
    return {"processed": processed, "reason": reason}


def process_apify(db, limit=None, batch_id=None, stop_event=None):
    """Like process(), but sends APIFY_BATCH_SIZE profiles per Apify run
    (each run has a start fee, so batching keeps the cost per profile low)."""
    from app.provider import apify_fetch_many, from_apify
    processed = 0
    reason = "Queue empty: all records processed"
    while limit is None or processed < limit:
        if stop_event is not None and stop_event.is_set():
            reason = "Stopped by user"
            break
        size = config.APIFY_BATCH_SIZE if limit is None else min(config.APIFY_BATCH_SIZE, limit - processed)
        claimed = []
        for _ in range(size):
            rec = db.claim_one(batch_id)
            if not rec:
                break
            claimed.append(rec)
        if not claimed:
            break
        urls = [r.get("linkedin_url") for r in claimed]
        try:
            results = apify_fetch_many(urls)
        except ProviderError as exc:
            for rec in claimed:
                db.fail(rec, exc.status, str(exc), retry=exc.retryable)
            log.error("Apify run failed for %d records: %s", len(claimed), exc)
            reason = f"Stopped: {exc}"[:500]
            break
        for rec in claimed:
            try:
                fields = from_apify(results.get(rec.get("linkedin_url")), rec)
                db.finish(rec.get("prospect_id"), rec.get("linkedin_url"), fields)
            except ProviderError as exc:
                db.fail(rec, exc.status, str(exc), retry=False)
            processed += 1
        log.info("Apify run: %d profiles (%d total this run)", len(claimed), processed)
        if _pause(config.REQUEST_DELAY_SECONDS, stop_event):
            reason = "Stopped by user"
            break
    else:
        reason = f"Reached limit of {limit} records"
    log.info("Apify processing finished. Processed this run: %d (%s)", processed, reason)
    return {"processed": processed, "reason": reason}


def process_mindcase(db, limit=None, batch_id=None, stop_event=None):
    """Like process_apify(): MINDCASE_BATCH_SIZE profiles per Mindcase call."""
    from app.provider import mindcase_fetch_many, from_mindcase
    processed = 0
    reason = "Queue empty: all records processed"
    while limit is None or processed < limit:
        if stop_event is not None and stop_event.is_set():
            reason = "Stopped by user"
            break
        size = config.MINDCASE_BATCH_SIZE if limit is None else min(config.MINDCASE_BATCH_SIZE, limit - processed)
        claimed = []
        for _ in range(size):
            rec = db.claim_one(batch_id)
            if not rec:
                break
            claimed.append(rec)
        if not claimed:
            break
        urls = [r.get("linkedin_url") for r in claimed]
        try:
            results = mindcase_fetch_many(urls)
        except ProviderError as exc:
            for rec in claimed:
                db.fail(rec, exc.status, str(exc), retry=exc.retryable)
            log.error("Mindcase call failed for %d records: %s", len(claimed), exc)
            reason = f"Stopped: {exc}"[:500]
            break
        for rec in claimed:
            try:
                fields = from_mindcase(results.get(rec.get("linkedin_url")), rec)
                db.finish(rec.get("prospect_id"), rec.get("linkedin_url"), fields)
            except ProviderError as exc:
                db.fail(rec, exc.status, str(exc), retry=False)
            processed += 1
        log.info("Mindcase run: %d profiles (%d total this run)", len(claimed), processed)
        if _pause(config.REQUEST_DELAY_SECONDS, stop_event):
            reason = "Stopped by user"
            break
    else:
        reason = f"Reached limit of {limit} records"
    log.info("Mindcase processing finished. Processed this run: %d (%s)", processed, reason)
    return {"processed": processed, "reason": reason}


def status(db):
    """Print queue counts."""
    print("total", db.output.count_documents({}))
    for st in ["pending", "processing", "retry", "completed", "failed"]:
        print(st, db.output.count_documents({"task_status": st}))
    print("verified", db.output.count_documents(
        {"task_status": "completed", "verification_status": "verified"}))


def probe_mindcase(url):
    """Call Mindcase once and print the raw row (no DB writes). Use this to see
    the real field names if a column comes back empty."""
    from app.provider import mindcase_fetch_many
    print(json.dumps(mindcase_fetch_many([url]).get(url), indent=2, default=str)[:6000])


def probe(url):
    """Call the provider once and print the raw JSON (no DB writes)."""
    print(json.dumps(fetch_raw(url), indent=2)[:4000])


def review(db, limit=None):
    """Manual review: opens each queued profile in your logged-in browser,
    and you type in the details you see. Nothing is extracted automatically."""
    from playwright.sync_api import sync_playwright

    fields_to_ask = [
        ("full_name", "Full name"),
        ("current_job_title", "Current job title"),
        ("current_company", "Current company"),
        ("employment_start_date", "Start date"),
        ("previous_job_title", "Previous job title"),
        ("previous_company", "Previous company"),
        ("industry", "Industry"),
        ("location", "Location"),
    ]
    done = 0
    skipped = set()
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(config.BROWSER_PROFILE_DIR, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            while limit is None or done < limit:
                record = db.claim_one()
                if not record:
                    break
                url = record.get("linkedin_url")
                if url in skipped:  # only skipped records are left
                    db.release(record)
                    break
                print(f"\n[{done + 1}] {url}")
                if url:
                    page.goto(url, wait_until="domcontentloaded")
                answer = input("Enter=fill details, s=skip, q=quit: ").strip().lower()
                if answer in ("s", "q"):
                    db.release(record)
                    if answer == "q":
                        break
                    skipped.add(url)
                    continue
                values = {}
                for field, label in fields_to_ask:
                    values[field] = input(f"{label}: ").strip() or None
                values.update({
                    "source": "manual_review",
                    "verification_status": "verified" if values.get("current_company") else "needs_review",
                    "error_message": None,
                })
                db.finish(record.get("prospect_id"), url, values)
                done += 1
        finally:
            ctx.close()
    log.info("Reviewed %d records", done)


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Employment verification queue")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import-file"); p.add_argument("path", nargs="?", default=config.INPUT_FILE)
    sub.add_parser("import-mongo")
    p = sub.add_parser("process"); p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("review"); p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("probe"); p.add_argument("url")
    p = sub.add_parser("probe-mindcase"); p.add_argument("url")
    sub.add_parser("status")
    sub.add_parser("requeue")
    sub.add_parser("requeue-all")
    p = sub.add_parser("export"); p.add_argument("--output", default=config.OUTPUT_FILE)
    args = parser.parse_args()

    db = Database()
    if args.command == "import-file":
        import_file(db, args.path)
    elif args.command == "import-mongo":
        import_mongo(db)
    elif args.command == "process":
        process(db, args.limit)
    elif args.command == "review":
        review(db, args.limit)
    elif args.command == "probe-mindcase":
        probe_mindcase(args.url)
    elif args.command == "status":
        status(db)
    elif args.command == "probe":
        probe(args.url)
    elif args.command == "requeue":
        log.info("Requeued %d records (failed/retry/processing only)", db.requeue_all())
    elif args.command == "requeue-all":
        log.info("Requeued %d records (including completed)", db.requeue_everything())
    elif args.command == "export":
        count = export_report(db.output, args.output)
        log.info("Exported %d records to %s", count, args.output)


if __name__ == "__main__":
    main()