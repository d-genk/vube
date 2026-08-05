"""
crop_images.py -- crop the page images in one directory of already-unpacked PDFs.

Use this when the archive is already extracted and you just want the pages
cropped. To pick and unpack an archive first, use extract_and_crop.py; to run
the whole thing end to end including submission, use automate_pipeline.py.

    python crop_images.py E:/vube/temp/1812_0
    python crop_images.py E:/vube/temp/1812_0 --outlier-check

The cropping itself lives in crop_core.py and the extraction in
page_extract.py. This file is only a command line front end for them.
"""

import argparse
import sys

from page_extract import process_pdf_images_dynamic


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("directory", help="directory containing the PDFs to process")
    parser.add_argument("--outlier-check", action="store_true",
                        help="delete page images that are far smaller than the "
                             "rest, on the assumption they are truncated")
    args = parser.parse_args()

    try:
        process_pdf_images_dynamic(directory_path=args.directory,
                                   run_outlier_check=args.outlier_check)
    except NotADirectoryError as e:
        print(f"[!] {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
