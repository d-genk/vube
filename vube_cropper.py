"""
vube_cropper.py -- desktop app for cropping page scans off provider PDFs.

Double-click the packaged VubeCropper.exe, or run:

    python vube_cropper.py                 # opens the window
    python vube_cropper.py --cli SOURCE    # headless, for scripting

The window is the intended way in. Pick the folder on the external drive, pick
where the cropped pages should go, press Start. Nothing is ever deleted and the
source drive is only ever read from.

Three things are deliberately optional and all default to the cautious choice:

  * Sending pages on to the transcription API is OFF. Cropping is local and
    free; uploading is neither, so it has to be asked for.
  * Deleting odd-looking pages is not offered at all. Instead the app flags
    them and shows you each one, and you decide.
  * Searching sub-folders is ON, because archive drives are usually nested.
"""

import argparse
import os
import queue
import sys
import threading
import traceback
import webbrowser

import cv2
import numpy as np
from PIL import Image

from page_extract import (
    find_pdfs,
    flag_pages,
    process_pdf,
    restore_full_page,
)

APP_NAME = "Vube Page Cropper"
SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".vube_cropper.json")
STRETCH_LO, STRETCH_HI = 222, 255


# ---------------------------------------------------------------------------
# Core run, shared by the window and the --cli mode
# ---------------------------------------------------------------------------

class CropRun:
    """One cropping job. Runs on a worker thread; reports through callbacks."""

    def __init__(self, source, output_dir, recursive=True):
        self.source = source
        self.output_dir = output_dir
        self.recursive = recursive
        self.results = []
        self.flagged = []
        self.error = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    @property
    def stopped(self):
        return self._stop.is_set()

    def run(self, on_progress=None, on_log=None):
        """
        on_progress(done, total, label) and on_log(text) are both optional.
        Returns True if the run finished without an unhandled error.
        """
        def log(msg):
            if on_log:
                on_log(msg)

        try:
            pdfs = find_pdfs(self.source, recursive=self.recursive)
            if not pdfs:
                self.error = ("No PDF files found in that folder.\n\n"
                              "Check that you picked the right folder, and that "
                              "'Include sub-folders' is ticked if the PDFs are "
                              "nested inside it.")
                return False

            log(f"Found {len(pdfs)} PDF file(s).")
            total = len(pdfs)
            for i, pdf in enumerate(pdfs, 1):
                if self.stopped:
                    log("Stopped.")
                    break
                name = os.path.basename(pdf)
                if on_progress:
                    on_progress(i - 1, total, name)

                # Mirror any sub-folder structure into the output directory so
                # two archives cannot overwrite each other's page numbering.
                out = self.output_dir
                if os.path.isdir(self.source):
                    rel = os.path.relpath(os.path.dirname(pdf), self.source)
                    if rel not in (".", ""):
                        out = os.path.join(self.output_dir, rel)

                pages = process_pdf(pdf, output_dir=out,
                                    should_stop=lambda: self.stopped)
                self.results.extend(pages)
                cropped = sum(1 for p in pages if p.box)
                log(f"  {name}: {len(pages)} page(s), {cropped} cropped")

            if on_progress:
                on_progress(total, total, "")

            self.flagged = flag_pages(self.results)
            cropped = sum(1 for r in self.results if r.box)
            log("")
            log(f"Done. {len(self.results)} page(s) written to {self.output_dir}")
            log(f"  {cropped} cropped, {len(self.results) - cropped} left unchanged")
            if self.flagged:
                log(f"  {len(self.flagged)} page(s) flagged for review")
            return True
        except Exception:
            self.error = traceback.format_exc()
            return False


# ---------------------------------------------------------------------------
# Image helpers for the review screen
# ---------------------------------------------------------------------------

def stretch(gray):
    """Mount board and paper are both near-white; this pulls them apart."""
    span = STRETCH_HI - STRETCH_LO
    return np.clip((gray.astype(np.float32) - STRETCH_LO) / span * 255,
                   0, 255).astype(np.uint8)


