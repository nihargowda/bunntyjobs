"""Shared paths and helpers for the application tracker (resume registry + email status)."""
import csv
import re
from datetime import date, datetime
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent  # "ApplyPilot setup"
import sys  # noqa: E402
sys.path.insert(0, str(SETUP))
from settings import SETTINGS  # noqa: E402


class Paths:
    """Every file the tracker reads or writes, relative to one root, so tests can point it at a temp folder."""

    def __init__(self, root=SETUP):
        self.root = Path(root)
        self.apps = self.root / "Applications"
        self.log = self.apps / "applications_log.csv"          # shared by all agents (record.sh appends)
        self.resume_log = self.apps / "resume_log.csv"         # which resume went with each submission
        self.snapshots = self.apps / "resumes_used"            # frozen copy of every resume file sent
        self.status = self.apps / "application_status.json"    # latest status per application, from email
        self.inbox = self.apps / "inbox"                       # raw email batches (gitignored)
        self.digest = self.apps / "morning_digest.md"
        self.approved = self.root / SETTINGS["resumes"]["default"]       # your default resume (config/settings.json)


SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "plc", "gmbh", "the", "lp", "llp"}


def norm(s):
    """Lowercase, accents and punctuation stripped, legal suffixes dropped: 'Acme Health, LLC' -> 'acme health', 'ÉCLAIR' -> 'eclair'."""
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    words = re.sub(r"[^a-z0-9]+", " ", s.lower()).split()
    return " ".join(w for w in words if w not in SUFFIXES)


def app_key(company, title):
    """One key per application: company + title without trailing '(...)' qualifiers."""
    return norm(company) + "|" + norm(re.sub(r"\s*\(.*\)$", "", title or ""))


def is_submitted(status):
    """Submitted, or already further along (the log has rows like 'ONLINE ASSESSMENT')."""
    s = (status or "").strip().lower()
    return s.startswith("submit") or s == "applied" or any(w in s for w in ("assessment", "interview", "offer"))


def submitted_apps(paths):
    """Submitted applications from the shared log, one per key: first submission date, notes of every row."""
    apps = {}
    try:
        with open(paths.log, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if not is_submitted(r.get("status")):
                    continue
                k = app_key(r.get("company"), r.get("title"))
                a = apps.setdefault(k, {"key": k, "company": (r.get("company") or "").strip(),
                                        "title": (r.get("title") or "").strip(),
                                        "date": (r.get("date") or "").strip(), "notes": ""})
                a["notes"] = " ".join(x for x in (a["notes"], (r.get("notes") or "").strip()) if x)
    except OSError:
        pass
    return apps


def parse_date(s):
    """Email dates arrive as RFC 2822 or ISO strings; returns a naive local datetime (or None)."""
    from email.utils import parsedate_to_datetime
    if not s:
        return None
    for f in (lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")), parsedate_to_datetime):
        try:
            d = f(str(s).strip())
            return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
        except (ValueError, TypeError, IndexError):
            continue
    return None


def today():
    return date.today()
