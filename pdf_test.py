"""
pdf_test.py -- scratch runner for trying the extraction/cropping on one directory.

This is the throwaway-experiment entry point; crop_images.py is the same thing
with a proper command line. Both call into page_extract.py, which holds the one
real implementation.

    python pdf_test.py E:/sanskrit
"""

import sys

from page_extract import process_pdf_images_dynamic

DEFAULT_DIR = "E:/sanskrit"

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    process_pdf_images_dynamic(target)
