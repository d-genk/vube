#!/usr/bin/env python3
"""
collect_jobs.py

Reads the shared ledger, downloads the artifacts of every job that has finished, and
marks those records COLLECTED in place.

The ledger (see job_ledger.py) is both the pending queue and the permanent record: a
record enters as SUBMITTED and stays in the file for good, its status advancing as the
job progresses. Nothing is ever deleted, so the ledger remains the provenance record of
which source keys went into which job -- which matters here, because these jobs run with
delete_data set and the source copies are gone after processing.

    SUBMITTED   -> COLLECTED    artifacts downloaded to --out-dir
    SUBMITTED   -> JOB_FAILED   the API reported the job as failed

Jobs still running are left as SUBMITTED, so the script is safe to re-run as often as
you like -- each pass collects whatever has finished since the last one.

Safety properties, in the order they matter:

- A record advances to COLLECTED only after every artifact for that job is written to
  disk. A partial or failed download leaves it SUBMITTED for the next run.
- Each record is updated as soon as its job is collected, not batched to the end of the
  run, so an interrupted run cannot re-download work it already has.
- The ledger is rewritten atomically (temp file + os.replace) and re-read at each update,
  so concurrently appended lines survive. Even so, prefer not to run the submitter and
  collector simultaneously.

Example:

    python collect_jobs.py --out-dir ./output --email you@example.org
"""

import os
import sys
import getpass
import argparse

import requests

try:
    from submit_job import (
        login,
        print_status,
        filename_from_content_disposition,
        sanitize_filename,
        unique_path,
        ARTIFACT_SUFFIXES,
        DEFAULT_API_URL,
    )
    import job_ledger
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    print("[!] Ensure submit_job.py and job_ledger.py are in the current directory.")
    sys.exit(1)

DEFAULT_OUT_DIR = "./output"

TERMINAL_FAILURE_STATES = {"FAILED", "ERROR"}


