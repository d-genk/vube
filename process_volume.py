#!/usr/bin/env python3
"""
process_volume.py

Takes as input a specific volume prefix, confirms that it hasn't been processed yet,
queries a specified S3 bucket for all objects having that volume ID as a prefix,
and submits the job to the Archivault image processing pipeline.
"""

import os
import sys
import json
import argparse
import getpass
import io
import boto3
from PIL import Image

# Import utilities from submit_job.py
try:
    from submit_job import (
        login,
        submit_job,
        download_artifacts,
        DEFAULT_API_URL,
        DEFAULT_METADATA_SCHEMA
    )
except ImportError:
    print("[!] Error: Could not import 'submit_job.py'. Ensure it is in the current directory.")
    sys.exit(1)

# Import helper functions from process_random_volume.py
try:
    from process_random_volume import (
        print_status,
        map_language,
        map_time_period,
        list_s3_objects_with_prefix,
        is_landscape_image,
        is_ecclesiastical_or_sacramental
    )
except ImportError:
    print("[!] Error: Could not import 'process_random_volume.py'. Ensure it is in the current directory.")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Process a specific volume prefix after confirming it has not been processed yet."
    )
    
    # Volume Prefix (positional or option)
    parser.add_argument("volume_prefix_pos", nargs="?", help="Specific volume prefix / ID to process")
    parser.add_argument("--volume-prefix", "-p", "--prefix", "--volume-id", dest="volume_prefix_opt", help="Specific volume prefix / ID to process")
    
    # Bucket & Files
    parser.add_argument("--source-bucket", required=True, help="S3 bucket containing the source files")
    parser.add_argument("--processed-file", help="Path to text file containing list of processed volume IDs (one per line)", default="processed_volumes.txt")
    parser.add_argument("--landscape-file", default="landscape_volumes.txt", help="Path to text file containing list of landscape volume IDs (one per line)")
    parser.add_argument("--volumes-file", default="volumes.json", help="Path to volumes.json database (default: volumes.json)")
    parser.add_argument("--force", action="store_true", help="Force processing even if the volume prefix is listed in the processed file")
    
    # Credentials & API URL
    parser.add_argument("--email", help="Authentication email")
    parser.add_argument("--password", help="Authentication password")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base API URL for the Archivault backend")
    parser.add_argument("--out-dir", default="./output", help="Directory to save downloaded artifacts")
    
    args = parser.parse_args()

    volume_prefix = args.volume_prefix_pos or args.volume_prefix_opt
    if not volume_prefix:
        parser.error("Volume prefix must be provided either as a positional argument or via --volume-prefix.")
    volume_prefix = volume_prefix.strip()

    print_status(f"Target volume prefix: '{volume_prefix}'")

    # 1. Read processed volume IDs & confirm that it hasn't been processed yet
    processed = set()
    if args.processed_file and os.path.exists(args.processed_file):
        print_status(f"Reading processed volumes list from '{args.processed_file}'...")
        try:
            with open(args.processed_file, 'r', encoding='utf-8') as f:
                for line in f:
                    val = line.strip()
                    if val:
                        processed.add(val)
            print_status(f"Found {len(processed)} already processed volume ID(s).")
        except Exception as e:
            print(f"[!] Error reading processed file: {e}")
            sys.exit(1)

    if volume_prefix in processed:
        if args.force:
            print_status(f"Warning: Volume prefix '{volume_prefix}' is in '{args.processed_file}', but --force was specified. Proceeding...")
        else:
            print(f"[!] Error: Volume prefix '{volume_prefix}' has already been processed (found in '{args.processed_file}').")
            print("[!] Use --force if you want to re-process this volume.")
            sys.exit(1)
    else:
        print_status(f"Confirmed: Volume prefix '{volume_prefix}' has not been processed yet.")

    # Read landscape volume IDs
    landscape_set = set()
    if args.landscape_file and os.path.exists(args.landscape_file):
        print_status(f"Reading landscape volumes list from '{args.landscape_file}'...")
        try:
            with open(args.landscape_file, 'r', encoding='utf-8') as f:
                for line in f:
                    val = line.strip()
                    if val:
                        landscape_set.add(val)
            print_status(f"Found {len(landscape_set)} already identified landscape volume ID(s).")
        except Exception as e:
            print(f"[!] Warning: Error reading landscape file: {e}")

    if volume_prefix in landscape_set:
        print_status(f"Warning: Volume prefix '{volume_prefix}' is marked as landscape in '{args.landscape_file}'.")

    # 2. Look up volume metadata from volumes.json (if available)
    volume_record = None
    if os.path.exists(args.volumes_file):
        print_status(f"Searching for metadata of '{volume_prefix}' in '{args.volumes_file}'...")
        try:
            with open(args.volumes_file, 'r', encoding='utf-8') as f:
                volumes = json.load(f)
                for vol in volumes:
                    if str(vol.get("id")) == volume_prefix:
                        volume_record = vol
                        break
        except Exception as e:
            print(f"[!] Warning: Error reading JSON volumes file: {e}")

    if volume_record:
        print_status(f"Found metadata record for volume prefix '{volume_prefix}' in '{args.volumes_file}'.")
        fields = volume_record.get("fields", {})
    else:
        print_status(f"No metadata record found in '{args.volumes_file}' for prefix '{volume_prefix}'. Using fallback defaults.")
        fields = {}

    title = fields.get("title", f"Volume {volume_prefix}")
    print_status(f"Volume Title: '{title}'")

    # 3. Query S3 bucket for objects with specified volume prefix
    keys = list_s3_objects_with_prefix(args.source_bucket, volume_prefix)
    if not keys:
        print(f"[!] Error: No S3 objects found under prefix '{volume_prefix}' in bucket '{args.source_bucket}'.")
        sys.exit(1)
    
    print_status(f"Found {len(keys)} S3 object(s) with prefix '{volume_prefix}'.")

    # 4. Check aspect ratio of middle image
    sorted_keys = sorted(keys)
    middle_key = sorted_keys[len(sorted_keys) // 2]
    if is_landscape_image(args.source_bucket, middle_key):
        print_status(f"Warning: Volume prefix '{volume_prefix}' middle image has landscape orientation.")
        if args.landscape_file and volume_prefix not in landscape_set:
            try:
                with open(args.landscape_file, 'a', encoding='utf-8') as f:
                    f.write(volume_prefix + "\n")
                print_status(f"Recorded '{volume_prefix}' into landscape file '{args.landscape_file}'.")
            except Exception as e:
                print(f"[!] Warning: Could not write to landscape file: {e}")

    # 5. Map metadata fields
    raw_lang = fields.get("language")
    mapped_language = map_language(raw_lang)
    print_status(f"Language Mapping: original={raw_lang} -> mapped='{mapped_language}'")

    start_date = fields.get("start_date")
    end_date = fields.get("end_date")
    mapped_time_period = map_time_period(start_date, end_date)
    print_status(f"Time Period Mapping: start={start_date}, end={end_date} -> mapped='{mapped_time_period}'")

    country = fields.get("country", "US")
    state = fields.get("state", "TN")
    description = fields.get("description", "")
    print_status(f"Additional Metadata: country='{country}', state='{state}'")

    # 6. Credentials & Authentication
    email = args.email
    if not email:
        email = input("Email: ").strip()
    if not email:
        print("[!] Error: Email is required.")
        sys.exit(1)

    password = args.password
    if not password:
        password = getpass.getpass("Password: ")
    if not password:
        print("[!] Error: Password is required.")
        sys.exit(1)

    # Retrieve login token
    token = login(args.api_url, email, password)

    # 7. Construct Job metadata
    metadata = {
        "writing_style": "",
        "language": mapped_language,
        "time_period": mapped_time_period,
        "layout_structure": "",
        "transcription_model": "gemini-3.1-pro-preview",
        "captioning_model": "gemini-3.1-flash-lite",
        "foliation_model": "gemini-3.1-flash-lite",
        "aggregation_model": "gemini-3.1-flash-lite",
        "metadata_model": "gemini-3.1-flash-lite",
        "non_textual_elements": [],
        "transcription_preferences": {
            "expand_abbreviations": False,
            "preserve_line_breaks": True,
            "retain_punctuation_and_spelling": True,
            "normalize_to_modern_language": False,
            "ignore_marginalia": False
        },
        "metadata_schema": DEFAULT_METADATA_SCHEMA,
        "additional_context_file": "",
        "additional_context_modules": ["foliation", "metadata", "transcription", "ner", "aggregation", "captioning", "layout"],
        "foliation_file": "",
        "foliation_override_discrete": False,
        "delete_data": True,
        "transcription_instructions": ""
    }

    # 8. Submit job and poll for completion
    job_id, artifacts, upload_duration, inference_duration = submit_job(
        api_url=args.api_url,
        token=token,
        directory=None,
        files_to_upload=[],
        title=volume_prefix,
        steps=["transcribe"],
        country=country,
        state=state,
        description=description,
        metadata=metadata,
        source_bucket=args.source_bucket,
        keys=keys
    )

    if artifacts:
        print_status("Downloading artifacts...")
        download_artifacts(artifacts, args.out_dir)
        print_status("Pipeline execution complete.")
        
        # 9. Add to processed file
        if args.processed_file:
            try:
                with open(args.processed_file, 'a', encoding='utf-8') as f:
                    f.write(volume_prefix + "\n")
                print_status(f"Added volume prefix '{volume_prefix}' to processed file '{args.processed_file}'.")
            except Exception as e:
                print(f"[!] Warning: Could not write to processed file '{args.processed_file}': {e}")
    else:
        print_status("No artifacts were returned for this job.")


if __name__ == "__main__":
    main()
