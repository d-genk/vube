import os
import io
import csv
import zipfile
import random
import hashlib
import fitz  # PyMuPDF
import cv2
import numpy as np
from PIL import Image

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
            if len(row) >= 3:
                name, drive, score = row[0], row[1], int(row[2])
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


def detect_and_remove_low_outliers(file_paths: list):
    """
    Identifies and deletes files that are statistically low-end outliers in size.
    Uses three robust complementary methods:
    1. Direct relative check: size < 15% of median size.
    2. Modified Z-score (using Median Absolute Deviation) < -3.5 (highly robust).
    3. Classic Z-score < -2.5 (when std_dev is meaningful and size < 30% of median).
    For N=2, uses a direct relative comparison (one file < 15% of the other).
    """
    if not file_paths:
        return

    valid_files = []
    sizes = []
    for path in file_paths:
        if os.path.exists(path):
            valid_files.append(path)
            sizes.append(os.path.getsize(path))

    if not valid_files:
        print("No created files found to analyze.")
        return

    if len(valid_files) < 3:
        # With 1 or 2 files, standard statistical outlier detection (MAD/std_dev) is not applicable.
        # But we can perform a direct ratio comparison for exactly 2 files.
        if len(valid_files) == 2:
            s1, s2 = sizes[0], sizes[1]
            if s2 > 0 and (s1 / s2) < 0.15:
                try:
                    os.remove(valid_files[0])
                    print(f"Deleted corrupted/outlier file: '{os.path.basename(valid_files[0])}' ({s1 / 1024:.2f} KB). Reason: Size is only {s1/s2:.1%} of the other file.")
                except Exception as e:
                    print(f"Error deleting file '{valid_files[0]}': {e}")
            elif s1 > 0 and (s2 / s1) < 0.15:
                try:
                    os.remove(valid_files[1])
                    print(f"Deleted corrupted/outlier file: '{os.path.basename(valid_files[1])}' ({s2 / 1024:.2f} KB). Reason: Size is only {s2/s1:.1%} of the other file.")
                except Exception as e:
                    print(f"Error deleting file '{valid_files[1]}': {e}")
        return

    sizes = np.array(sizes, dtype=np.float64)
    median_size = np.median(sizes)
    mean_size = np.mean(sizes)
    std_dev = np.std(sizes)
    mad = np.median(np.abs(sizes - median_size))

    print(f"\n--- Statistical Outlier Analysis of Created Files ---")
    print(f"Total files analyzed: {len(valid_files)}")
    print(f"Mean size: {mean_size / 1024:.2f} KB")
    print(f"Median size: {median_size / 1024:.2f} KB")
    print(f"Std Dev: {std_dev / 1024:.2f} KB")
    print(f"Median Absolute Deviation (MAD): {mad / 1024:.2f} KB")

    deleted_count = 0
    for path, size in zip(valid_files, sizes):
        is_outlier = False
        reason = ""

        # Method 1: Obvious relative low-end outlier compared to the median (extremely robust)
        if size < 0.15 * median_size:
            is_outlier = True
            reason = f"Size ({size / 1024:.2f} KB) is less than 15% of median ({median_size / 1024:.2f} KB)"

        # Method 2: Robust Modified Z-score (low-end outlier using MAD)
        #elif mad > 0:
            #mod_z = 0.6745 * (size - median_size) / mad
            #if mod_z < -3.5:
                #is_outlier = True
                #reason = f"Modified Z-score ({mod_z:.2f}) < -3.5"

        # Method 3: Standard Z-score (when std_dev is significant)
        elif not is_outlier and std_dev > 0:
            z_score = (size - mean_size) / std_dev
            if z_score < -2.5 and size < 0.3 * median_size:
                is_outlier = True
                reason = f"Z-score ({z_score:.2f}) < -2.5 and size < 30% of median"

        if is_outlier:
            try:
                os.remove(path)
                print(f"Deleted corrupted/outlier file: '{os.path.basename(path)}' ({size / 1024:.2f} KB). Reason: {reason}")
                deleted_count += 1
            except Exception as e:
                print(f"Error deleting outlier file '{path}': {e}")

    if deleted_count == 0:
        print("No corrupted/outlier files detected.")
    else:
        print(f"Successfully deleted {deleted_count} corrupted/outlier file(s).")


