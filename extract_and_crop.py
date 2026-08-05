"""
extract_and_crop.py -- pick an archive off the drive, unpack it, and crop the
pages inside it.

Archive selection lives here. Page extraction and cropping live in
page_extract.py and crop_core.py; they are imported below rather than copied,
so there is exactly one implementation to fix when something is wrong with it.
"""

import os
import csv
import zipfile
import random

# Re-exported so existing callers (automate_pipeline.py) keep working unchanged.
from page_extract import (  # noqa: F401
    process_pdf_images_dynamic,
    detect_and_remove_low_outliers,
)

def process_and_extract(archive_list_csv, archive_dir, extract_dir, drive_number, processed_csv):
    """
    Randomly selects a ZIP archive based on drive number filter and weighted scores,
    extracts its contents to the target directory, and marks it as processed.
    Returns a tuple (selected_archive, extracted_subdir_path).
    """
    # 1. Read the archive list
    archives = []
    if not os.path.exists(archive_list_csv):
        print(f"Error: Archive list CSV '{archive_list_csv}' not found.")
        return None, None

    with open(archive_list_csv, 'r', newline='', encoding='utf-8') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                name, drive = row[0], row[1]
                score = int(row[2]) if len(row) >= 3 else 1
                if drive == drive_number:
                    archives.append((name, score))
                    
    # 2. Read processed archives
    processed = set()
    actual_processed_csv = processed_csv if processed_csv else 'processed_archives.csv'
    if os.path.exists(actual_processed_csv):
        with open(actual_processed_csv, 'r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if row:
                    processed.add(row[0])
                    
    # 3. Filter out already processed archives
    available = [a for a in archives if a[0] not in processed]
    
    if not available:
        print(f"No available archives to process for drive '{drive_number}'.")
        return None, None
        
    # 4. Weighted random selection
    names = [a[0] for a in available]
    weights = [a[1] for a in available]
    
    selected_archive = random.choices(names, weights=weights, k=1)[0]
    print(f"Selected archive: {selected_archive}")
    
    # 5. Extract the archive
    zip_path = os.path.join(archive_dir, selected_archive)
    if not os.path.exists(zip_path):
        print(f"Error: Archive '{zip_path}' not found in the specified directory.")
        return None, None
        
    os.makedirs(extract_dir, exist_ok=True)
    
    extracted_subdir = extract_dir
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # Detect if there's a common top-level subdirectory inside the ZIP
            names_in_zip = [name for name in zip_ref.namelist() if not name.startswith('__MACOSX')]
            if names_in_zip:
                first_parts = names_in_zip[0].split('/')
                if len(first_parts) > 1:
                    possible_prefix = first_parts[0]
                    if all(name.startswith(possible_prefix + '/') or name == possible_prefix for name in names_in_zip):
                        extracted_subdir = os.path.join(extract_dir, possible_prefix)
                        print(f"Detected sub-directory within archive: '{possible_prefix}'")
            
            zip_ref.extractall(extract_dir)
        print(f"Successfully extracted '{selected_archive}' to '{extract_dir}'.")
    except zipfile.BadZipFile:
        print(f"Error: '{zip_path}' is a bad zip file.")
        return None, None
    except Exception as e:
        print(f"Error extracting '{zip_path}': {e}")
        return None, None
        
    # 6. Update processed list
    with open(actual_processed_csv, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([selected_archive])
        
    print(f"Added '{selected_archive}' to '{actual_processed_csv}'.")
    return selected_archive, extracted_subdir


# =========================================================================
# CONFIGURATION BLOCK & EXECUTION PIPELINE
# =========================================================================
if __name__ == "__main__":
    # EDIT THESE PARAMETERS DIRECTLY IN THE TEXT EDITOR
    # -------------------------------------------------------------
    
    # Path to the archive selection CSV containing name, drive, and score columns
    ARCHIVE_LIST_CSV = "matched_archives.csv"
    
    # Directory where ZIP archives are stored
    ARCHIVE_DIR = "F:/1000303/PDF/00010101_99991231"
    
    # Directory to extract the PDFs from the ZIP archive to
    EXTRACT_DIR = "E:/vube/temp"
    
    # Drive number/identifier to filter available ZIP archives in the CSV
    DRIVE_NUMBER = "ii"
    
    # CSV file listing already processed archives to avoid duplicates
    PROCESSED_CSV = "processed_archives.csv"
    
    # Set to True to DELETE cropped pages whose file size falls far below the
    # batch median. Off by default: file size mostly tracks ink density, so a
    # sparse page looks the same as a truncated one, and a deleted page simply
    # disappears from the job without a trace. Use the review screen in
    # vube_cropper.py instead, which flags the same pages but asks first.
    RUN_OUTLIER_DETECTION = False
    
    # -------------------------------------------------------------
    
    print("Starting process pipeline...")
    print(f"Filtering archives by Drive Number: '{DRIVE_NUMBER}'")
    
    # Phase 1: Select and extract the archive
    selected, target_subdir = process_and_extract(
        archive_list_csv=ARCHIVE_LIST_CSV,
        archive_dir=ARCHIVE_DIR,
        extract_dir=EXTRACT_DIR,
        drive_number=DRIVE_NUMBER,
        processed_csv=PROCESSED_CSV
    )
    
    # Phase 2: If an archive was successfully extracted, process/crop all PDFs in that directory
    if selected:
        print(f"\nArchive '{selected}' successfully extracted.")
        print(f"Starting PDF image extraction and cropping in '{target_subdir}'...")
        process_pdf_images_dynamic(
            directory_path=target_subdir,
            run_outlier_check=RUN_OUTLIER_DETECTION
        )
        print("\nPipeline execution complete.")
    else:
        print("\nPipeline execution aborted: No archive was extracted.")
