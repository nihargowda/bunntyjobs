"""Your details and job filters, read from config/.

Every script reads its personal values and preferences from here, so the code itself holds none.
  config/settings.json         job filters and preferences (copy settings.example.json)
  config/profile.json          personal details used on application forms (copy profile.example.json)
  config/candidate_facts.json  approved facts and short answers (copy candidate_facts.example.json)
Your filled-in copies are gitignored; only the *.example.json templates are committed.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"

DEFAULTS = {
    "timezone": "America/New_York",
    "daily_target": 50,
    "min_score": 7,
    "senior_min_score": 9,
    "special_min_score": {"title_pattern": "", "min_score": 7},
    "senior_title_pattern": r"\b(senior|sr)\b",
    "title_exclude": [],
    "title_exclude_levels": [],
    "preferred_title_pattern": r"product (manag|owner|lead)|product management",
    "salary_minimum": 0,
    "excluded_companies": [],
    "excluded_industries": [],
    "description_exclude": [],
    "staffing_firms": [],
    "company_cap": 2,
    "company_cap_days": 60,
    "us_only": True,
    "need_sponsorship": False,
    "h1b_skip_tiers": [],
    "region_priority": [],
    "resumes": {"default": "resumes/resume.pdf", "variants": []},
    "scan_title_keep": "",
    "scan_title_drop": [],
    "gmail_address": "",
    "launchd_label": "com.applypilot.pipeline",
}


def _strip_comments(x):
    """Drop "_comment"-style keys so the templates can explain themselves."""
    if isinstance(x, dict):
        return {k: _strip_comments(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, list):
        return [_strip_comments(v) for v in x]
    return x


def load(name):
    """config/<name>.json, else the committed config/<name>.example.json (with a note), else {}."""
    real, example = CONFIG / f"{name}.json", CONFIG / f"{name}.example.json"
    for path in (real, example):
        if path.exists():
            if path is example:
                print(f"note: {real.relative_to(ROOT)} not found, using {example.name}. "
                      f"Copy it to {real.name} and fill in your own details.", file=sys.stderr)
            return _strip_comments(json.loads(path.read_text()))
    return {}


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


SETTINGS = _merge(DEFAULTS, load("settings"))


def profile():
    """Your personal details (config/profile.json)."""
    return load("profile")


def rx(patterns):
    """One case-insensitive regex from a pattern or a list of them. An empty list never matches."""
    if isinstance(patterns, str):
        patterns = [patterns] if patterns else []
    if not patterns:
        return re.compile(r"(?!x)x")
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


def words(names):
    """Regex for a list of company names, matched as whole words with flexible spacing."""
    return rx([r"\b" + r"\s*".join(map(re.escape, n.split())) + r"\b" for n in names if n.strip()])


def resume_for(title):
    """Absolute path of the resume to send for a job title: the first variant whose pattern matches, else the default."""
    r = SETTINGS["resumes"]
    for v in r.get("variants", []):
        if v.get("title_pattern") and re.search(v["title_pattern"], title or "", re.I):
            f = ROOT / v["file"]
            if f.exists():
                return str(f)
    return str(ROOT / r["default"])
