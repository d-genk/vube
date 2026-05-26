#!/usr/bin/env python3
"""
Automated Archive Processing and Job Submission Pipeline

This script automates the complete process:
1. Prompts for credentials securely (email and password).
2. Verifies credentials against the Archivault API.
3. Selects a ZIP archive from a weighted priority CSV and extracts it.
4. Processes and crops any PDFs/images inside the archive.
5. Performs smart file filtering to exclude raw PDFs if cropped pages exist.
6. Submits the job to the Archivault image processing pipeline with the "transcribe" step
   and Gemini 3.1 Pro transcription model.
7. Polls for completion and downloads the final transcription and metadata artifacts.
"""

import os
import sys
import getpass
import argparse
import requests

# Import processing functions from extract_and_crop
try:
    from extract_and_crop import process_and_extract, process_pdf_images_dynamic
except ImportError:
    print("[!] Error: Could not import 'extract_and_crop.py'. Ensure it is in the current directory.")
    sys.exit(1)

# Import job submission utilities from submit_job
try:
    from submit_job import (
        login,
        get_files_to_upload,
        submit_job,
        DEFAULT_API_URL,
        DEFAULT_METADATA_SCHEMA
    )
except ImportError:
    print("[!] Error: Could not import 'submit_job.py'. Ensure it is in the current directory.")
    sys.exit(1)


def print_status(msg):
    print(f"[*] {msg}")


