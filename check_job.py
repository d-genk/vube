#!/usr/bin/env python3
"""
check_job.py

Prints the full server-side record for one or more jobs. Read-only: it never writes to
the ledger, downloads anything, or changes job state.

collect_jobs.py reports only what it needs to decide whether to collect. When a job is
not behaving, the useful detail is in the rest of the record -- `progress`, `error`,
`n_images`, `pdf_count`, `skip_note` -- so this dumps the whole thing.

Job IDs come from the ledger, or are given explicitly:

    python check_job.py --job-id 4a8473b9-8af1-43f0-a167-0a8d537931f6 --email you@example.org
    python check_job.py --pending --email you@example.org
"""

import sys
import json
import getpass
import argparse

import requests

try:
    from submit_job import login, print_status, DEFAULT_API_URL
    import job_ledger
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Print the server-side record for one or more jobs.")
    parser.add_argument("--job-id", action="append", dest="job_ids", help="Job ID to inspect (repeatable)")
    parser.add_argument("--pending", action="store_true",
                        help="Inspect every job still marked SUBMITTED in the ledger")
    parser.add_argument("--ledger", default=job_ledger.DEFAULT_LEDGER,
                        help=f"Ledger to read job IDs from (default: {job_ledger.DEFAULT_LEDGER})")
    parser.add_argument("--email", help="Archivault account email")
    parser.add_argument("--password", help="Archivault account password (prompted if omitted)")
    parser.add_argument("--api-url", default=None, help="Base Archivault API URL")
    parser.add_argument("--raw", action="store_true", help="Print the full JSON record, not a summary")

    args = parser.parse_args()

    records, _ = job_ledger.read_ledger(args.ledger)
    by_id = {r["job_id"]: r for r in records if r.get("job_id")}

    job_ids = list(args.job_ids or [])
    if args.pending:
        job_ids += [r["job_id"] for r in job_ledger.pending_jobs(records) if r["job_id"] not in job_ids]

    if not job_ids:
        parser.error("Give --job-id and/or --pending.")

    api_url = args.api_url
    if not api_url:
        urls = {by_id[j].get("api_url") for j in job_ids if j in by_id and by_id[j].get("api_url")}
        api_url = urls.pop() if len(urls) == 1 else DEFAULT_API_URL

    email = args.email or input("Email: ").strip()
    password = args.password or getpass.getpass("Password: ")
    if not email or not password:
        print("[!] Error: Email and password are required.")
        sys.exit(1)

    token = login(api_url, email, password)
    headers = {"Authorization": f"Bearer {token}"}

    for job_id in job_ids:
        local = by_id.get(job_id, {})
        print()
        print("=" * 78)
        print(f"job_id     {job_id}")
        print(f"title      {local.get('job_title', '(not in ledger)')}")
        print(f"issue      {local.get('issue', '-')}")
        print(f"ledger     {local.get('status', '-')}  submitted {local.get('submitted_at', '-')}"
              f"  pages {local.get('page_count', '-')}")
        print("-" * 78)

        try:
            resp = requests.get(f"{api_url}/jobs/{job_id}", headers=headers, timeout=30)
        except requests.exceptions.RequestException as e:
            print(f"[!] network error: {e}")
            continue

        if not resp.ok:
            print(f"[!] HTTP {resp.status_code}: {resp.text[:500]}")
            continue

        data = resp.json()

        if args.raw:
            print(json.dumps(data, indent=2, default=str))
            continue

        # Artifacts carry long presigned URLs that bury everything else; summarize them.
        for field in ("status", "job_title", "createdAt", "updatedAt", "n_images",
                      "pdf_count", "error", "skip_note", "batch_mode", "steps"):
            if field in data:
                print(f"{field:<12} {data[field]}")

        progress = data.get("progress")
        if progress:
            print(f"{'progress':<12} {json.dumps(progress)}")

        artifacts = data.get("artifacts")
        if artifacts:
            keys = [k for k in artifacts if isinstance(artifacts[k], dict)]
            print(f"{'artifacts':<12} {keys or list(artifacts)}")

        extra = sorted(set(data) - {
            "status", "job_title", "createdAt", "updatedAt", "n_images", "pdf_count",
            "error", "skip_note", "batch_mode", "steps", "progress", "artifacts",
        })
        if extra:
            print(f"{'other keys':<12} {extra}")
            print("             (re-run with --raw to see them)")

    print()


if __name__ == "__main__":
    main()