def process_pdf_images_dynamic(directory_path: str, run_outlier_check: bool = False):
    """
    Extracts images from each PDF, ignores exact duplicates using MD5 hashing, 
    sorts them logically, calculates the document bounding box, and performs a 
    size-based outlier detection at the end to delete corrupted files.
    """
    if not os.path.isdir(directory_path):
        raise NotADirectoryError(f"The directory path '{directory_path}' does not exist.")

    created_files = []

    for filename in os.listdir(directory_path):
        if filename.lower().endswith(".pdf"):
            pdf_path = os.path.join(directory_path, filename)
            
            try:
                doc = fitz.open(pdf_path)
                image_counter = 1 
                seen_image_hashes = set() 
                
                for page_index in range(len(doc)):
                    page = doc[page_index]
                    image_list = page.get_images(full=True)
                    
                    # Sort images by their internal PDF name to maintain logical order
                    image_list.sort(key=lambda img: img[7])
                    
                    for img_index, img_info in enumerate(image_list):
                        xref = img_info[0]
                        base_image = doc.extract_image(xref)
                        image_bytes = base_image["image"]

                        # Duplicate check via MD5
                        img_hash = hashlib.md5(image_bytes).hexdigest()
                        if img_hash in seen_image_hashes:
                            continue
                        seen_image_hashes.add(img_hash)
                        
                        # Decode bytes into OpenCV
                        np_arr = np.frombuffer(image_bytes, np.uint8)
                        cv_img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                        
                        if cv_img is None:
                            print(f"Warning: OpenCV could not decode image {image_counter} in '{filename}'")
                            continue
                            
                        height, width = cv_img.shape[:2]
                        
                        # Thresholding and Contour Detection
                        gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
                        # Threshold at 250 to cleanly catch off-white document background and text
                        _, thresh = cv2.threshold(gray, 250, 255, cv2.THRESH_BINARY_INV)
                        
                        # Use morphological closing to merge text lines and paper textures 
                        # into a single solid rectangular component representing the page.
                        # Kernel size is computed relative to the image dimensions (2%).
                        k_size_x = max(5, int(width * 0.02))
                        k_size_y = max(5, int(height * 0.02))
                        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k_size_x, k_size_y))
                        closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
                        
                        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        
                        # Find the contour with the largest bounding box area
                        best_box = None
                        if contours:
                            candidates = []
                            for cnt in contours:
                                x, y, w, h = cv2.boundingRect(cnt)
                                area = w * h
                                candidates.append((area, x, y, x + w, y + h))
                            
                            # Sort by area descending
                            candidates.sort(key=lambda c: c[0], reverse=True)
                            
                            # The largest candidate is our document page scan
                            best_box = (candidates[0][1], candidates[0][2], candidates[0][3], candidates[0][4])
                        else:
                            best_box = (0, 0, width, height) 

                        # Crop and Save
                        with Image.open(io.BytesIO(image_bytes)) as img:
                            left, top, right, bottom = best_box
                            cropped_img = img.crop((left, top, right, bottom))
                            
                            base_name = os.path.splitext(filename)[0]
                            output_filename = f"{base_name}_{image_counter:02d}.png"
                            output_path = os.path.join(directory_path, output_filename)
                            
                            cropped_img.save(output_path, format="PNG")
                            created_files.append(output_path)
                            print(f"Success: '{filename}' image {image_counter} saved as '{output_filename}'")
                            
                        image_counter += 1 
                        
                if image_counter == 1:
                    print(f"Skipped: '{filename}' (No embedded images found)")
                    
                doc.close()
                
            except Exception as e:
                print(f"Error processing '{filename}': {e}")
                
    # Run the robust outlier check for corrupted/truncated files at the very end if enabled
    if run_outlier_check and created_files:
        detect_and_remove_low_outliers(created_files)


# =========================================================================
# CONFIGURATION BLOCK & EXECUTION PIPELINE
# =========================================================================
if __name__ == "__main__":
    # EDIT THESE PARAMETERS DIRECTLY IN THE TEXT EDITOR
    # -------------------------------------------------------------
    
    # Path to the archive selection CSV containing name, drive, and score columns
    ARCHIVE_LIST_CSV = "filtered_archives.csv"
    
    # Directory where ZIP archives are stored
    ARCHIVE_DIR = "F:/1000302/PDF/00010101_99991231"
    
    # Directory to extract the PDFs from the ZIP archive to
    EXTRACT_DIR = "E:/vube/temp"
    
    # Drive number/identifier to filter available ZIP archives in the CSV
    DRIVE_NUMBER = "i"
    
    # CSV file listing already processed archives to avoid duplicates
    PROCESSED_CSV = "processed_archives.csv"
    
    # Set to True to enable robust statistical outlier detection to delete
    # corrupted or truncated image files after extraction and cropping.
    RUN_OUTLIER_DETECTION = True
    
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
