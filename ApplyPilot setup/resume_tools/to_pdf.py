#!/usr/bin/env python3
"""Turn a plain-text resume into a clean one-column PDF (ATS-friendly: real text, standard headings).

  python3 to_pdf.py resumes/resume_product_owner.txt          -> writes resumes/resume_product_owner.pdf

Text format: first line is your name, the next lines up to the first blank line are the header
(headline, contact). Lines in ALL CAPS are section headings. Lines starting with "- " are bullets.
A line with a tab, or 2+ spaces, before a date range puts the dates on the right ("Title, Company    2021 - Present").
Uses headless Chrome, Chromium or Edge; prints the page count so you can keep it to one page.
"""
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CSS = """
@page { size: 8.5in 11in; margin: 0.45in 0.6in; }
body { font-family: Helvetica, Arial, sans-serif; font-size: 9.6pt; line-height: 1.25; color: #000; margin: 0; }
.name { font-size: 17pt; font-weight: bold; text-align: center; margin: 0; }
.hd { text-align: center; margin: 1px 0; font-size: 9.4pt; }
h2 { font-size: 10.5pt; margin: 8px 0 3px; padding-bottom: 1px; border-bottom: 1px solid #000; }
p { margin: 0 0 2px; } ul { margin: 1px 0 2px; padding-left: 15px; } li { margin: 0 0 1px; }
.row { display: flex; justify-content: space-between; font-weight: bold; margin-top: 4px; }
.row span:last-child { font-weight: normal; }
"""
BROWSERS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "google-chrome", "chromium", "chromium-browser", "microsoft-edge"]
DATES = re.compile(r"^(.*?)(?:\t|\s{2,})((?:[A-Z][a-z]{2,8}\.? )?\d{4}\s*[-–]\s*(?:(?:[A-Z][a-z]{2,8}\.? )?\d{4}|Present|Current))\s*$")


def to_html(text):
    lines = text.strip().splitlines()
    out, i = [f"<p class='name'>{html.escape(lines[0].strip())}</p>"], 1
    while i < len(lines) and lines[i].strip():
        out.append(f"<p class='hd'>{html.escape(lines[i].strip())}</p>")
        i += 1
    in_list = False
    for line in lines[i:]:
        s = line.rstrip()
        bullet = s.lstrip().startswith(("- ", "• ", "* "))
        if in_list and not bullet:
            out.append("</ul>"); in_list = False
        if not s.strip():
            continue
        if bullet:
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{html.escape(s.lstrip()[2:].strip())}</li>")
        elif s.strip().isupper() and len(s.strip()) < 40:
            out.append(f"<h2>{html.escape(s.strip())}</h2>")
        elif DATES.match(s):
            left, right = DATES.match(s).groups()
            out.append(f"<div class='row'><span>{html.escape(left.strip())}</span><span>{html.escape(right)}</span></div>")
        else:
            out.append(f"<p>{html.escape(s.strip())}</p>")
    if in_list:
        out.append("</ul>")
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{''.join(out)}</body></html>"


def browser():
    for b in BROWSERS:
        if os.path.isabs(b) and os.path.exists(b):
            return b
        if not os.path.isabs(b) and shutil.which(b):
            return shutil.which(b)
    sys.exit("No Chrome, Chromium or Edge found. Install one, or export the PDF from a word processor.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1]).resolve()
    pdf = src.with_suffix(".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "resume.html"
        page.write_text(to_html(src.read_text()))
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp}/profile",
                        "--no-pdf-header-footer", f"--print-to-pdf={pdf}", page.as_uri()],
                       capture_output=True, timeout=90)
    if not pdf.exists():
        sys.exit("The browser didn't produce a PDF.")
    pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
    print(f"wrote {pdf} ({pages} page{'s' if pages != 1 else ''})" + ("  -- over one page, consider trimming" if pages > 1 else ""))
