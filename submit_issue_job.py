#!/usr/bin/env python3
"""
submit_issue_job.py

Submits one issue's worth of pre-processed page images from S3 to the Archivault
pipeline as a single transcription job.

Source keys take the form:

    [prefix]/YYYY_X_PPPP/PPPP_YYYYMMDD_FFFFFFF_NNNN.jpeg

An "issue" is every object sharing the [prefix]/YYYY_X_PPPP/PPPP_YYYYMMDD_FFFFFFF
stem, i.e. all NNNN pages of one issue. The script lists the bucket, subtracts the
issues already claimed in the ledger (see job_ledger.py), picks one of the remainder at
random, and submits its pages in a single job. A ledger record is written only after
the API has accepted the submission.

Images are copied server-side from the source bucket, so nothing is downloaded here;
the only reason boto3 credentials are needed is to enumerate keys. Reciprocal bucket
and Lambda permissions are assumed to already be in place.

Example:

    python submit_issue_job.py \
        --source-bucket my-source-bucket \
        --key-prefix collections/periodicals \
        --profile vube-source \
        --email you@example.org
"""

import os
import re
import sys
import random
import argparse
import getpass

import boto3
from botocore.exceptions import BotoCoreError, ClientError

try:
    from submit_job import (
        login,
        submit_job,
        print_status,
        DEFAULT_API_URL,
        DEFAULT_METADATA_SCHEMA,
        MODEL_OPTIONS,
    )
    import job_ledger
except ImportError as e:
    print(f"[!] Error: Could not import a required local module ({e}).")
    print("[!] Ensure submit_job.py and job_ledger.py are in the current directory.")
    sys.exit(1)

DEFAULT_TRANSCRIPTION_MODEL = "gemini-3.1-pro-preview"
DEFAULT_TRANSCRIPTION_INSTRUCTIONS = "Transcribe the long s as s rather than f."

# PPPP_YYYYMMDD_FFFFFFF_NNNN.jpeg -- the stem (everything but the page number and
# extension) is both the grouping key and the job title. Anything in the bucket that
# does not match this shape is not a page image and is skipped.
PAGE_FILENAME_RE = re.compile(r"^(?P<stem>\d{4}_\d{8}_\d{7})_\d{4}\.jpe?g$", re.IGNORECASE)


def build_issue_index(bucket, key_prefix, profile=None):
    """List the bucket and group page keys by issue.

    Returns (issues, skipped_count) where issues maps
    '[prefix]/YYYY_X_PPPP/PPPP_YYYYMMDD_FFFFFFF' -> sorted list of full page keys.
    """
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    issues = {}
    skipped = 0
    total = 0

    print_status(f"Listing s3://{bucket}/{key_prefix} ...")
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                total += 1
                head, _, filename = key.rpartition("/")
                match = PAGE_FILENAME_RE.match(filename)
                if not head or not match:
                    skipped += 1
                    continue
                issue = f"{head}/{match.group('stem')}"
                issues.setdefault(issue, []).append(key)
    except (BotoCoreError, ClientError) as e:
        print(f"[!] Error listing bucket '{bucket}': {e}")
        sys.exit(1)

    for keys in issues.values():
        keys.sort()

    print_status(
        f"Scanned {total} object(s): {len(issues)} issue(s), {skipped} non-page object(s) skipped."
    )
    return issues, skipped


def add_job_arguments(parser):
    """Add the arguments shared by every script that submits an issue.

    Kept here so submit_issue_job.py and its batch wrapper cannot drift apart on
    defaults -- build_metadata() below reads this exact namespace.
    """
    parser.add_argument("--source-bucket", required=True, help="S3 bucket holding the pre-processed page images")
    parser.add_argument("--key-prefix", required=True, help="Key prefix under which the target objects live")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"),
                        help="AWS profile with read access to the source bucket "
                             "(default: $AWS_PROFILE, else the default credential chain)")
    parser.add_argument("--ledger", default=job_ledger.DEFAULT_LEDGER,
                        help=f"JSON Lines ledger of every issue touched (default: {job_ledger.DEFAULT_LEDGER})")
    parser.add_argument("--legacy-processed-file", default=job_ledger.LEGACY_PROCESSED_FILE,
                        help="Superseded processed_issues.txt, still read as an extra exclusion source")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Treat issues whose job failed as eligible again")

    parser.add_argument("--email", help="Archivault account email")
    parser.add_argument("--password", help="Archivault account password (prompted if omitted)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base Archivault API URL")

    parser.add_argument("--country", default="GB", help="Country of origin (default: GB)")
    parser.add_argument("--state", default="", help="State/Province")
    parser.add_argument("--description", default="", help="Job description")
    parser.add_argument("--writing-style", default="printed", help="Writing style (default: printed)")
    parser.add_argument("--language", default="english", help="Language (default: english)")
    parser.add_argument("--time-period", default="19th_century_or_earlier",
                        help="Time period (default: 19th_century_or_earlier)")
    parser.add_argument("--layout-structure", default="", help="Layout structure")
    parser.add_argument("--transcription-model", default=DEFAULT_TRANSCRIPTION_MODEL,
                        choices=MODEL_OPTIONS["transcription"],
                        help=f"Transcription model (default: {DEFAULT_TRANSCRIPTION_MODEL})")
    parser.add_argument("--transcription-instructions", default=DEFAULT_TRANSCRIPTION_INSTRUCTIONS,
                        help="Custom transcription instructions, max 500 chars "
                             f"(default: \"{DEFAULT_TRANSCRIPTION_INSTRUCTIONS}\")")
    return parser


