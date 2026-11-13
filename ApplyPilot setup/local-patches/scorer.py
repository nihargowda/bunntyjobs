"""Job fit scoring: LLM-powered evaluation of candidate-job match quality.

Scores jobs on a 1-10 scale by comparing the user's resume against each
job description. All personal data is loaded at runtime from the user's
profile and resume file.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot.config import RESUME_PATH, load_profile
from applypilot.database import get_connection, get_jobs_by_stage
from applypilot.llm import get_client

# [local patch] job filters come from your settings: setup.sh copies settings.py and config/settings.json to ~/.applypilot
import sys as _sys
_sys.path.insert(0, os.path.expanduser("~/.applypilot"))
from settings import SETTINGS as _S, rx as _rx, words as _words  # noqa: E402

log = logging.getLogger(__name__)


# ── Scoring Prompt ────────────────────────────────────────────────────────

SCORE_PROMPT = """You are a job fit evaluator. Given a candidate's resume and a job description, score how well the candidate fits the role.

SCORING CRITERIA:
- 9-10: Perfect match. Candidate has direct experience in nearly all required skills and qualifications.
- 7-8: Strong match. Candidate has most required skills, minor gaps easily bridged.
- 5-6: Moderate match. Candidate has some relevant skills but missing key requirements.
- 3-4: Weak match. Significant skill gaps, would need substantial ramp-up.
- 1-2: Poor match. Completely different field or experience level.

IMPORTANT FACTORS:
- Weight technical skills heavily (programming languages, frameworks, tools)
- Consider transferable experience (automation, scripting, API work)
- Factor in the candidate's project experience
- Be realistic about experience level vs. job requirements (years of experience, seniority)

RESPOND IN EXACTLY THIS FORMAT (no other text):
SCORE: [1-10]
KEYWORDS: [comma-separated ATS keywords from the job description that match or could match the candidate]
REASONING: [2-3 sentences explaining the score]"""


def _parse_score_response(response: str) -> dict:
    """Parse the LLM's score response into structured data.

    Args:
        response: Raw LLM response text.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    score = 0
    keywords = ""
    reasoning = response

    for line in response.split("\n"):
        line = line.strip()
        if line.startswith("SCORE:"):
            try:
                score = int(re.search(r"\d+", line).group())
                score = max(1, min(10, score))
            except (AttributeError, ValueError):
                score = 0
        elif line.startswith("KEYWORDS:"):
            keywords = line.replace("KEYWORDS:", "").strip()
        elif line.startswith("REASONING:"):
            reasoning = line.replace("REASONING:", "").strip()

    return {"score": score, "keywords": keywords, "reasoning": reasoning}


# [local patch] smaller scoring prompt (2026-10-25): the role overview plus the requirements section,
# instead of the first 6,000 chars, which often spent tokens on "About us" and cut off the requirements.
_REQS_HEADING = re.compile(
    r"(?im)^[\s#*\-•]*(minimum qualifications|basic qualifications|required qualifications|qualifications|"
    r"requirements|what you(?:'|’)ll (?:need|bring)|what we(?:'|’)re looking for|who you are|"
    r"you (?:have|bring|might be)|about you|skills (?:and|&) experience|experience (?:and|&) skills|must have)\b")


def _trim_description(desc: str, head: int = 1200, reqs: int = 2400) -> str:
    if len(desc) <= head + reqs:
        return desc
    m = _REQS_HEADING.search(desc, head)
    if not m:
        return desc[:head + reqs]
    return desc[:head] + "\n[...]\n" + desc[m.start():m.start() + reqs]


def _scoring_resume(text: str) -> str:
    """[local patch] Drop the contact header (name, phone, email, address); scoring only needs the rest."""
    i = text.find("\nSUMMARY")
    return text[i + 1:] if i != -1 else text


def score_job(resume_text: str, job: dict) -> dict:
    """Score a single job against the resume.

    Args:
        resume_text: The candidate's full resume text.
        job: Job dict with keys: title, site, location, full_description.

    Returns:
        {"score": int, "keywords": str, "reasoning": str}
    """
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {job['site']}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{_trim_description(job.get('full_description') or '')}"
    )

    messages = [
        {"role": "system", "content": SCORE_PROMPT},
        {"role": "user", "content": f"RESUME:\n{resume_text}\n\n---\n\nJOB POSTING:\n{job_text}"},
    ]

    try:
        client = get_client()
        response = client.chat(messages, max_tokens=512, temperature=0.2)
        return _parse_score_response(response)
    except Exception as e:
        log.error("LLM error scoring job '%s': %s", job.get("title", "?"), e)
        return {"score": 0, "keywords": "", "reasoning": f"LLM error: {e}"}


