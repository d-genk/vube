#!/usr/bin/env python3
"""
job_ledger.py

The single record of every issue this pipeline has touched.

One JSON Lines file (default: submitted_jobs.jsonl) holds one record per issue. The
record is created at submission and updated in place as the job moves through its
lifecycle, so the ledger answers both questions the pipeline asks:

    "which issues have already been dealt with?"   -> every record in the file
    "which jobs still need collecting?"            -> records with status SUBMITTED

This replaces the earlier split between processed_issues.txt and a separate manifest.
The text file held a strict subset of what the ledger already records, and keeping the
two in sync was a failure mode with no upside.

## Statuses

    SUBMITTED       job accepted by the API, artifacts not yet downloaded
    COLLECTED       artifacts downloaded; terminal success
    JOB_FAILED      the API reported the job as failed; terminal, will never complete
    SUBMIT_FAILED   submission itself failed; the issue never reached the API

An issue is eligible for submission when it has no record, or its only records are
SUBMIT_FAILED -- nothing reached the API, so nothing was billed. JOB_FAILED is not
retried by default (the job consumed credits and the failure may well repeat), but
--retry-failed opts into it.

## Concurrency

Appends are atomic-ish (one line, flushed and fsynced). Updates rewrite the whole file
via a temp file and os.replace, re-reading it first so that concurrently appended lines
survive and unparseable lines pass through untouched. That makes an interrupted run safe,
but it is not a substitute for locking -- do not run two scripts against one ledger
simultaneously.

## Migration

Ledger records are keyed on `issue`, and the default filename is unchanged from the
earlier manifest, so an existing submitted_jobs.jsonl is already a valid ledger. A
legacy processed_issues.txt, if present, is still read as an additional exclusion source
so issues completed under the old scheme are never re-submitted. It is never written to,
and can be deleted once you are satisfied the ledger covers everything.
"""

import os
import json
import datetime
import tempfile

DEFAULT_LEDGER = "submitted_jobs.jsonl"
LEGACY_PROCESSED_FILE = "processed_issues.txt"

RECORD_VERSION = 1

STATUS_SUBMITTED = "SUBMITTED"
STATUS_COLLECTED = "COLLECTED"
STATUS_JOB_FAILED = "JOB_FAILED"
STATUS_SUBMIT_FAILED = "SUBMIT_FAILED"

# Statuses that make an issue ineligible for resubmission. SUBMIT_FAILED is absent by
# design: nothing reached the API, so the issue is free to try again.
CLAIMED_STATUSES = {STATUS_SUBMITTED, STATUS_COLLECTED, STATUS_JOB_FAILED}

# Dropped from CLAIMED_STATUSES when --retry-failed is given.
RETRYABLE_ON_REQUEST = {STATUS_JOB_FAILED}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_ledger(path):
    """Return (records, malformed_count). A missing ledger is empty, not an error."""
    records = []
    malformed = 0

    if not path or not os.path.exists(path):
        return records, malformed

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1

    return records, malformed


def claimed_issues(records, retry_failed=False):
    """Issues that must not be submitted again."""
    blocking = set(CLAIMED_STATUSES)
    if retry_failed:
        blocking -= RETRYABLE_ON_REQUEST

    return {r["issue"] for r in records if r.get("issue") and r.get("status") in blocking}


def pending_jobs(records):
    """Records awaiting collection, in ledger order."""
    return [r for r in records if r.get("status") == STATUS_SUBMITTED and r.get("job_id")]


def legacy_processed_issues(path=LEGACY_PROCESSED_FILE):
    """Issues recorded by the superseded processed_issues.txt, read only.

    Kept so that issues completed before the ledger existed stay excluded. Nothing
    writes to this file any more.
    """
    issues = set()
    if not path or not os.path.exists(path):
        return issues

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            val = line.strip()
            if val and not val.startswith("#"):
                issues.add(val)

    return issues


def build_record(issue, job_title, keys, status, job_id=None, error=None, **extra):
    """Create a ledger record. `extra` carries submission context (api_url, bucket, model)."""
    record = {
        "record_version": RECORD_VERSION,
        "status": status,
        "job_id": job_id,
        "job_title": job_title,
        "issue": issue,
        "submitted_at": utc_now(),
        "page_count": len(keys),
        "keys": list(keys),
        "error": error,
    }
    record.update(extra)
    return record


def append_record(path, record):
    """Append one record, flushed and fsynced before returning.

    The record is the only durable pointer to a submitted job, so it is written
    immediately rather than buffered until the end of a run.
    """
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception as e:
        print(f"[!] Warning: could not write ledger record to '{path}': {e}")
        print("[!] Record the following manually or the job cannot be tracked:")
        print(f"[!]   {json.dumps(record, ensure_ascii=False)}")
        return False


def update_records(path, updates):
    """Merge field updates into records, keyed by job ID. Returns the number updated.

    Rewrites the ledger atomically. The file is re-read here rather than written back
    from a caller's snapshot, so lines appended since the caller loaded it survive, and
    unparseable lines are passed through verbatim.
    """
    if not updates:
        return 0

    lines, updated = [], 0

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                lines.append(stripped)
                continue

            patch = updates.get(record.get("job_id"))
            if patch:
                record.update(patch)
                updated += 1
                lines.append(json.dumps(record, ensure_ascii=False))
            else:
                lines.append(stripped)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

    return updated


def load_exclusions(ledger_path, legacy_path=LEGACY_PROCESSED_FILE, retry_failed=False, verbose=True):
    """Every issue that should be kept out of a new submission run."""
    records, malformed = read_ledger(ledger_path)
    if malformed and verbose:
        print(f"[!] Warning: skipped {malformed} malformed line(s) in '{ledger_path}'.")

    claimed = claimed_issues(records, retry_failed=retry_failed)
    legacy = legacy_processed_issues(legacy_path)

    if verbose:
        print(f"[*] Ledger '{ledger_path}' holds {len(records)} record(s); {len(claimed)} issue(s) claimed.")
        if legacy:
            print(f"[*] Legacy '{legacy_path}' contributes {len(legacy - claimed)} additional issue(s).")

    return records, claimed | legacy
