"""Keep job identifiers while dropping application-link tracking parameters."""
import re
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize(url):
    parts = urlsplit(url or "")
    tracking = {"source", "src", "ref", "referrer", "trackingid", "trk", "cmpid"}
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in tracking]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(sorted(query)), ""))


GENERIC_CO = r"\b(inc|llc|ltd|plc|corp|corporation|co|company|the|technologies|technology|group|holdings)\b"


def app_key(company, title):
    """One identity per job for every application count and duplicate check (dashboard,
    apply_control): 'Quillo Technologies' = 'Quillo', 'PM (Technical)' = 'Product Manager (Technical)'.
    Location in the title is kept, so the same role in two cities stays two applications."""
    co = re.sub(r"[^a-z0-9]+", " ", re.sub(GENERIC_CO, " ", (company or "").lower())).split()
    ti = re.sub(r"\bpm\b", "product manager", (title or "").lower())
    ti = re.sub(r"\bsr\b\.?", "senior", ti)
    return " ".join(co), re.sub(r"[^a-z0-9]+", " ", ti).strip()


def is_submitted(status):
    """A confirmed submission in applications_log.csv ('submitted', 'submitted (verified)', 'applied')."""
    st = (status or "").strip().lower()
    return st.startswith("submit") or st == "applied"


if __name__ == "__main__":
    print(normalize(sys.argv[1]))