def resolve_credentials(args):
    """Prompt for anything the caller did not supply on the command line."""
    email = args.email or input("Email: ").strip()
    if not email:
        print("[!] Error: Email is required.")
        sys.exit(1)

    password = args.password or getpass.getpass("Password: ")
    if not password:
        print("[!] Error: Password is required.")
        sys.exit(1)

    return email, password


def issue_title(issue):
    """PPPP_YYYYMMDD_FFFFFFF -- the trailing stem is the job title."""
    return issue.rsplit("/", 1)[-1]


def ledger_record(args, issue, keys, job_id, status, error=None):
    """Build the ledger record for one submission attempt.

    Defined here rather than in job_ledger.py because the submission context it captures
    comes from this module's argument namespace.
    """
    return job_ledger.build_record(
        issue=issue,
        job_title=issue_title(issue),
        keys=keys,
        status=status,
        job_id=job_id,
        error=error,
        api_url=args.api_url,
        source_bucket=args.source_bucket,
        key_prefix=args.key_prefix,
        steps=["transcribe"],
        transcription_model=args.transcription_model,
        batch_mode=True,
        delete_data=True,
    )


def submit_issue(args, token, issue, keys):
    """Submit one issue as a transcription-only batch job. Returns its job ID."""
    title = issue_title(issue)
    job_id, _artifacts, _upload_duration, _inference_duration, _final_title = submit_job(
        api_url=args.api_url,
        token=token,
        directory=None,
        files_to_upload=[],
        title=title,
        steps=["transcribe"],
        country=args.country,
        state=args.state,
        description=args.description,
        metadata=build_metadata(args),
        source_bucket=args.source_bucket,
        keys=keys,
        batch_mode=True,
    )
    return job_id


def build_metadata(args):
    """Job metadata for a transcription-only batch job.

    Mirrors the defaults in submit_job.py; only the fields this pipeline actually
    cares about are overridden.
    """
    return {
        "writing_style": args.writing_style,
        "language": args.language,
        "time_period": args.time_period,
        "layout_structure": args.layout_structure,
        "transcription_model": args.transcription_model,
        "captioning_model": "gemini-3.5-flash-lite",
        "foliation_model": "gemini-3.7-flash",
        "aggregation_model": "gemini-3.5-flash-lite",
        "metadata_model": "gemini-3.7-flash",
        "ner_model": "gemini-3.7-flash",
        "non_textual_elements": [],
        "transcription_preferences": {
            "expand_abbreviations": False,
            "preserve_line_breaks": True,
            "retain_punctuation_and_spelling": True,
            "normalize_to_modern_language": False,
            "ignore_marginalia": False,
        },
        "metadata_schema": DEFAULT_METADATA_SCHEMA,
        "additional_context_file": "",
        "additional_context_modules": [
            "foliation", "metadata", "transcription", "ner", "aggregation", "captioning", "layout"
        ],
        "foliation_file": "",
        "foliation_override_discrete": False,
        "allow_subject_similarity": False,
        "delete_data": True,
        "batch_mode": True,
        "transcription_instructions": args.transcription_instructions,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Submit one unprocessed issue from S3 to Archivault as a transcription batch job."
    )

    add_job_arguments(parser)
    parser.add_argument("--issue", help="Submit this specific issue stem instead of picking a random unprocessed one")
    parser.add_argument("--force", action="store_true", help="Submit even if the issue is already in the processed file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the issue that would be submitted and exit without submitting")

    args = parser.parse_args()

    if len(args.transcription_instructions) > 500:
        parser.error("--transcription-instructions must be at most 500 characters.")

    # 1. Index the bucket and subtract what has already been done.
    issues, _ = build_issue_index(args.source_bucket, args.key_prefix, args.profile)
    if not issues:
        print(f"[!] No page images found under prefix '{args.key_prefix}' in bucket '{args.source_bucket}'.")
        sys.exit(1)

    _records, excluded = job_ledger.load_exclusions(
        args.ledger, args.legacy_processed_file, retry_failed=args.retry_failed
    )

    if args.issue:
        issue = args.issue.strip()
        if issue not in issues:
            print(f"[!] Issue '{issue}' was not found under prefix '{args.key_prefix}'.")
            sys.exit(1)
        if issue in excluded and not args.force:
            print(f"[!] Issue '{issue}' is already claimed in '{args.ledger}'. Use --force to re-submit.")
            sys.exit(1)
    else:
        unprocessed = sorted(set(issues) - excluded)
        if not unprocessed:
            print_status("Every issue under this prefix has already been processed. Nothing to do.")
            sys.exit(0)
        print_status(f"{len(unprocessed)} issue(s) remaining unprocessed.")
        issue = random.choice(unprocessed)

    keys = issues[issue]
    title = issue_title(issue)
    print_status(f"Selected issue '{issue}' ({len(keys)} page(s)); job title '{title}'.")

    if args.dry_run:
        print_status("Dry run -- not submitting. Keys that would be sent:")
        for key in keys:
            print(f"    {key}")
        return

    # 2. Authenticate.
    email, password = resolve_credentials(args)
    token = login(args.api_url, email, password)

    # 3. Submit. Batch mode returns as soon as the job is enqueued; artifacts are
    #    collected later from the dashboard or by polling GET /jobs/{jobId}.
    job_id = submit_issue(args, token, issue, keys)

    # 4. Only now is the issue considered spoken for.
    job_ledger.append_record(args.ledger, ledger_record(args, issue, keys, job_id, job_ledger.STATUS_SUBMITTED))
    print_status(f"Submitted issue '{issue}' as job {job_id}; recorded in '{args.ledger}'.")


if __name__ == "__main__":
    main()