def review_thumbnail(result, max_h=560):
    """
    Side-by-side PIL image: the full page with the crop drawn on it, next to
    the cropped result. Contrast-stretched so the mount is actually visible.
    """
    import fitz
    import hashlib

    doc = fitz.open(result.pdf_path)
    try:
        seen, counter, data = set(), 1, None
        from page_extract import natural_key
        for pi in range(len(doc)):
            for info in sorted(doc[pi].get_images(full=True),
                               key=lambda i: natural_key(i[7])):
                blob = doc.extract_image(info[0])
                digest = hashlib.md5(blob["image"]).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                if counter == result.image_index:
                    data = blob["image"]
                    break
                counter += 1
            if data:
                break
    finally:
        doc.close()

    if data is None:
        return None

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    left = cv2.cvtColor(stretch(gray), cv2.COLOR_GRAY2BGR)
    if result.box:
        l, t, r, b = result.box
        cv2.rectangle(left, (l, t), (r - 1, b - 1), (0, 200, 0), max(4, w // 200))
        right = cv2.cvtColor(stretch(gray[t:b, l:r]), cv2.COLOR_GRAY2BGR)
    else:
        right = left.copy()

    def fit(a):
        ah, aw = a.shape[:2]
        return cv2.resize(a, (max(1, int(aw * max_h / ah)), max_h))

    left, right = fit(left), fit(right)
    gap = np.full((max_h, 18, 3), 245, np.uint8)
    combined = np.hstack([left, gap, right])
    return Image.fromarray(cv2.cvtColor(combined, cv2.COLOR_BGR2RGB))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def launch_gui():
    import json
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from PIL import ImageTk

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("760x620")
    root.minsize(700, 560)

    settings = {}
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            settings = json.load(fh)
    except Exception:
        pass

    source_var = tk.StringVar(value=settings.get("source", ""))
    output_var = tk.StringVar(value=settings.get("output", ""))
    recursive_var = tk.BooleanVar(value=settings.get("recursive", True))
    review_var = tk.BooleanVar(value=settings.get("review", True))
    submit_var = tk.BooleanVar(value=False)   # never remembered; always opt in

    state = {"run": None, "thread": None}
    msgq = queue.Queue()

    pad = {"padx": 12, "pady": 6}
    frm = ttk.Frame(root, padding=14)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    ttk.Label(frm, text="Step 1  --  Where are the PDFs?",
              font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=3,
                                                  sticky="w", pady=(0, 2))
    ttk.Label(frm, text="This folder is only ever read from.",
              foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w",
                                      pady=(0, 6))

    ttk.Label(frm, text="Source folder").grid(row=2, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=source_var).grid(row=2, column=1, sticky="ew", **pad)

    def pick_source():
        d = filedialog.askdirectory(title="Choose the folder containing the PDFs")
        if d:
            source_var.set(d)
            if not output_var.get():
                output_var.set(os.path.join(os.path.expanduser("~"),
                                            "Desktop", "Cropped Pages"))
    ttk.Button(frm, text="Browse...", command=pick_source).grid(row=2, column=2, **pad)

    ttk.Checkbutton(frm, text="Include sub-folders",
                    variable=recursive_var).grid(row=3, column=1, sticky="w", padx=12)

    ttk.Separator(frm).grid(row=4, column=0, columnspan=3, sticky="ew", pady=10)

    ttk.Label(frm, text="Step 2  --  Where should the cropped pages go?",
              font=("Segoe UI", 11, "bold")).grid(row=5, column=0, columnspan=3,
                                                  sticky="w")
    ttk.Label(frm, text="Output folder").grid(row=6, column=0, sticky="w", **pad)
    ttk.Entry(frm, textvariable=output_var).grid(row=6, column=1, sticky="ew", **pad)

    def pick_output():
        d = filedialog.askdirectory(title="Choose where to save the cropped pages")
        if d:
            output_var.set(d)
    ttk.Button(frm, text="Browse...", command=pick_output).grid(row=6, column=2, **pad)

    ttk.Separator(frm).grid(row=7, column=0, columnspan=3, sticky="ew", pady=10)

    ttk.Label(frm, text="Step 3  --  Options",
              font=("Segoe UI", 11, "bold")).grid(row=8, column=0, columnspan=3,
                                                  sticky="w")
    ttk.Checkbutton(frm, text="Show me any pages that look unusual when finished",
                    variable=review_var).grid(row=9, column=0, columnspan=3,
                                              sticky="w", padx=12, pady=2)
    ttk.Checkbutton(frm, text="Also send the cropped pages to the transcription "
                              "API (uploads data)",
                    variable=submit_var).grid(row=10, column=0, columnspan=3,
                                              sticky="w", padx=12, pady=2)

    ttk.Separator(frm).grid(row=11, column=0, columnspan=3, sticky="ew", pady=10)

    bar = ttk.Progressbar(frm, mode="determinate")
    bar.grid(row=12, column=0, columnspan=3, sticky="ew", padx=12)
    status = ttk.Label(frm, text="Ready.", foreground="#333")
    status.grid(row=13, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 0))

    logbox = tk.Text(frm, height=11, wrap="none", font=("Consolas", 9),
                     background="#fbfbfb")
    logbox.grid(row=14, column=0, columnspan=3, sticky="nsew", padx=12, pady=8)
    frm.rowconfigure(14, weight=1)
    logbox.configure(state="disabled")

    btns = ttk.Frame(frm)
    btns.grid(row=15, column=0, columnspan=3, sticky="ew", padx=12)
    start_btn = ttk.Button(btns, text="Start")
    start_btn.pack(side="left")
    stop_btn = ttk.Button(btns, text="Stop", state="disabled")
    stop_btn.pack(side="left", padx=6)
    review_btn = ttk.Button(btns, text="Review flagged pages", state="disabled")
    review_btn.pack(side="left", padx=6)
    open_btn = ttk.Button(btns, text="Open output folder", state="disabled")
    open_btn.pack(side="left", padx=6)

    def log(msg):
        msgq.put(("log", msg))

    def append(msg):
        logbox.configure(state="normal")
        logbox.insert("end", msg + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def save_settings():
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump({"source": source_var.get(), "output": output_var.get(),
                           "recursive": recursive_var.get(),
                           "review": review_var.get()}, fh)
        except Exception:
            pass

    # -- review window ------------------------------------------------------

    def open_review(flagged):
        win = tk.Toplevel(root)
        win.title(f"Review -- {len(flagged)} page(s) flagged")
        win.geometry("1080x740")

        idx = {"i": 0}
        head = ttk.Label(win, font=("Segoe UI", 10, "bold"))
        head.pack(anchor="w", padx=14, pady=(12, 2))
        why = ttk.Label(win, foreground="#a33", wraplength=1020, justify="left")
        why.pack(anchor="w", padx=14)
        ttk.Label(win, foreground="#555", wraplength=1020, justify="left",
                  text="Left: the whole scan, with the crop outlined in green. "
                       "Right: what will be sent. Both are brightness-boosted so "
                       "the provider's white mount is visible.").pack(
                           anchor="w", padx=14, pady=(6, 4))

        canvas = ttk.Label(win)
        canvas.pack(pady=6)
        keeper = {"img": None}

        def show():
            r = flagged[idx["i"]]
            head.config(text=f"Page {idx['i'] + 1} of {len(flagged)}   --   {r.name}")
            why.config(text="Flagged because: " + "; ".join(r.flags))
            thumb = review_thumbnail(r)
            if thumb:
                keeper["img"] = ImageTk.PhotoImage(thumb)
                canvas.config(image=keeper["img"])
            else:
                canvas.config(image="", text="(could not render preview)")
            state_lbl.config(
                text="Currently: cropped" if r.box else "Currently: full page, not cropped")

        def step(delta):
            idx["i"] = max(0, min(len(flagged) - 1, idx["i"] + delta))
            show()

        def use_full():
            r = flagged[idx["i"]]
            if not r.box:
                return
            if restore_full_page(r):
                append(f"Reverted to full page: {r.name}")
                show()

        state_lbl = ttk.Label(win, foreground="#333")
        state_lbl.pack(pady=(2, 4))

        row = ttk.Frame(win)
        row.pack(pady=8)
        ttk.Button(row, text="< Previous", command=lambda: step(-1)).pack(side="left", padx=4)
        ttk.Button(row, text="Keep the crop", command=lambda: step(1)).pack(side="left", padx=4)
        ttk.Button(row, text="Use the full page instead",
                   command=use_full).pack(side="left", padx=4)
        ttk.Button(row, text="Next >", command=lambda: step(1)).pack(side="left", padx=4)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=18)
        show()

    # -- run control --------------------------------------------------------

    def on_done(ok):
        start_btn.config(state="normal")
        stop_btn.config(state="disabled")
        open_btn.config(state="normal")
        run = state["run"]
        if not ok and run and run.error:
            status.config(text="Finished with a problem.")
            messagebox.showerror(APP_NAME, run.error)
            return
        status.config(text="Finished.")
        if run and run.flagged and review_var.get():
            review_btn.config(state="normal",
                              text=f"Review {len(run.flagged)} flagged page(s)")
            if messagebox.askyesno(
                    APP_NAME,
                    f"{len(run.results)} page(s) cropped.\n\n"
                    f"{len(run.flagged)} of them look unusual. Nothing has been "
                    f"deleted -- would you like to look at them now?"):
                open_review(run.flagged)
        else:
            messagebox.showinfo(APP_NAME,
                                f"{len(run.results) if run else 0} page(s) written to\n"
                                f"{output_var.get()}")
        if submit_var.get() and run and run.results:
            messagebox.showinfo(
                APP_NAME,
                "Cropping is done and the pages are ready to upload.\n\n"
                "Submission runs through automate_pipeline.py, which needs your "
                "API login. Ask Daniel to set that up the first time, then this "
                "box will hand off to it automatically.")

    def pump():
        try:
            while True:
                kind, payload = msgq.get_nowait()
                if kind == "log":
                    append(payload)
                elif kind == "progress":
                    done, total, label = payload
                    bar["maximum"] = max(1, total)
                    bar["value"] = done
                    status.config(text=f"{done} of {total}: {label}" if label
                                  else f"{done} of {total}")
                elif kind == "done":
                    on_done(payload)
        except queue.Empty:
            pass
        root.after(120, pump)

    def start():
        src, out = source_var.get().strip(), output_var.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning(APP_NAME, "Pick a source folder first.")
            return
        if not out:
            messagebox.showwarning(APP_NAME, "Pick an output folder first.")
            return
        if os.path.abspath(out).startswith(os.path.abspath(src) + os.sep):
            if not messagebox.askyesno(
                    APP_NAME,
                    "The output folder is inside the source folder. Cropped pages "
                    "will be written onto the same drive you are reading from.\n\n"
                    "Continue anyway?"):
                return
        try:
            os.makedirs(out, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not create the output folder:\n{e}")
            return

        save_settings()
        logbox.configure(state="normal")
        logbox.delete("1.0", "end")
        logbox.configure(state="disabled")
        bar["value"] = 0
        review_btn.config(state="disabled")
        start_btn.config(state="disabled")
        stop_btn.config(state="normal")
        status.config(text="Working...")

        run = CropRun(src, out, recursive=recursive_var.get())
        state["run"] = run

        def worker():
            ok = run.run(on_progress=lambda d, t, l: msgq.put(("progress", (d, t, l))),
                         on_log=log)
            msgq.put(("done", ok))

        t = threading.Thread(target=worker, daemon=True)
        state["thread"] = t
        t.start()

    def stop():
        if state["run"]:
            state["run"].stop()
            status.config(text="Stopping...")
            stop_btn.config(state="disabled")

    def open_output():
        out = output_var.get().strip()
        if out and os.path.isdir(out):
            webbrowser.open(f"file:///{os.path.abspath(out)}")

    start_btn.config(command=start)
    stop_btn.config(command=stop)
    open_btn.config(command=open_output)
    review_btn.config(command=lambda: state["run"] and open_review(state["run"].flagged))

    pump()
    root.mainloop()


# ---------------------------------------------------------------------------

def run_cli(args):
    out = args.output or os.path.join(os.getcwd(), "cropped_pages")
    run = CropRun(args.source, out, recursive=not args.no_recursive)
    ok = run.run(on_log=print,
                 on_progress=lambda d, t, l: None)
    if not ok:
        print(run.error or "failed", file=sys.stderr)
        return 1
    if run.flagged:
        print(f"\n{len(run.flagged)} page(s) flagged for review "
              f"(nothing was deleted):")
        for r in run.flagged:
            print(f"  {r.name}: {'; '.join(r.flags)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--cli", metavar="SOURCE", dest="source",
                    help="run without the window, on this folder")
    ap.add_argument("-o", "--output", help="output folder (CLI mode)")
    ap.add_argument("--no-recursive", action="store_true",
                    help="do not search sub-folders (CLI mode)")
    args = ap.parse_args()

    if args.source:
        return run_cli(args)
    launch_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