def fetch_job(api_url, token, job_id):
    """Return (status, job_data, error). status is None when the job could not be read."""
    try:
        resp = requests.get(f"{api_url}/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    except requests.exceptions.RequestException as e:
        return None, None, f"network error: {e}"

    if resp.status_code == 404:
        return None, None, "job not found (404)"
    if resp.status_code == 403:
        return None, None, "job belongs to another account (403)"
    if not resp.ok:
        return None, None, f"status check failed: {resp.status_code} {resp.text[:200]}"

    try:
        data = resp.json()
    except ValueError:
        return None, None, "status response was not valid JSON"

    return (data.get("status") or "").upper(), data, None


def download_artifacts(artifacts, out_dir, job_title, job_id):
    """Download every artifact for one job.

    Returns (filenames, errors). Unlike submit_job.download_artifacts, a failed download
    is reported back to the caller -- this script advances ledger records on success, so
    it cannot treat a failure as a no-op.
    """
    os.makedirs(out_dir, exist_ok=True)
    basename = (job_title or "").strip() or job_id or "artifact"

    filenames, errors = [], []

    for key, info in artifacts.items():
        if not isinstance(info, dict) or "presigned_url" not in info:
            continue
        url = info["presigned_url"]
        if not url:
            continue

        try:
            resp = requests.get(url, stream=True, timeout=120)
        except requests.exceptions.RequestException as e:
            errors.append(f"{key}: network error: {e}")
            continue

        if not resp.ok:
            errors.append(f"{key}: HTTP {resp.status_code}")
            continue

        # Prefer the name the aggregator stamped, so CLI downloads match the UI.
        filename = filename_from_content_disposition(resp.headers.get("Content-Disposition"))
        if not filename:
            filename = f"{basename}{ARTIFACT_SUFFIXES.get(key, f'_{key}')}"
        filename = sanitize_filename(filename, fallback=f"{job_id or 'artifact'}_{key}")
        filepath = unique_path(out_dir, filename)

        try:
            with open(filepath, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
        except Exception as e:
            errors.append(f"{key}: write failed: {e}")
            # A half-written file must not be mistaken for a good artifact.
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            continue

        print_status(f"    saved {os.path.basename(filepath)}")
        filenames.append(os.path.basename(filepath))

    return filenames, errors


def apply_update(ledger_path, job_id, patch, description):
    """Write one status change back to the ledger, reporting failure loudly."""
    try:
        job_ledger.update_records(ledger_path, {job_id: patch})
        return True
    except Exception as e:
        print(f"[!]     could not update ledger for {job_id} ({description}): {e}")
        return False


def resolve_api_url(pending, override):
    """The API each job was submitted to, from the ledger unless overridden."""
    if override:
        return override

    api_urls = {r.get("api_url") for r in pending if r.get("api_url")}
    if len(api_urls) == 1:
        return api_urls.pop()
    if len(api_urls) > 1:
        print(f"[!] Ledger mixes multiple api_url values: {sorted(api_urls)}")
        print("[!] Re-run with --api-url to choose one explicitly.")
        sys.exit(1)
    return DEFAULT_API_URL


def main():
    parser = argparse.ArgumentParser(
        description="Download artifacts for completed jobs and advance their ledger records."
    )
    parser.add_argument("--ledger", default=job_ledger.DEFAULT_LEDGER,
                        help=f"JSON Lines ledger written by the submitters (default: {job_ledger.DEFAULT_LEDGER})")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help=f"Directory to save downloaded artifacts (default: {DEFAULT_OUT_DIR})")
    parser.add_argument("--job-id", action="append", dest="job_ids",
                        help="Collect only this job ID (repeatable); default is every pending job")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report each job's status without downloading or touching the ledger")

    parser.add_argument("--email", help="Archivault account email")
    parser.add_argument("--password", help="Archivault account password (prompted if omitted)")
    parser.add_argument("--api-url", default=None,
                        help="Base Archivault API URL (default: the api_url recorded in the ledger)")

    args = parser.parse_args()

    if not os.path.exists(args.ledger):
        print(f"[!] Ledger '{args.ledger}' does not exist. Nothing to collect.")
        sys.exit(1)

    records, malformed = job_ledger.read_ledger(args.ledger)
    if malformed:
        print(f"[!] Warning: {malformed} malformed line(s) in '{args.ledger}' were ignored and left in place.")

    pending = job_ledger.pending_jobs(records)
    if args.job_ids:
        wanted = set(args.job_ids)
        pending = [r for r in pending if r["job_id"] in wanted]

    if not pending:
        print_status(f"No pending jobs in '{args.ledger}'. Nothing to collect.")
        return

    print_status(f"{len(pending)} pending job(s) in '{args.ledger}'.")

    api_url = resolve_api_url(pending, args.api_url)
    print_status(f"Using API {api_url}")

    email = args.email or input("Email: ").strip()
    if not email:
        print("[!] Error: Email is required.")
        sys.exit(1)
    password = args.password or getpass.getpass("Password: ")
    if not password:
        print("[!] Error: Password is required.")
        sys.exit(1)

    token = login(api_url, email, password)

    collected, failed_jobs, still_running, errored = [], [], [], []

    for index, record in enumerate(pending, start=1):
        job_id = record["job_id"]
        title = record.get("job_title") or job_id
        print_status(f"[{index}/{len(pending)}] {title} ({job_id})")

        status, data, error = fetch_job(api_url, token, job_id)
        if error:
            print(f"[!]     {error}")
            errored.append((record, error))
            continue

        if status in TERMINAL_FAILURE_STATES:
            job_error = (data or {}).get("error", "unknown error")
            print(f"[!]     job {status}: {job_error}")
            failed_jobs.append((record, job_error))
            if not args.dry_run:
                apply_update(args.ledger, job_id, {
                    "status": job_ledger.STATUS_JOB_FAILED,
                    "error": job_error,
                    "failed_at": job_ledger.utc_now(),
                }, "marking failed")
            continue

        if status != "COMPLETED":
            print_status(f"    still processing (status {status or 'UNKNOWN'})")
            still_running.append(record)
            continue

        artifacts = (data or {}).get("artifacts") or {}
        if not artifacts:
            print("[!]     COMPLETED but returned no artifacts; leaving as SUBMITTED.")
            errored.append((record, "completed with no artifacts"))
            continue

        if args.dry_run:
            print_status(f"    would download {len(artifacts)} artifact(s) to '{args.out_dir}'")
            collected.append((record, []))
            continue

        filenames, download_errors = download_artifacts(artifacts, args.out_dir, title, job_id)
        if download_errors or not filenames:
            for message in download_errors:
                print(f"[!]     {message}")
            print("[!]     download incomplete; leaving as SUBMITTED for the next run.")
            errored.append((record, "; ".join(download_errors) or "no artifacts downloaded"))
            continue

        # Advance the record immediately, so an interrupted run never re-downloads
        # artifacts it already has on disk.
        apply_update(args.ledger, job_id, {
            "status": job_ledger.STATUS_COLLECTED,
            "collected_at": job_ledger.utc_now(),
            "artifact_files": filenames,
            "out_dir": os.path.abspath(args.out_dir),
        }, "marking collected")
        collected.append((record, filenames))

    # Summary.
    print_status(
        f"Collected {len(collected)}, still running {len(still_running)}, "
        f"job failures {len(failed_jobs)}, errors {len(errored)}."
    )
    if collected and not args.dry_run:
        print_status(f"Artifacts saved to '{os.path.abspath(args.out_dir)}'.")
    if failed_jobs:
        print("[!] Jobs the API reports as failed (recorded JOB_FAILED):")
        for record, job_error in failed_jobs:
            print(f"    {record.get('job_title')}  {record['job_id']}  -- {job_error}")
        print("[!] Their issues stay claimed. Re-run a submitter with --retry-failed to")
        print("[!] make them eligible again.")
    if errored:
        print("[!] Jobs left as SUBMITTED after an error (will retry next run):")
        for record, message in errored:
            print(f"    {record.get('job_title')}  {record['job_id']}  -- {message}")


if __name__ == "__main__":
    main()
