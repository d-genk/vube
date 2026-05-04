import os
import io
import fitz  # PyMuPDF
import cv2
import numpy as np
import hashlib
from PIL import Image

def process_pdf_images_dynamic(directory_path: str):
    """
    Extracts images from each PDF, ignores exact duplicates using MD5 hashing, 
    sorts them logically, calculates the document bounding box, and performs a 
    modulo-8 phase fold check to detect underlying JPEG macroblock corruption.
    """
    if not os.path.isdir(directory_path):
        raise NotADirectoryError(f"The directory path '{directory_path}' does not exist.")

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
                        _, thresh = cv2.threshold(gray, 240, 255, cv2.THRESH_BINARY_INV)
                        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        
                        x_margin = width * 0.20
                        y_margin = height * 0.20
                        
                        best_box = None
                        max_area = 0
                        
                        # 20% Heuristic
                        for cnt in contours:
                            x, y, w, h = cv2.boundingRect(cnt)
                            area = w * h
                            
                            if (x <= x_margin and 
                                (x + w) >= (width - x_margin) and 
                                y <= y_margin and 
                                (y + h) >= (height - y_margin)):
                                
                                if area > max_area:
                                    max_area = area
                                    best_box = (x, y, x + w, y + h)
                        
                        # Fallbacks
                        if best_box is None and contours:
                            largest_contour = max(contours, key=cv2.contourArea)
                            x, y, w, h = cv2.boundingRect(largest_contour)
                            best_box = (x, y, x + w, y + h)
                        elif best_box is None:
                            best_box = (0, 0, width, height) 

                        # 4. Crop and Sanity Check
                        with Image.open(io.BytesIO(image_bytes)) as img:
                            left, top, right, bottom = best_box
                            cropped_img = img.crop((left, top, right, bottom))
                            
                            cropped_arr = np.array(cropped_img)
                            is_corrupt = False
                            
                            # ==========================================
                            # Post-Processing Sanity Check: 8x8 Grid Detection
                            # ==========================================
                            crop_h = cropped_arr.shape[0]
                            bottom_slice = cropped_arr[int(crop_h * 0.85):, :]
                            
                            if bottom_slice.size > 0:
                                # Standardize to a 2D grayscale array regardless of original color depth
                                if bottom_slice.ndim == 3 and bottom_slice.shape[2] >= 3:
                                    gray_bottom = cv2.cvtColor(bottom_slice, cv2.COLOR_RGB2GRAY)
                                else:
                                    gray_bottom = bottom_slice
                                    
                                gray_float = gray_bottom.astype(np.float32)
                                
                                # Check horizontal 8x8 grid (vertical block boundaries)
                                diff_x = np.abs(np.diff(gray_float, axis=1))
                                trunc_w = (diff_x.shape[1] // 8) * 8
                                
                                if trunc_w >= 8:
                                    # Fold columns into 8 phase buckets and calculate the mean difference
                                    folded_x = diff_x[:, :trunc_w].reshape(-1, trunc_w // 8, 8).mean(axis=(0, 1))
                                    # Add a tiny epsilon to prevent division by zero in blank images
                                    ratio_x = np.max(folded_x) / (np.median(folded_x) + 1e-5)
                                    
                                    # Natural images rarely exceed ~1.2. An 8x8 JPEG grid spikes easily > 2.0
                                    if ratio_x > 2.0:
                                        print(f"Skipped: '{filename}' image {image_counter} (Corrupted - 8x8 Grid Detected)")
                                        is_corrupt = True

                                # Check vertical 8x8 grid (horizontal block boundaries)
                                if not is_corrupt:
                                    diff_y = np.abs(np.diff(gray_float, axis=0))
                                    trunc_h = (diff_y.shape[0] // 8) * 8
                                    
                                    if trunc_h >= 8:
                                        # Fold rows into 8 phase buckets
                                        folded_y = diff_y[:trunc_h, :].reshape(trunc_h // 8, 8, -1).mean(axis=(0, 2))
                                        ratio_y = np.max(folded_y) / (np.median(folded_y) + 1e-5)
                                        
                                        if ratio_y > 2.0:
                                            print(f"Skipped: '{filename}' image {image_counter} (Corrupted - 8x8 Grid Detected)")
                                            is_corrupt = True

                            # Only save if it passed the visual sanity checks
                            if not is_corrupt:
                                base_name = os.path.splitext(filename)[0]
                                output_filename = f"{base_name}_{image_counter:02d}.png"
                                output_path = os.path.join(directory_path, output_filename)
                                
                                cropped_img.save(output_path, format="PNG")
                                print(f"Success: '{filename}' image {image_counter} saved as '{output_filename}'")
                            
                        image_counter += 1 
                        
                if image_counter == 1:
                    print(f"Skipped: '{filename}' (No embedded images found)")
                    
                doc.close()
                
            except Exception as e:
                print(f"Error processing '{filename}': {e}")

# ==========================================
# Example Execution
# ==========================================
if __name__ == "__main__":
    target_dir = "E:/vube/sample" 
    process_pdf_images_dynamic(target_dir)