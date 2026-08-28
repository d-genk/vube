#!/usr/bin/env python3
"""
submit_issue_batch.py

Submits a specified number of randomly selected unprocessed issues, recording each one
in the shared ledger so a collector script can pick up the finished artifacts later.

Batch jobs turn around in hours, so submission and collection are necessarily two
separate runs. This script owns the first half: choose issues, submit them, and leave
behind enough information that the second half needs no memory of this process.

Eligibility and bookkeeping both live in the ledger (see job_ledger.py) -- one JSON
Lines file holding one record per issue, whose status is updated in place as the job
progresses. An issue is eligible when it has no ledger record, or only a SUBMIT_FAILED
one. There is no longer a separate processed-issues text file; a legacy one is still
read as an extra exclusion source if present.

Example:

    python submit_issue_batch.py         --source-bucket my-source-bucket         --key-prefix collections/periodicals         --profile vube-source         --email you@example.org         --count 25
"""

# python submit_issue_batch.py --source-bucket vubp-image-mls-825428742173-us-east-1-an --key-prefix Daniel --profile vube --email daniel.genkins@gmail.com --count 100

import sys
import time
import random
import argparse

try:
    from submit_job import login
    from submit_issue_job import (
        add_job_arguments,
        build_issue_index,
        resolve_credentials,
        submit_issue,
        ledger_record,
        print_status,
    )
    import job_ledger
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    print("[!] Ensure submit_job.py, submit_issue_job.py and job_ledger.py are present.")
    sys.exit(1)

# One 402 (out of credits) or a bad token fails every remaining issue identically, so
# a run that is failing consistently is stopped rather than churning through the list.
MAX_CONSECUTIVE_FAILURES = 3


def main():
    parser = argparse.ArgumentParser(
        description="Submit N randomly selected unprocessed issues and record each submission."
    )
    add_job_arguments(parser)
    parser.add_argument("--count", "-n", type=int, default=1,
                        help="Number of unprocessed issues to submit (default: 1)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to pause between submissions (default: 0)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed the random selection, for a reproducible run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the issues that would be submitted and exit without submitting")

    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1.")
    if len(args.transcription_instructions) > 500:
        parser.error("--transcription-instructions must be at most 500 characters.")
    if args.seed is not None:
        random.seed(args.seed)

    # 1. Index the bucket, then subtract everything already spoken for.
    issues, _ = build_issue_index(args.source_bucket, args.key_prefix, args.profile)
    if not issues:
        print(f"[!] No page images found under prefix '{args.key_prefix}' in bucket '{args.source_bucket}'.")
        sys.exit(1)

    _records, excluded = job_ledger.load_exclusions(
        args.ledger, args.legacy_processed_file, retry_failed=args.retry_failed
    )
    unprocessed = sorted(set(issues) - excluded)
    if not unprocessed:
        print_status("Every issue under this prefix has already been processed. Nothing to do.")
        sys.exit(0)

    # random.sample gives N distinct issues; no need to guard against repeats.
    count = min(args.count, len(unprocessed))
    if count < args.count:
        print_status(f"Only {count} unprocessed issue(s) available; requested {args.count}.")
    selected = random.sample(unprocessed, count)

    print_status(f"{len(unprocessed)} issue(s) unprocessed; submitting {count}.")

    if args.dry_run:
        print_status("Dry run -- not submitting. Issues that would be submitted:")
        for issue in selected:
            print(f"    {issue}  ({len(issues[issue])} page(s))")
        return

    # 2. Authenticate once for the whole run.
    email, password = resolve_credentials(args)
    token = login(args.api_url, email, password)

    # 3. Submit each issue, recording the outcome before moving on.
    submitted = []
    failed = []
    consecutive_failures = 0

    for index, issue in enumerate(selected, start=1):
        keys = issues[issue]
        print_status(f"[{index}/{count}] Submitting '{issue}' ({len(keys)} page(s))...")

        job_id = None
        error = None
        try:
            job_id = submit_issue(args, token, issue, keys)
        except SystemExit as e:
            # submit_job.py exits the process on an API error. In a batch run that
            # would discard the remaining issues, so it is caught and treated as a
            # single failed submission.
            error = f"submission aborted (exit code {e.code})"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        if job_id:
            job_ledger.append_record(
                args.ledger, ledger_record(args, issue, keys, job_id, job_ledger.STATUS_SUBMITTED)
            )
            submitted.append((issue, job_id))
            consecutive_failures = 0
            print_status(f"[{index}/{count}] Submitted as job {job_id}.")
        else:
            print(f"[!] [{index}/{count}] Failed to submit '{issue}': {error}")
            # SUBMIT_FAILED does not claim the issue -- nothing reached the API, so it
            # stays eligible for a later run.
            job_ledger.append_record(
                args.ledger,
                ledger_record(args, issue, keys, None, job_ledger.STATUS_SUBMIT_FAILED, error=error),
            )
            failed.append((issue, error))
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[!] Aborting run after {consecutive_failures} consecutive failures.")
                print("[!] Check credit balance and credentials before retrying.")
                break

        if args.delay and index < count:
            time.sleep(args.delay)

    # 4. Summary.
    print_status(f"Run complete: {len(submitted)} submitted, {len(failed)} failed.")
    for issue, job_id in submitted:
        print(f"    {job_id}  {issue}")
    if failed:
        print("[!] Failed issues (recorded SUBMIT_FAILED; they remain eligible for a later run):")
        for issue, error in failed:
            print(f"    {issue}  -- {error}")

    if submitted:
        print_status(f"Ledger '{args.ledger}' updated; run collect_jobs.py once the jobs finish.")

    sys.exit(1 if failed and not submitted else 0)


if __name__ == "__main__":
    main()