def download_artifacts_by_title(artifacts, output_dir, job_title):
    """
    Downloads job artifacts but names them after the job title instead
    of inheriting the S3 object key.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    for key, info in artifacts.items():
        if isinstance(info, dict) and 'presigned_url' in info:
            url = info['presigned_url']
            if not url:
                continue
                
            # Determine extension based on artifact type/key
            if key == 'json':
                filename = f"{job_title}.json"
            elif key == 'markdown':
                filename = f"{job_title}.md"
            elif key == 'tables_zip':
                filename = f"{job_title}_tables.zip"
            else:
                # Fallback for unexpected artifact keys
                filename = f"{job_title}_{key}"
                
            filepath = os.path.join(output_dir, filename)
            print_status(f"Downloading {key} artifact to {filepath}...")
            
            resp = requests.get(url, stream=True)
            if resp.ok:
                with open(filepath, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
            else:
                print(f"[!] Failed to download {key} artifact: {resp.status_code}")



def filter_pdf_files(files_list):
    """
    Filters out all PDF files from the upload list to ensure no raw PDF files
    are ever uploaded to the pipeline.
    """
    filtered_list = []
    for filepath in files_list:
        if filepath.lower().endswith('.pdf'):
            print_status(f"Filter: Excluding raw PDF '{os.path.basename(filepath)}' from submission.")
        else:
            filtered_list.append(filepath)
            
    return filtered_list


def split_files(files_list, max_size=1000):
    """
    Recursively divides a list of files in half until all resulting
    sublists contain at most max_size files.
    """
    if len(files_list) <= max_size:
        return [files_list]
    mid = len(files_list) // 2
    left = files_list[:mid]
    right = files_list[mid:]
    return split_files(left, max_size) + split_files(right, max_size)


def main():
    parser = argparse.ArgumentParser(description="Fully automated archive extraction, cropping, and transcription submission script.")
    
    # Extraction & Selection Parameters
    parser.add_argument("--archive-list-csv", default="filtered_archives.csv", help="Path to filtered archives list CSV")
    parser.add_argument("--archive-dir", default="F:/1000302/PDF/00010101_99991231", help="Directory where ZIP archives are stored")
    parser.add_argument("--extract-dir", default="E:/vube/temp", help="Directory where files will be extracted")
    parser.add_argument("--drive-number", default="i", help="Drive number/identifier to filter available archives")
    parser.add_argument("--processed-csv", default="processed_archives.csv", help="CSV listing processed archives")
    
    # PDF Image Cropping Options
    parser.add_argument("--run-outlier-detection", action="store_true", default=True, help="Enable robust statistical outlier detection for cropped images")
    parser.add_argument("--no-outlier-detection", action="store_false", dest="run_outlier_detection", help="Disable robust statistical outlier detection")
    
    # API & Job Settings
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base API URL for the Archivault backend")
    parser.add_argument("--out-dir", default="./output", help="Directory to save downloaded transcription/metadata artifacts")
    
    # Metadata Override Options (defaults match user requirements)
    parser.add_argument("--transcription-model", default="gemini-3.1-pro", help="Transcription model (default: gemini-3.1-pro)")
    parser.add_argument("--steps", nargs="*", default=["transcribe"], help="Processing steps (default: transcribe)")
    parser.add_argument("--country", default="", help="Country of origin")
    parser.add_argument("--state", default="", help="State/Province")
    parser.add_argument("--description", default="", help="Job description")
    parser.add_argument("--delete-data", action="store_true", help="Delete data from S3 after processing")
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("    ARCHIVE PROCESSING & JOB SUBMISSION PIPELINE    ")
    print("="*60 + "\n")
    
    # 1. Prompt securely for email and password
    print_status("Authentication required")
    email = input("Email: ").strip()
    if not email:
        print("[!] Error: Email cannot be empty.")
        sys.exit(1)
        
    password = getpass.getpass("Password: ")
    if not password:
        print("[!] Error: Password cannot be empty.")
        sys.exit(1)
        
    # 2. Login immediately to verify credentials and get the JWT token
    print()
    token = login(args.api_url, email, password)
    print()
    
    # 3. Prompt for sequential loop iterations
    print_status("Pipeline loop configuration")
    while True:
        runs_str = input("Number of runs to execute sequentially: ").strip()
        try:
            iterations = int(runs_str)
            if iterations <= 0:
                print("[!] Please enter a positive integer greater than 0.")
                continue
            break
        except ValueError:
            print("[!] Invalid input. Please enter a valid integer.")
    print()
    
    # 4. Sequentially execute runs
    for run_idx in range(1, iterations + 1):
        print("\n" + "="*60)
        print(f"    SEQUENTIAL RUN {run_idx} OF {iterations}")
        print("="*60 + "\n")
        
        # Phase 1: Select and extract the archive
        print_status("Phase 1: Selecting and extracting archive...")
        selected_archive, target_subdir = process_and_extract(
            archive_list_csv=args.archive_list_csv,
            archive_dir=args.archive_dir,
            extract_dir=args.extract_dir,
            drive_number=args.drive_number,
            processed_csv=args.processed_csv
        )
        
        if not selected_archive:
            print_status("Warning: No available archives found. Pipeline sequential loop complete.")
            break
            
        # Job title should be the name of the ZIP archive with .zip removed
        archive_lower = selected_archive.lower()
        if archive_lower.endswith(".zip"):
            job_title = selected_archive[:-4]
        else:
            job_title = selected_archive
            
        print_status(f"Selected Archive: {selected_archive}")
        print_status(f"Job Title: '{job_title}'")
        print_status(f"Extracted Path: '{target_subdir}'")
        print()
        
        # Phase 2: Process and crop all PDFs/images in the directory
        print_status("Phase 2: Running PDF extraction and dynamic page cropping...")
        try:
            process_pdf_images_dynamic(
                directory_path=target_subdir,
                run_outlier_check=args.run_outlier_detection
            )
        except Exception as e:
            print(f"[!] Warning: PDF extraction and cropping encountered an issue: {e}")
            
        print()
        
        # Phase 3: Smart file collection and filtering
        print_status("Phase 3: Collecting files and applying filters...")
        all_files = get_files_to_upload(target_subdir)
        files_to_upload = [os.path.abspath(f) for f in all_files]
        
        # Exclude raw PDF files completely from upload
        files_to_upload = filter_pdf_files(files_to_upload)
        
        if not files_to_upload:
            print(f"[!] Error on Run {run_idx}: No files found to upload in target directory. Skipping to next run.")
            continue
            
        print_status(f"Total files prepared for upload: {len(files_to_upload)}")
        print()
        
        # Build job metadata structure
        metadata = {
            "writing_style": "",
            "language": "english",
            "time_period": "",
            "layout_structure": "",
            "transcription_model": args.transcription_model,
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
            "delete_data": args.delete_data
        }
        
        # Split files if we have more than 1000 images
        parts = split_files(files_to_upload, max_size=1000)
        
        if len(parts) > 1:
            print_status(f"Job consists of {len(files_to_upload)} images (exceeds 1000). Divided into {len(parts)} parts for sequential processing.")
            for part_idx, part_files in enumerate(parts):
                part_title = f"{job_title}_{part_idx + 1}"
                print_status(f"\n--- Processing Part {part_idx + 1} of {len(parts)}: '{part_title}' ({len(part_files)} images) ---")
                
                # Phase 4: Submit job to the pipeline
                print_status(f"Phase 4: Submitting part {part_idx + 1} to Archivault processing pipeline...")
                job_id, artifacts = submit_job(
                    api_url=args.api_url,
                    token=token,
                    directory=target_subdir,
                    files_to_upload=part_files,
                    title=part_title,
                    steps=args.steps,
                    country=args.country,
                    state=args.state,
                    description=args.description,
                    metadata=metadata
                )
                
                # Phase 5: Download output artifacts named after the job title
                if artifacts:
                    print_status(f"Phase 5: Downloading output transcription artifacts for part {part_idx + 1}...")
                    download_artifacts_by_title(artifacts, args.out_dir, part_title)
                    print_status(f"Part {part_idx + 1} of {job_title} completed successfully!")
                else:
                    print_status(f"No artifacts returned for part {part_idx + 1} of {job_title}.")
            print_status(f"Run {run_idx} sequential execution completed successfully!")
        else:
            # Phase 4: Submit job to the pipeline
            print_status("Phase 4: Submitting job to Archivault processing pipeline...")
            job_id, artifacts = submit_job(
                api_url=args.api_url,
                token=token,
                directory=target_subdir,
                files_to_upload=files_to_upload,
                title=job_title,
                steps=args.steps,
                country=args.country,
                state=args.state,
                description=args.description,
                metadata=metadata
            )
            
            # Phase 5: Download output artifacts named after the job title
            if artifacts:
                print_status("Phase 5: Downloading output transcription artifacts...")
                download_artifacts_by_title(artifacts, args.out_dir, job_title)
                print_status(f"Run {run_idx} sequential execution completed successfully!")
            else:
                print_status(f"No artifacts returned for this job. Run {run_idx} complete.")
            
    print("\n" + "="*60)
    print("    PIPELINE SEQUENTIAL RUNS COMPLETE    ")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
