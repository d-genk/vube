import os
import csv
import argparse
import re

def get_priority_score(year, pub_id):
    # Priority 1: Specific publication IDs within certain year ranges
    if pub_id == '6899204' and 1803 <= year <= 1830:
        return 90
    if pub_id == '8544261' and 1808 <= year <= 1830:
        return 90
    if pub_id == '6452150' and 1818 <= year <= 1830:
        return 90
    if pub_id == '8446405' and 1770 <= year <= 1830:
        return 90
    
    # Priority 2: All other publications between 1790 and 1820
    if 1790 <= year <= 1820:
        return 9
        
    # Priority 3: All other publications between 1770 and 1830
    if 1770 <= year <= 1830:
        return 1
        
    return 0

def process_archives(directory, drive_number, csv_path=None):
    results = []
    
    # Load existing data if CSV is provided
    if csv_path and os.path.exists(csv_path):
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            results = list(reader)
            
    # Pattern to match YYYY_X_ZZZZZZZ
    # Assumes YYYY is 4 digits, publication ID is 4 to 7 digits
    pattern = re.compile(r'^(\d{4})_[^_]+_(\d{4,7})(?:\.zip)?$')
    
    if not os.path.isdir(directory):
        print(f"Error: Directory '{directory}' does not exist.")
        return

    for filename in os.listdir(directory):
        #print(filename)
        if not filename.lower().endswith('.zip'):
            continue
            
        name_without_ext = filename[:-4]
        match = pattern.match(name_without_ext)
        if match:
            year = int(match.group(1))
            #print(year)
            pub_id = match.group(2)
            
            score = get_priority_score(year, pub_id)
            if score > 0:
                results.append([filename, drive_number, score])
                
    output_path = csv_path if csv_path else 'filtered_archives.csv'
    
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerows(results)
        
    print(f"Processed directory. Output saved to {output_path}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Filter ZIP archives based on priority criteria.")
    parser.add_argument('directory', help="Local directory path containing ZIP archives.")
    parser.add_argument('drive_number', help="Drive number to record in the CSV.")
    parser.add_argument('--csv', help="Optional path to a local CSV to augment.", default=None)
    
    args = parser.parse_args()
    process_archives(args.directory, args.drive_number, args.csv)