# [local patch] Title pre-filter and scoring priority -----------------------

_KEEP_TITLE = _rx(_S["scan_title_keep"]) if _S["scan_title_keep"] else None  # None: score every title
_DROP_TITLE = _rx(_S["scan_title_drop"])
_PREFERRED_TITLE = _rx(_S["preferred_title_pattern"])
_FORWARD_DEPLOYED = re.compile(r"forward[- ]deployed", re.I)
_FDE_DROP = re.compile(r"\b(director|vp|vice president|head of|chief|principal|staff|intern|internship|clearance|ts/sci|secret)\b", re.I)
_PREFERRED_LOC = _rx(_S["region_priority"])


_NO_SPONSOR = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|no)\s+(currently\s+)?(be\s+able\s+to\s+)?(provide|offer|sponsor|support)\w*\s+"
    r"(visa\s+|employment\s+|immigration\s+|h-?1b\s+)*sponsorship"
    r"|sponsorship(\s+[\w.,'-]+){0,4}?\s+(is|will\s+not\s+be|cannot\s+be)?\s*not\s+(be\s+)?(available|provided|offered|possible|considered)"
    r"|sponsorship(\s+[\w.,'-]+){0,4}?\s+(is\s+)?unavailable"
    r"|without\s+(the\s+need\s+for\s+)?(current\s+or\s+future\s+)?(visa\s+|employer\s+)?sponsorship"
    r"|no\s+(visa\s+)?sponsorship"
    r"|(must|required to)\s+be\s+(a\s+)?u\.?s\.?\s+citizen|u\.?s\.?\s+citizenship\s+(is\s+)?required"
    r"|security\s+clearance|green\s+card\s+holders?\s+only", re.I)
# Staffing firms whose contract roles are wanted (settings: staffing_firms)
_AGENCY = _words(_S["staffing_firms"])
_CONTRACT = re.compile(r"\b(contract|contractor|temporary|temp|part[- ]time|freelance|1099|c2c|corp[- ]to[- ]corp)\b", re.I)


_EXCLUDED_ORG = _rx(_S["excluded_industries"])


