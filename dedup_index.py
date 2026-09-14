#!/usr/bin/env python3
"""
dedup_index.py

Builds a duplicate index for a collection of page images, and is the authority the
submitters consult to decide which images to send for transcription.

## Why duplicates exist

The data provider supplies one file per identified article, so a page carrying the end
of one article and the start of the next is delivered twice -- once in each article's
file. Pages sharing a FFFFFFF were already deduplicated during cropping; the overlap
that remains is between different FFFFFFF values within the same issue.

## What counts as a duplicate

Only images within the same issue -- the same PPPP(P)_YYYYMMDD -- are ever compared.
This is both a large saving and a correctness rule: two blank or near-blank pages from
different issues can be byte-identical without being the same page, and merging them
would attach one issue's transcription to another issue's image.

## Two tiers

    etag    Byte-identical images, read straight from the S3 listing. Free -- no
            downloads, no decoding. If the provider's crops are deterministic this is
            the whole answer.
    dhash   Perceptual hash (64-bit difference hash). Catches images that are the same
            page but not byte-identical (recompression, minor crop jitter). Requires
            downloading each image once; still no transcription spend.

Both tiers produce the same manifest shape, so consumers do not care which ran.

## The manifest

A single JSON document. Only multi-member clusters are stored -- any key absent from the
index is its own representative, which keeps the file small and the semantics simple:

    {
      "version": 1,
      "method": "etag",
      "bucket": "...", "key_prefix": "BP2/",
      "stats": {...},
      "clusters": [
        {"id": "...", "issue": "1759_18150101",
         "representative": "BP2/1815_0_1759/1759_18150101_0000117_0003.jpeg",
         "members": ["BP2/.../..._0003.jpeg", "BP2/.../..._0001.jpeg"]}
      ]
    }

The representative is the lexicographically smallest key in the cluster, so rebuilding
the index over unchanged data yields an identical manifest.

`members` is what makes the transcription re-attachable: transcribe the representative
once, then map its text back onto every member.
"""

import os
import io
import re
import sys
import json
import argparse
import datetime
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_INDEX = "dedup_index.json"
INDEX_VERSION = 1

# Matches submit_volume_job.PAGE_FILENAME_RE. Kept independent so this script can be run
# against a raw prefix without importing the submitter.
PAGE_FILENAME_RE = re.compile(
    r"^(?P<pub>\d+)_(?P<date>\d{8})_(?P<fid>\d+)_(?P<page>\d+)\.jpe?g$", re.IGNORECASE
)


def print_status(msg):
    print(f"[*] {msg}")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_folder_list(path):
    """Volume folder names that scope a run, from a CSV or a plain newline list.

    Lives here because the index build and the submitters must agree on exactly which
    volumes are in play. The bucket prefix holds far more than any one request: BP2/
    contains millions of objects, so an unscoped run would index -- and submit -- volumes
    nobody asked for.

    A CSV is read by its "Folder name" column; anything else is treated as one name per
    line. Returns None when no path is given, meaning "no restriction".
    """
    if not path:
        return None
    if not os.path.exists(path):
        print(f"[!] Folder list '{path}' does not exist.")
        sys.exit(1)

    names = []
    with open(path, "r", encoding="utf-8-sig") as f:
        head = f.readline()
        f.seek(0)
        if "," in head and "folder" in head.lower():
            import csv
            for r in csv.DictReader(f):
                col = next((c for c in r if c and c.strip().lower() in ("folder name", "folder")), None)
                if col and r[col].strip():
                    names.append(r[col].strip())
        else:
            names = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    folders = set(names)
    print_status(f"Scope: {len(folders)} folder(s) from '{path}'.")
    return folders


