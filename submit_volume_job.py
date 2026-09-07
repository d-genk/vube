#!/usr/bin/env python3
"""
submit_volume_job.py

Submits one volume's worth of pre-processed page images from S3 to the Archivault
pipeline as a single transcription job.

Source keys take the form:

    [prefix]/YYYY_X_PPPP/PPPP_YYYYMMDD_FFFFFFF_NNNN.jpeg

Field widths are not fixed: the publication id (PPPP) is 4 or 5 digits in this
collection, and the other numeric fields are matched by shape rather than length. The
publication id in a page's filename must agree with the one in its folder.

A "volume" is every page object under one [prefix]/YYYY_X_PPPP folder -- that is, every
issue in the volume and every page of those issues, submitted together as one job. The
script lists the bucket, subtracts the volumes already claimed in the ledger (see
job_ledger.py), picks one of the remainder at random, and submits it. A ledger record is
written only after the API has accepted the submission.

Aggregating at the volume rather than the issue level keeps the number of concurrent
provider batches down: Gemini's batch API caps concurrent batches per key (100), and one
job occupies roughly one batch slot at a time, so the job count is the thing to control.

Images are copied server-side from the source bucket, so nothing is downloaded here;
the only reason boto3 credentials are needed is to enumerate keys. Reciprocal bucket
and Lambda permissions are assumed to already be in place.

Example:

    python submit_volume_job.py \
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
from collections import Counter

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

# PPPP_YYYYMMDD_FFFFFFF_NNNN.jpeg -- anything not matching this shape is not a page.
# Digit counts are deliberately unpinned except for the date: publication ids run to 4
# or 5 digits here, and pinning a width silently drops every volume that disagrees.
PAGE_FILENAME_RE = re.compile(
    r"^(?P<pub>\d+)_(?P<date>\d{8})_(?P<fid>\d+)_(?P<page>\d+)\.jpe?g$", re.IGNORECASE
)

# YYYY_X_PPPP -- the folder holding one volume's pages. Its name is the job title, and
# the folder path is the grouping key.
VOLUME_DIR_RE = re.compile(r"^(?P<year>\d{4})_(?P<vol>[^_]+)_(?P<pub>\d+)$")


def build_volume_index(bucket, key_prefix, profile=None):
    """List the bucket and group page keys by volume.

    Returns (volumes, skipped) where volumes maps '[prefix]/YYYY_X_PPPP' -> the sorted
    list of every page key in that folder, and skipped counts what was left out and why.

    Skips are categorised and reported rather than summed into one number: a pattern that
    quietly stops matching part of the collection is otherwise invisible, which is exactly
    how an earlier fixed-width publication id dropped a third of the volumes.
    """
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    volumes = {}
    skipped = Counter()
    total = 0

    print_status(f"Listing s3://{bucket}/{key_prefix} ...")
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                total += 1
                head, _, filename = key.rpartition("/")
                if not head:
                    skipped["at prefix root, no volume folder"] += 1
                    continue

                page_match = PAGE_FILENAME_RE.match(filename)
                if not page_match:
                    skipped["not a page filename"] += 1
                    continue

                dir_match = VOLUME_DIR_RE.match(head.rsplit("/", 1)[-1])
                if not dir_match:
                    skipped["unrecognized volume folder"] += 1
                    continue

                # The volume is read off the key path rather than reconstructed, so the
                # publication ids are cross-checked to catch a misfiled page instead of
                # silently filing it under the wrong volume.
                if page_match.group("pub") != dir_match.group("pub"):
                    skipped["publication id disagrees with folder"] += 1
                    continue

                volumes.setdefault(head, []).append(key)
    except (BotoCoreError, ClientError) as e:
        print(f"[!] Error listing bucket '{bucket}': {e}")
        sys.exit(1)

    for keys in volumes.values():
        keys.sort()

    pages = sum(len(k) for k in volumes.values())
    print_status(
        f"Scanned {total} object(s): {len(volumes)} volume(s) covering {pages} page(s)."
    )
    if skipped:
        print(f"[!] {sum(skipped.values())} object(s) skipped:")
        for reason, count in skipped.most_common():
            print(f"[!]     {count:>7}  {reason}")

    return volumes, skipped


def add_job_arguments(parser):
    """Add the arguments shared by every script that submits a volume.

    Kept here so submit_volume_job.py and its batch wrapper cannot drift apart on
    defaults -- build_metadata() below reads this exact namespace.
    """
    parser.add_argument("--source-bucket", required=True, help="S3 bucket holding the pre-processed page images")
    parser.add_argument("--key-prefix", required=True, help="Key prefix under which the target objects live")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"),
                        help="AWS profile with read access to the source bucket "
                             "(default: $AWS_PROFILE, else the default credential chain)")
    parser.add_argument("--ledger", default=job_ledger.DEFAULT_LEDGER,
                        help=f"JSON Lines ledger of every volume touched (default: {job_ledger.DEFAULT_LEDGER})")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Treat volumes whose job failed as eligible again")

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


def volume_title(volume):
    """YYYY_X_PPPP -- the folder name is the job title."""
    return volume.rsplit("/", 1)[-1]


def ledger_record(args, volume, keys, job_id, status, error=None):
    """Build the ledger record for one submission attempt.

    Defined here rather than in job_ledger.py because the submission context it captures
    comes from this module's argument namespace.
    """
    return job_ledger.build_record(
        volume=volume,
        job_title=volume_title(volume),
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


def submit_volume(args, token, volume, keys):
    """Submit one volume as a transcription-only batch job. Returns its job ID."""
    title = volume_title(volume)
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
        description="Submit one unprocessed volume from S3 to Archivault as a transcription batch job."
    )
    add_job_arguments(parser)
    parser.add_argument("--volume", help="Submit this specific volume path instead of picking a random unprocessed one")
    parser.add_argument("--force", action="store_true", help="Submit even if the volume is already claimed in the ledger")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the volume that would be submitted and exit without submitting")

    args = parser.parse_args()

    if len(args.transcription_instructions) > 500:
        parser.error("--transcription-instructions must be at most 500 characters.")

    # 1. Index the bucket and subtract everything already spoken for.
    volumes, _ = build_volume_index(args.source_bucket, args.key_prefix, args.profile)
    if not volumes:
        print(f"[!] No page images found under prefix '{args.key_prefix}' in bucket '{args.source_bucket}'.")
        sys.exit(1)

    _records, excluded = job_ledger.load_exclusions(args.ledger, retry_failed=args.retry_failed)

    if args.volume:
        volume = args.volume.strip().rstrip("/")
        if volume not in volumes:
            print(f"[!] Volume '{volume}' was not found under prefix '{args.key_prefix}'.")
            sys.exit(1)
        if volume in excluded and not args.force:
            print(f"[!] Volume '{volume}' is already claimed in '{args.ledger}'. Use --force to re-submit.")
            sys.exit(1)
    else:
        unprocessed = sorted(set(volumes) - excluded)
        if not unprocessed:
            print_status("Every volume under this prefix has already been processed. Nothing to do.")
            sys.exit(0)
        print_status(f"{len(unprocessed)} volume(s) remaining unprocessed.")
        volume = random.choice(unprocessed)

    keys = volumes[volume]
    title = volume_title(volume)
    print_status(f"Selected volume '{volume}' ({len(keys)} page(s)); job title '{title}'.")

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
    job_id = submit_volume(args, token, volume, keys)

    # 4. Only now is the volume considered spoken for.
    job_ledger.append_record(args.ledger, ledger_record(args, volume, keys, job_id, job_ledger.STATUS_SUBMITTED))
    print_status(f"Submitted volume '{volume}' as job {job_id}; recorded in '{args.ledger}'.")


if __name__ == "__main__":
    main()