def _max_annual_pay(salary: str) -> float | None:
    """Highest pay in the listing, converted to a yearly figure (None if not stated)."""
    nums = [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", salary or "")]
    if not nums:
        return None
    top = max(nums)
    s = salary.lower()
    if "hour" in s:
        top *= 2080
    elif "month" in s:
        top *= 12
    elif "week" in s:
        top *= 52
    return top


def _prefilter_reason(title: str, description: str = "", salary: str = "", location: str = "") -> str | None:
    """Return why a job is skipped, or None to score it."""
    from applypilot.us_only import non_us_location
    place = non_us_location(location) if _S["us_only"] else None
    if place:
        return f"outside the US ({place})"  # [local patch] settings: us_only
    m = _NO_SPONSOR.search(description or "") if _S["need_sponsorship"] else None
    if m:
        return f"no visa sponsorship: \"{m.group(0)[:60]}\""
    m = _EXCLUDED_ORG.search(f"{title} {(description or '')[:1500]}")
    if m:
        return f"excluded organization type: '{m.group(0)}'"
    pay = _max_annual_pay(salary)
    floor = _S["salary_minimum"] or 0  # settings: salary_minimum
    if floor and pay is not None and pay < floor:
        return f"pay below ${floor:,} ({salary})"
    if _CONTRACT.search(title) and not _AGENCY.search(f"{title} {(description or '')[:3000]}"):
        return "not a full-time role (title)"
    if _KEEP_TITLE and not _KEEP_TITLE.search(title):
        return "title doesn't match scan_title_keep"
    m = _DROP_TITLE.search(title)
    if m:
        return f"title contains '{m.group(0)}'"
    return None


_TECH_TERMS = re.compile(
    r"\b(saas|software|platform|cloud|apis?|ai|ai[- ]powered|generative|llms?|artificial intelligence|machine learning|"
    r"technology company|tech company|fintech|developers?|data platform|digital products?|engineers|engineering teams?|"
    r"mobile apps?|web app|startup)\b", re.I)
_TECH_NAMES = re.compile(
    r"google|deepmind|youtube|amazon|aws|\bring\b|meta\b|whatsapp|instagram|microsoft|apple|salesforce|adobe|oracle|"
    r"paypal|cisco|lenovo|stripe|intuit|servicenow|workday|atlassian|uber|lyft|"
    r"airbnb|netflix|linkedin|nvidia|intel|ibm|dell|hp\b|snowflake|databricks|datadog|okta|twilio|shopify|block\b", re.I)


def is_tech_company(job: dict) -> bool:
    """[local patch] Heuristic: does the posting read like a software/tech company?"""
    head = f"{job.get('application_url') or ''} {job.get('title') or ''} {(job.get('full_description') or '')[:800]}"
    if _TECH_NAMES.search(head):
        return True
    text = (job.get("full_description") or "")[:2500]
    return len(set(m.group(0).lower() for m in _TECH_TERMS.finditer(text))) >= 3


def _priority(job: dict) -> tuple:
    title = (job.get("title") or "").lower()
    if _PREFERRED_TITLE.search(title):
        t = 0
    elif _KEEP_TITLE is None or _KEEP_TITLE.search(title):
        t = 1
    else:
        t = 2
    loc = 0 if _PREFERRED_LOC.search(job.get("location") or "") else 1
    tech = 0 if is_tech_company(job) else 1
    return (t, tech, loc)


def run_scoring(limit: int = 0, rescore: bool = False) -> dict:
    """Score unscored jobs that have full descriptions.

    Args:
        limit: Maximum number of jobs to score in this run.
        rescore: If True, re-score all jobs (not just unscored ones).

    Returns:
        {"scored": int, "errors": int, "elapsed": float, "distribution": list}
    """
    resume_text = _scoring_resume(RESUME_PATH.read_text(encoding="utf-8"))
    conn = get_connection()

    if rescore:
        query = "SELECT * FROM jobs WHERE full_description IS NOT NULL"
        if limit > 0:
            query += f" LIMIT {limit}"
        jobs = conn.execute(query).fetchall()
    else:
        jobs = get_jobs_by_stage(conn=conn, stage="pending_score", limit=limit)

    if not jobs:
        log.info("No unscored jobs with descriptions found.")
        return {"scored": 0, "errors": 0, "elapsed": 0.0, "distribution": []}

    # Convert sqlite3.Row to dicts if needed
    if jobs and not isinstance(jobs[0], dict):
        columns = jobs[0].keys()
        jobs = [dict(zip(columns, row)) for row in jobs]

    log.info("Scoring %d jobs sequentially...", len(jobs))
    t0 = time.time()
    completed = 0
    errors = 0
    results: list[dict] = []

    # [local patch] best-fit titles/locations first so a small daily quota goes furthest
    jobs.sort(key=_priority)

    # [local patch] cap AI-scored jobs per pass (pipeline.sh sets this for Groq) so one slow key can't hold the loop
    llm_cap = int(os.environ.get("APPLYPILOT_SCORE_LIMIT") or 0)
    llm_calls = 0

    for job in jobs:
        # [local patch] skip clearly irrelevant titles without spending an LLM request
        skip_reason = _prefilter_reason(job.get("title") or "", job.get("full_description") or "",
                                        job.get("salary") or "", job.get("location") or "")
        if skip_reason:
            result = {"score": -1, "keywords": "", "reasoning": f"[prefilter] {skip_reason}"}
        else:
            if llm_cap and llm_calls >= llm_cap:
                continue  # cap reached: keep recording free prefilter skips, leave the rest for the next pass
            llm_calls += 1
            result = score_job(resume_text, job)
        result["url"] = job["url"]
        completed += 1

        if str(result.get("reasoning", "")).startswith("LLM error"):
            # [local patch] leave the job unscored so it is retried later
            errors += 1
            log.info("[%d/%d] not scored (LLM error)  %s", completed, len(jobs), job.get("title", "?")[:60])
            if "429" in result["reasoning"]:
                log.warning("LLM quota exhausted -- stopping this scoring pass; unscored jobs stay pending.")
                break
            continue

        results.append(result)
        # [local patch] save each score immediately so downstream stages can pick it up
        conn.execute(
            "UPDATE jobs SET fit_score = ?, score_reasoning = ?, scored_at = ? WHERE url = ?",
            (result["score"], f"{result['keywords']}\n{result['reasoning']}",
             datetime.now(timezone.utc).isoformat(), result["url"]),
        )
        conn.commit()

        log.info(
            "[%d/%d] score=%d  %s",
            completed, len(jobs), result["score"], job.get("title", "?")[:60],
        )

    elapsed = time.time() - t0
    log.info("Done: %d scored in %.1fs (%.1f jobs/sec)", len(results), elapsed, len(results) / elapsed if elapsed > 0 else 0)

    # Score distribution
    dist = conn.execute("""
        SELECT fit_score, COUNT(*) FROM jobs
        WHERE fit_score IS NOT NULL
        GROUP BY fit_score ORDER BY fit_score DESC
    """).fetchall()
    distribution = [(row[0], row[1]) for row in dist]

    return {
        "scored": len(results),
        "errors": errors,
        "elapsed": elapsed,
        "distribution": distribution,
    }