def list_objects(bucket, key_prefix, profile=None, folders=None):
    """Every page object in scope, with the ETag and size S3 gives away free.

    When `folders` is given, each folder is listed under its own prefix rather than
    scanning the whole key_prefix -- 150 small listings beat one multi-million-object
    scan by orders of magnitude.
    """
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    rows = []
    skipped = 0
    paginator = s3.get_paginator("list_objects_v2")

    if folders:
        prefixes = [f"{key_prefix.rstrip('/')}/{name}/" for name in sorted(folders)]
        print_status(f"Listing {len(prefixes)} scoped folder(s) under s3://{bucket}/{key_prefix} ...")
    else:
        prefixes = [key_prefix]
        print_status(f"Listing s3://{bucket}/{key_prefix} (unscoped) ...")

    try:
        for i, prefix in enumerate(prefixes, start=1):
            found = 0
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    found += 1
                    match = PAGE_FILENAME_RE.match(obj["Key"].rsplit("/", 1)[-1])
                    if not match:
                        skipped += 1
                        continue
                    rows.append({
                        "key": obj["Key"],
                        # A multipart upload's ETag is not an MD5; those carry a "-N"
                        # suffix and compare only to parts-identical uploads. Recorded
                        # as-is, and counted below so a corpus of them stays visible.
                        "etag": obj["ETag"].strip('"'),
                        "size": obj["Size"],
                        "issue": f"{match.group('pub')}_{match.group('date')}",
                        "fid": match.group("fid"),
                    })
            if folders and not found:
                print(f"[!] No objects under '{prefix}' -- folder named in the list but not in the bucket.")
            if folders and i % 25 == 0:
                print_status(f"    {i}/{len(prefixes)} folders listed, {len(rows)} pages so far")
    except (BotoCoreError, ClientError) as e:
        print(f"[!] Error listing bucket '{bucket}': {e}")
        sys.exit(1)

    print_status(f"{len(rows)} page object(s) listed; {skipped} non-page object(s) ignored.")
    return rows


def cluster_by_signature(rows, signature_of):
    """Group rows into duplicate clusters, strictly within an issue.

    Returns (clusters, cross_issue) where cross_issue counts signatures that recur in
    more than one issue -- not merged, but reported, since a large number usually means
    blank pages rather than genuine duplicates.
    """
    buckets = collections.defaultdict(list)
    for row in rows:
        sig = signature_of(row)
        if sig is None:
            continue
        buckets[(row["issue"], sig)].append(row)

    by_signature = collections.defaultdict(set)
    for (issue, sig) in buckets:
        by_signature[sig].add(issue)
    cross_issue = sum(1 for sig, issues in by_signature.items() if len(issues) > 1)

    clusters = []
    for (issue, sig), members in sorted(buckets.items()):
        if len(members) < 2:
            continue
        keys = sorted(m["key"] for m in members)
        clusters.append({
            "id": str(sig),
            "issue": issue,
            "representative": keys[0],
            "members": keys,
        })

    return clusters, cross_issue


def dhash_bytes(data, size=8):
    """64-bit difference hash: downscale to (size+1)x size greyscale, compare neighbours.

    Robust to recompression and small brightness shifts, which is what separates this
    from the ETag tier. Returns None if the bytes are not a readable image.
    """
    try:
        from PIL import Image
    except ImportError:
        print("[!] Pillow is required for --method dhash (pip install pillow).")
        sys.exit(1)

    try:
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
            px = list(img.getdata())
    except Exception:
        return None

    bits = 0
    for row in range(size):
        base = row * (size + 1)
        for col in range(size):
            bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
    return f"{bits:016x}"


