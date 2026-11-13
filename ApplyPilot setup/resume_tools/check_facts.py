#!/usr/bin/env python3
"""Check a tailored resume against the base resume: it may reorder and reword, never add facts.

  python3 check_facts.py resumes/resume.txt resumes/resume_product_owner.txt

Flags numbers (metrics, years, dates, team sizes) and capitalised names (employers, schools, tools,
certifications) that appear in the tailored version but nowhere in the base. Exit 1 if anything is flagged.
Anything flagged must be removed, or confirmed by the person as true and added to the base resume first.
"""
import re
import sys
from pathlib import Path

COMMON = {"I", "A", "An", "The", "And", "Or", "For", "With", "To", "In", "On", "Of", "At", "By", "As", "From",
          "Summary", "Experience", "Education", "Skills", "Certifications", "Core", "Competencies", "Professional",
          "Present", "Current", "Led", "Built", "Owned", "Drove", "Shipped", "Managed", "Partnered", "Defined",
          "Delivered", "Analyzed", "Created", "Designed", "Launched", "Improved", "Reduced", "Increased", "Grew",
          "Wrote", "Ran", "Worked", "Supported", "Developed", "Implemented", "Coordinated", "Translated", "Conducted"}


def numbers(text):
    return {n.strip(".,") for n in re.findall(r"\d[\d,./%+$]*", text)}


def names(text):
    return {w for w in re.findall(r"\b[A-Z][A-Za-z0-9&+.#-]{1,}(?:[ \t]+[A-Z][A-Za-z0-9&+.#-]+)*", text)}  # within one line


def problems(base, tailored):
    base_names = " ".join(names(base)).lower() + " " + base.lower()
    new_numbers = sorted(numbers(tailored) - numbers(base))
    new_names = sorted(n for n in names(tailored) - names(base)
                       if n not in COMMON and n.lower() not in base_names)
    return new_numbers, new_names


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    base, tailored = (Path(p).read_text() for p in sys.argv[1:])
    nums, nms = problems(base, tailored)
    if not nums and not nms:
        print("OK: no numbers or names that aren't in the base resume.")
        sys.exit(0)
    if nums:
        print("Numbers not in the base resume:", ", ".join(nums))
    if nms:
        print("Names or terms not in the base resume (check each is true and in the base):", ", ".join(nms))
    sys.exit(1)
