import os
import re
import csv
from pathlib import Path

def filter_zip_to_csv(drive_path, target_numbers, output_csv_path):
    """
    Finds ZIP archives matching specific ending numbers and saves their filenames
    followed by ',ii' format into a CSV file.
    
    :param drive_path: Str or Path to the external hard drive/directory.
    :param target_numbers: List or set of numbers to match.
    :param output_csv_path: Str or Path where the resulting CSV file will be saved.
    """
    # Convert target numbers to a set of strings for rapid lookups
    target_set = {str(num).strip() for num in target_numbers}
    
    # Regex to match '_[4 or 5 digits].zip' at the end of the filename
    pattern = re.compile(r"_(\d{4,5})\.zip$", re.IGNORECASE)
    
    # Using a set to ensure unique filenames in the final output
    matched_filenames = set()
    
    print(f"Scanning '{drive_path}' for matching archives...")
    
    # Walk through the directory structure
    for root, _, files in os.walk(drive_path):
        for file in files:
            match = pattern.search(file)
            if match:
                final_component = match.group(1)
                
                if final_component in target_set:
                    # Append ONLY the filename
                    matched_filenames.add(file)
                    
    output_path = Path(output_csv_path)
    
    try:
        # Open CSV with newline='' as recommended by the csv module documentation
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            
            if matched_filenames:
                # Sort alphabetically for a clean, organized layout
                for filename in sorted(matched_filenames):
                    # Writes a row with the filename in the first column and 'ii' in the second column
                    writer.writerow([filename, 'ii'])
                print(f"Success! Found {len(matched_filenames)} unique matching archives.")
                print(f"List saved to: {output_path.resolve()}")
            else:
                print("Scan complete. No matching archives were found.")
                
    except IOError as e:
        print(f"Error writing to output file: {e}")

# ==============================================================================
# Configuration & Execution
# ==============================================================================
if __name__ == "__main__":
    # --- CHANGE THESE VARIABLES TO MATCH YOUR SYSTEM ---

    # 1. Path to your external hard drive
    # Windows example: "E:\\" or "E:/Data"
    # Mac/Linux example: "/Volumes/MyExternalDrive"
    DRIVE_TO_SCAN = "F:/1000303/PDF/00010101_99991231"

    # 2. The specific subset of 4 or 5-digit final components you want to find
    NUMBERS_TO_FIND = [1506, 1714, 22052, 1759, 1646]

    # 3. Where you want to save the resulting text file
    OUTPUT_FILE = "./matched_archives.txt"

    # Run the function
    filter_zip_to_csv(DRIVE_TO_SCAN, NUMBERS_TO_FIND, OUTPUT_FILE)