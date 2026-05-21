import os
import io
import fitz  # PyMuPDF
import cv2
import numpy as np
import hashlib
from PIL import Image

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
        elif mad > 0:
            mod_z = 0.6745 * (size - median_size) / mad
            if mod_z < -3.5:
                is_outlier = True
                reason = f"Modified Z-score ({mod_z:.2f}) < -3.5"

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


def process_pdf_images_dynamic(directory_path: str):
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

                        # 4. Crop and Save
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
                
    # Run the robust outlier check for corrupted/truncated files at the very end
    # if created_files:
        # detect_and_remove_low_outliers(created_files)

# ==========================================
# Example Execution
# ==========================================
if __name__ == "__main__":
    target_dir = "E:/vube/temp/1802_1" 
    process_pdf_images_dynamic(target_dir)