def compute_dhashes(bucket, rows, profile=None, workers=16):
    """Download each image once and hash it. Bandwidth-bound, so threads are enough."""
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    hashes = {}
    failures = 0
    done = 0

    def fetch(row):
        body = s3.get_object(Bucket=bucket, Key=row["key"])["Body"].read()
        return row["key"], dhash_bytes(body)

    print_status(f"Hashing {len(rows)} image(s) with {workers} workers...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, r) for r in rows]
        for future in as_completed(futures):
            done += 1
            try:
                key, value = future.result()
            except Exception:
                failures += 1
                continue
            if value is None:
                failures += 1
            else:
                hashes[key] = value
            if done % 2000 == 0:
                print_status(f"    {done}/{len(rows)} hashed")

    if failures:
        print(f"[!] {failures} image(s) could not be hashed; they stay unclustered "
              f"and will be submitted individually.")
    return hashes


def build_index(bucket, key_prefix, method="etag", profile=None, workers=16, folders=None):
    rows = list_objects(bucket, key_prefix, profile, folders)
    if not rows:
        print(f"[!] No page images found under '{key_prefix}'.")
        sys.exit(1)

    multipart = sum(1 for r in rows if "-" in r["etag"])
    if method == "etag" and multipart:
        print(f"[!] {multipart} object(s) have multipart ETags, which are not content "
              f"hashes. Those cannot be compared byte-wise; consider --method dhash.")

    if method == "etag":
        clusters, cross_issue = cluster_by_signature(rows, lambda r: r["etag"])
    elif method == "dhash":
        hashes = compute_dhashes(bucket, rows, profile, workers)
        clusters, cross_issue = cluster_by_signature(rows, lambda r: hashes.get(r["key"]))
    else:
        print(f"[!] Unknown method '{method}'.")
        sys.exit(1)

    duplicates = sum(len(c["members"]) - 1 for c in clusters)
    index = {
        "version": INDEX_VERSION,
        "generated_at": utc_now(),
        "method": method,
        "bucket": bucket,
        "key_prefix": key_prefix,
        "scoped_folders": sorted(folders) if folders else None,
        "stats": {
            "images": len(rows),
            "issues": len({r["issue"] for r in rows}),
            "clusters": len(clusters),
            "duplicate_images": duplicates,
            "unique_images": len(rows) - duplicates,
            "cross_issue_signatures": cross_issue,
        },
        "clusters": clusters,
    }
    return index


def load_index(path):
    """Return (key -> representative key) for every duplicate member.

    Keys absent from the mapping are their own representative, so a consumer can treat a
    missing entry as "submit this one".
    """
    if not path or not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        index = json.load(f)

    if index.get("version") != INDEX_VERSION:
        print(f"[!] Warning: '{path}' is version {index.get('version')}, expected {INDEX_VERSION}.")

    mapping = {}
    for cluster in index.get("clusters", []):
        rep = cluster["representative"]
        for member in cluster["members"]:
            if member != rep:
                mapping[member] = rep
    return mapping


def main():
    parser = argparse.ArgumentParser(
        description="Build a duplicate index for page images in S3, before transcription."
    )
    parser.add_argument("--source-bucket", required=True, help="S3 bucket holding the page images")
    parser.add_argument("--key-prefix", required=True, help="Key prefix to index (e.g. BP2/)")
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"), help="AWS profile")
    parser.add_argument("--method", default="etag", choices=["etag", "dhash"],
                        help="etag: byte-identical, free. dhash: perceptual, downloads each image once.")
    parser.add_argument("--workers", type=int, default=16, help="Parallel downloads for dhash (default: 16)")
    parser.add_argument("--folders-file", default=None,
                        help="CSV or newline list of volume folder names to restrict the run to "
                             "(the bucket prefix holds far more than one request)")
    parser.add_argument("--out", default=DEFAULT_INDEX, help=f"Manifest path (default: {DEFAULT_INDEX})")

    args = parser.parse_args()

    folders = load_folder_list(args.folders_file)
    index = build_index(args.source_bucket, args.key_prefix, args.method, args.profile,
                        args.workers, folders)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)

    s = index["stats"]
    print()
    print_status(f"Method: {index['method']}")
    print_status(f"Images            {s['images']:,}")
    print_status(f"Issues            {s['issues']:,}")
    print_status(f"Duplicate clusters{s['clusters']:>8,}")
    print_status(f"Duplicate images  {s['duplicate_images']:,} "
                 f"({s['duplicate_images'] / s['images']:.1%} of corpus)")
    print_status(f"To transcribe     {s['unique_images']:,}")
    if s["cross_issue_signatures"]:
        print(f"[!] {s['cross_issue_signatures']:,} signature(s) recur across different issues; "
              f"these were NOT merged (likely blank pages).")
    print_status(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
