import os
import csv
import zipfile
import random
import argparse

def process_and_extract(archive_list_csv, archive_dir, extract_dir, drive_number, processed_csv):
    # 1. Read the archive list
    archives = []
    if not os.path.exists(archive_list_csv):
        print(f"Error: Archive list CSV '{archive_list_csv}' not found.")
        return

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
        return
        
    # 4. Weighted random selection
    names = [a[0] for a in available]
    weights = [a[1] for a in available]
    
    selected_archive = random.choices(names, weights=weights, k=1)[0]
    print(f"Selected archive: {selected_archive}")
    
    # 5. Extract the archive
    zip_path = os.path.join(archive_dir, selected_archive)
    if not os.path.exists(zip_path):
        print(f"Error: Archive '{zip_path}' not found in the specified directory.")
        return
        
    os.makedirs(extract_dir, exist_ok=True)
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
        print(f"Successfully extracted '{selected_archive}' to '{extract_dir}'.")
    except zipfile.BadZipFile:
        print(f"Error: '{zip_path}' is a bad zip file.")
        return
    except Exception as e:
        print(f"Error extracting '{zip_path}': {e}")
        return
        
    # 6. Update processed list
    with open(actual_processed_csv, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([selected_archive])
        
    print(f"Added '{selected_archive}' to '{actual_processed_csv}'.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Randomly select and extract an archive based on priority.")
    parser.add_argument('archive_list_csv', help="Path to the filtered archive list CSV.")
    parser.add_argument('archive_dir', help="Directory containing the ZIP archives.")
    parser.add_argument('extract_dir', help="Directory to extract the files to.")
    parser.add_argument('drive_number', help="Drive number to filter archives by.")
    parser.add_argument('--processed_csv', help="Optional path to CSV listing processed archives.", default=None)
    
    args = parser.parse_args()
    process_and_extract(args.archive_list_csv, args.archive_dir, args.extract_dir, args.drive_number, args.processed_csv)
