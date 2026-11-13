#!/usr/bin/env python3
"""Is this checkout set up for its user? Lists every config file or field that is missing or still the example value.

  python3 check_config.py          -> human-readable list; exit 0 when ready, 1 when something is missing
  python3 check_config.py --json   -> the same as JSON (for Claude's /setup interview)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config"

# fields that must be filled in
PERSONAL = {
    "profile": ["personal.full_name", "personal.email", "personal.phone", "personal.city", "personal.province_state",
                "personal.country", "personal.postal_code", "work_authorization.legally_authorized_to_work",
                "work_authorization.require_sponsorship", "experience.current_title", "experience.current_company",
                "experience.years_of_experience_total", "resume_facts.preserved_school"],
    "candidate_facts": ["name", "first_name", "email", "phone", "location", "current_role", "experience", "education",
                        "highlights"],
    "settings": ["timezone", "gmail_address"],
}
# of those, the ones whose template value is a placeholder no real person has (name, contact, address, employer)
PLACEHOLDERS = {
    "profile": {"personal.full_name", "personal.email", "personal.phone", "personal.city", "personal.postal_code",
                "experience.current_company", "resume_facts.preserved_school"},
    "candidate_facts": {"name", "first_name", "email", "phone", "location", "current_role", "education", "highlights"},
    "settings": {"gmail_address"},
}
# answers almost every application form asks; each must be filled in (the rest of "answers" is optional)
REQUIRED_ANSWERS = ["work_authorization", "sponsorship", "employment_type", "salary", "notice", "earliest_start_date",
                    "relocation", "work_arrangement", "years_experience", "highest_education", "age_18_or_over",
                    "security_clearance", "how_heard"]


def get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def load(path):
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return None, "missing"
    except json.JSONDecodeError as e:
        return None, f"not valid JSON (line {e.lineno}: {e.msg})"


def problems():
    out = []
    for name, fields in PERSONAL.items():
        real, err = load(CONFIG / f"{name}.json")
        if err:
            out.append({"file": f"config/{name}.json", "field": None, "issue": err})
            continue
        example, _ = load(CONFIG / f"{name}.example.json")
        for f in fields:
            v = get(real, f)
            if v in (None, "", []):
                out.append({"file": f"config/{name}.json", "field": f, "issue": "empty"})
            elif example is not None and f in PLACEHOLDERS[name] and v == get(example, f):
                out.append({"file": f"config/{name}.json", "field": f, "issue": "still the example value"})
        if name == "candidate_facts":
            if any("Replace with" in str(h) for h in real.get("highlights", [])):
                out.append({"file": "config/candidate_facts.json", "field": "highlights", "issue": "still the example text"})
            answers = real.get("answers", {})
            for a in REQUIRED_ANSWERS:
                if not str(answers.get(a, "")).strip():
                    out.append({"file": "config/candidate_facts.json", "field": f"answers.{a}", "issue": "empty"})
            if real.get("answers_confirmed") is not True:
                out.append({"file": "config/candidate_facts.json", "field": "answers_confirmed",
                            "issue": "the application answers haven't been checked with you yet (set true after reviewing them)"})
        if name == "settings":
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(real.get("timezone", ""))
            except Exception:
                out.append({"file": "config/settings.json", "field": "timezone", "issue": "not a valid time zone name, e.g. America/Chicago"})
            resumes = real.get("resumes", {})
            default = resumes.get("default")
            if not default or not (ROOT / default).is_file():
                out.append({"file": "config/settings.json", "field": "resumes.default", "issue": f"resume file not found: {default}"})
            elif not (ROOT / default).with_suffix(".txt").is_file():
                out.append({"file": "config/settings.json", "field": "resumes.default",
                            "issue": f"needs a plain-text copy next to it: {Path(default).with_suffix('.txt')}"})
            for v in resumes.get("variants", []):
                if v.get("file") and not (ROOT / v["file"]).is_file():
                    out.append({"file": "config/settings.json", "field": "resumes.variants",
                                "issue": f"variant not found (remove it or add the file): {v['file']}"})
    searches = CONFIG / "searches.yaml"
    if not searches.exists():
        out.append({"file": "config/searches.yaml", "field": None, "issue": "missing"})
    else:
        text = searches.read_text()
        example = (CONFIG / "searches.example.yaml").read_text() if (CONFIG / "searches.example.yaml").exists() else ""
        listed = lambda key, t: re.findall(rf"\{{\s*{key}:\s*\"?([^\",}}]+)", t)
        if not listed("query", text):
            out.append({"file": "config/searches.yaml", "field": "queries", "issue": "no job titles to search for"})
        elif listed("query", text) == listed("query", example):
            out.append({"file": "config/searches.yaml", "field": "queries", "issue": "still the example job titles"})
        if not listed("location", text):
            out.append({"file": "config/searches.yaml", "field": "locations", "issue": "no locations to search"})
        elif listed("location", text) == listed("location", example):
            out.append({"file": "config/searches.yaml", "field": "locations", "issue": "still the example locations"})
    env = CONFIG / "env.txt"
    if not env.exists():
        out.append({"file": "config/env.txt", "field": None, "issue": "missing"})
    elif not re.search(r"^(GEMINI|GROQ|OPENROUTER)_API_KEY=\S+", env.read_text(), re.M):
        out.append({"file": "config/env.txt", "field": "API key", "issue": "needs at least one key (Gemini or Groq are free)"})
    return out


if __name__ == "__main__":
    found = problems()
    if "--json" in sys.argv:
        print(json.dumps({"ready": not found, "problems": found}, indent=1))
    elif not found:
        print("Config is complete. Next: bash setup.sh")
    else:
        print(f"Setup is not finished ({len(found)} item{'s' if len(found) != 1 else ''}):")
        for p in found:
            print(f"  - {p['file']}" + (f" -> {p['field']}" if p["field"] else "") + f": {p['issue']}")
        print("Fill these in (or run /setup in Claude Code), then run this check again.")
    sys.exit(0 if not found else 1)
