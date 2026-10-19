from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING, ReturnDocument
from app import config


def utcnow():
    return datetime.now(timezone.utc)


def _key(record):
    if record.get("prospect_id"):
        return {"prospect_id": record["prospect_id"]}
    return {"linkedin_url": record.get("linkedin_url")}


class Database:
    def __init__(self):
        self.client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=8000)
        self.client.admin.command("ping")
        db = self.client[config.MONGODB_DATABASE]
        self.input = db[config.MONGODB_INPUT_COLLECTION]
        self.output = db[config.MONGODB_OUTPUT_COLLECTION]
        self.output.create_index([("prospect_id", ASCENDING)], unique=True, sparse=True)
        self.output.create_index([("linkedin_url", ASCENDING)], unique=True, sparse=True)
        self.output.create_index([("verification_status", ASCENDING), ("updated_at", ASCENDING)])
        self.output.create_index([("task_status", ASCENDING), ("updated_at", ASCENDING)])
        self.output.create_index([("batch_ids", ASCENDING)])
        self.batches = db["upload_batches"]

    def enqueue(self, record, batch_id=None):
        url = (record.get("linkedin_url") or "").strip()
        pid = record.get("prospect_id")
        if not pid and not url:
            return "invalid"

        key = {"prospect_id": pid} if pid else {"linkedin_url": url}
        now = utcnow()

        on_insert = {
            "task_status": "pending",
            "verification_status": "needs_review",
            "error_message": None,
            "attempts": 0,
            "created_at": now,
            "updated_at": now,
        }
        to_set = {}
        if url:
            to_set["linkedin_url"] = url
        if record.get("full_name"):
            to_set["full_name"] = record["full_name"]
        if record.get("current_company"):
            to_set["input_company"] = record["current_company"]
        if record.get("current_job_title"):
            to_set["input_job_title"] = record["current_job_title"]

        update = {"$setOnInsert": on_insert}
        if to_set:
            update["$set"] = to_set
        if batch_id:
            update["$addToSet"] = {"batch_ids": batch_id}

        result = self.output.update_one(key, update, upsert=True)
        return "inserted" if result.upserted_id else "existing"

    def claim_one(self, batch_id=None):
        query = {"task_status": {"$in": ["pending", "retry"]}}
        if batch_id:
            query["batch_ids"] = batch_id
        return self.output.find_one_and_update(
            query,
            {"$set": {"task_status": "processing", "updated_at": utcnow()}},
            sort=[("updated_at", 1)],
            return_document=ReturnDocument.AFTER,
        )

    def release(self, record):
        """Put a claimed record back in the queue."""
        self.output.update_one(_key(record), {"$set": {"task_status": "pending"}})

    def finish(self, prospect_id, linkedin_url, fields):
        key = {"prospect_id": prospect_id} if prospect_id else {"linkedin_url": linkedin_url}
        fields = dict(fields)
        for k in ("prospect_id", "linkedin_url", "full_name"):
            if fields.get(k) is None:
                fields.pop(k, None)
        now = utcnow()
        fields.update({"updated_at": now, "checked_at": now, "task_status": "completed"})
        self.output.update_one(key, {"$set": fields}, upsert=True)

    def fail(self, record, status, message, retry=False):
        """Record a failure. Retryable errors are retried up to MAX_RETRIES
        attempts, then the record is marked failed for good."""
        doc = self.output.find_one_and_update(
            _key(record),
            {"$inc": {"attempts": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        attempts = (doc or {}).get("attempts", 1)
        will_retry = retry and attempts < config.MAX_RETRIES
        text = str(message)[:1000]
        if retry and not will_retry:
            text = f"Gave up after {attempts} attempts: {text}"[:1000]
        self.output.update_one(_key(record), {"$set": {
            "task_status": "retry" if will_retry else "failed",
            "verification_status": status,
            "error_message": text,
            "updated_at": utcnow(),
        }})
        return will_retry

    def requeue_all(self):
        """Requeue only records that still need work. Completed records are
        left alone so you don't re-fetch (and re-pay for) them."""
        res = self.output.update_many(
            {"task_status": {"$in": ["failed", "retry", "processing"]}},
            {"$set": {"task_status": "pending", "attempts": 0, "updated_at": utcnow()}},
        )
        return res.modified_count

    def requeue_everything(self):
        """Explicit full reset, including completed records."""
        res = self.output.update_many(
            {}, {"$set": {"task_status": "pending", "attempts": 0, "updated_at": utcnow()}}
        )
        return res.modified_count