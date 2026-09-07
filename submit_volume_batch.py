#!/usr/bin/env python3
"""
submit_volume_batch.py

Submits a specified number of randomly selected unprocessed volumes, recording each one
in the shared ledger so a collector script can pick up the finished artifacts later.

Batch jobs turn around in hours, so submission and collection are necessarily two
separate runs. This script owns the first half: choose volumes, submit them, and leave
behind enough information that the second half needs no memory of this process.

A volume is one [prefix]/YYYY_X_PPPP folder, submitted as a single job covering every
page of every issue in it. Eligibility and bookkeeping both live in the ledger (see
job_ledger.py) -- one JSON Lines file holding one record per volume, whose status is
updated in place as the job progresses. A volume is eligible when it has no ledger
record, or only a SUBMIT_FAILED one.

## Concurrency

Gemini's batch API caps concurrent batches per key at 100, and a job occupies roughly
one batch slot at a time (caption, then transcribe). So --count is effectively the
concurrency dial: keep it comfortably under 100 if other work shares the key.

Example:

    python submit_volume_batch.py         --source-bucket my-source-bucket         --key-prefix collections/periodicals         --profile vube-source         --email you@example.org         --count 25
"""

# python submit_volume_batch.py --source-bucket vubp-image-mls-825428742173-us-east-1-an --key-prefix Daniel --profile vube --email daniel.genkins@gmail.com --count 50

import sys
import time
import random
import argparse

try:
    from submit_job import login
    from submit_volume_job import (
        add_job_arguments,
        build_volume_index,
        resolve_credentials,
        submit_volume,
        ledger_record,
        print_status,
    )
    import job_ledger
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    print("[!] Ensure submit_job.py, submit_volume_job.py and job_ledger.py are present.")
    sys.exit(1)

# One 402 (out of credits) or a bad token fails every remaining volume identically, so
# a run that is failing consistently is stopped rather than churning through the list.
MAX_CONSECUTIVE_FAILURES = 3


def main():
    parser = argparse.ArgumentParser(
        description="Submit N randomly selected unprocessed volumes and record each submission."
    )
    add_job_arguments(parser)
    parser.add_argument("--count", "-n", type=int, default=1,
                        help="Number of unprocessed volumes to submit (default: 1)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to pause between submissions (default: 0)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed the random selection, for a reproducible run")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the volumes that would be submitted and exit without submitting")

    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be at least 1.")
    if len(args.transcription_instructions) > 500:
        parser.error("--transcription-instructions must be at most 500 characters.")
    if args.seed is not None:
        random.seed(args.seed)

    # 1. Index the bucket, then subtract everything already spoken for.
    volumes, _ = build_volume_index(args.source_bucket, args.key_prefix, args.profile)
    if not volumes:
        print(f"[!] No page images found under prefix '{args.key_prefix}' in bucket '{args.source_bucket}'.")
        sys.exit(1)

    _records, excluded = job_ledger.load_exclusions(args.ledger, retry_failed=args.retry_failed)
    unprocessed = sorted(set(volumes) - excluded)
    if not unprocessed:
        print_status("Every volume under this prefix has already been processed. Nothing to do.")
        sys.exit(0)

    # random.sample gives N distinct volumes; no need to guard against repeats.
    count = min(args.count, len(unprocessed))
    if count < args.count:
        print_status(f"Only {count} unprocessed volume(s) available; requested {args.count}.")
    selected = random.sample(unprocessed, count)

    print_status(f"{len(unprocessed)} volume(s) unprocessed; submitting {count}.")

    if args.dry_run:
        print_status("Dry run -- not submitting. Volumes that would be submitted:")
        for volume in selected:
            print(f"    {volume}  ({len(volumes[volume])} page(s))")
        return

    # 2. Authenticate once for the whole run.
    email, password = resolve_credentials(args)
    token = login(args.api_url, email, password)

    # 3. Submit each volume, recording the outcome before moving on.
    submitted = []
    failed = []
    consecutive_failures = 0

    for index, volume in enumerate(selected, start=1):
        keys = volumes[volume]
        print_status(f"[{index}/{count}] Submitting '{volume}' ({len(keys)} page(s))...")

        job_id = None
        error = None
        try:
            job_id = submit_volume(args, token, volume, keys)
        except SystemExit as e:
            # submit_job.py exits the process on an API error. In a batch run that
            # would discard the remaining volumes, so it is caught and treated as a
            # single failed submission.
            error = f"submission aborted (exit code {e.code})"
        except Exception as e:
            error = f"{type(e).__name__}: {e}"

        if job_id:
            job_ledger.append_record(
                args.ledger, ledger_record(args, volume, keys, job_id, job_ledger.STATUS_SUBMITTED)
            )
            submitted.append((volume, job_id))
            consecutive_failures = 0
            print_status(f"[{index}/{count}] Submitted as job {job_id}.")
        else:
            print(f"[!] [{index}/{count}] Failed to submit '{volume}': {error}")
            # SUBMIT_FAILED does not claim the volume -- nothing reached the API, so it
            # stays eligible for a later run.
            job_ledger.append_record(
                args.ledger,
                ledger_record(args, volume, keys, None, job_ledger.STATUS_SUBMIT_FAILED, error=error),
            )
            failed.append((volume, error))
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"[!] Aborting run after {consecutive_failures} consecutive failures.")
                print("[!] Check credit balance and credentials before retrying.")
                break

        if args.delay and index < count:
            time.sleep(args.delay)

    # 4. Summary.
    print_status(f"Run complete: {len(submitted)} submitted, {len(failed)} failed.")
    for volume, job_id in submitted:
        print(f"    {job_id}  {volume}")
    if failed:
        print("[!] Failed volumes (recorded SUBMIT_FAILED; they remain eligible for a later run):")
        for volume, error in failed:
            print(f"    {volume}  -- {error}")

    if submitted:
        print_status(f"Ledger '{args.ledger}' updated; run collect_jobs.py once the jobs finish.")

    sys.exit(1 if failed and not submitted else 0)


if __name__ == "__main__":
    main()
