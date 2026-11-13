#!/usr/bin/env python3
"""Application status from email.

The morning task fetches recent job emails with the Gmail connector and saves them as a JSON list
(Applications/inbox/YYYY-MM-DD.json: [{"id", "thread_id", "date", "from", "subject", "body", "url"}]).
This script then:
  1. classifies each email: rejected / consideration / interview / assessment / offer (or not job-related),
  2. pulls out a deadline for assessments and interview scheduling ("by Oct 30", "within 3 days"),
  3. matches it to a submitted application in applications_log.csv (company in sender, subject or body),
  4. updates Applications/application_status.json and writes Applications/morning_digest.md,
  5. with --notify, pops up a macOS notification when there's an interview, assessment or offer.

  mail_status.py ingest Applications/inbox/2026-10-26.json [--notify]
  mail_status.py show [company]
Nothing here sends email or changes Gmail.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Paths, app_key, norm, parse_date, submitted_apps, today  # noqa: E402

# Checked in this order; the first status with a matching phrase wins. Rejection phrases are specific
# enough to beat "interview"/"thank you for applying" wording that rejection emails also contain.
RULES = [
    ("offer", [r"pleased to (extend|offer)", r"offer letter", r"extend (you )?an offer", r"verbal offer"]),
    ("rejected", [
        r"(not|won't|will not|unable to|decided not to|decision not to)( to)? (be )?(mov(e|ing)|proceed(ing)?|progress(ing)?|advanc(e|ing))\b[^.]{0,40}\b(forward|further|with your)",
        r"move forward with (other|another|different)\b",  # snippets often cut off before "candidates"
        r"(pursue|pursuing|proceed with|progress with|focus on) (other|another) (candidates?|applicants?)",
        r"other candidates (whose|who)", r"not (been )?selected", r"regret to inform",
        r"position (has been|was) (filled|closed)", r"no longer (under consideration|considering)",
        r"decided not to (proceed|continue|pursue)", r"not (a|the right) (fit|match) (for|at) this time",
        r"unable to offer you", r"will not be (moving|proceeding|offering)", r"(have|'ve) closed (this|the) (role|position|requisition)",
    ]),
    # receipts for work already done: confirmations, not something to do ("done" closes an open assessment)
    ("done", [
        r"(thank you|thanks) for (taking the time to )?complet(e|ing) (the|our|your)",
        r"(successfully )?(submitted|completed) (your|the) (online )?assessment",
        r"assessment (has been|was) (successfully )?(completed|submitted)",
    ]),
    ("consideration", [r"received your interview availability"]),
    ("assessment", [
        r"\bassessments?\b", r"coding (challenge|test|exercise)", r"take[- ]home", r"hacker ?rank", r"codesignal",
        r"codility", r"testgorilla", r"hirevue", r"one[- ]way (video )?interview", r"(online|skills?) test",
        r"complete (the|this|our|a) (short )?(questionnaire|exercise|case study|test|challenge)",
        r"case study (assignment|exercise)", r"work sample", r"criteria (corp|cognitive)",
    ]),
    ("interview", [
        r"schedul(e|ing) (a|an|your|the)? ?(\w+ ){0,2}(interview|call|chat|conversation|screen|meeting)",  # "a 30 minute call"
        r"\b(phone|video|recruiter|initial|first[- ]round|technical|hiring manager) (screen|interview|call)",
        r"invite you (to|for) (an? )?(interview|conversation|call)", r"interview (invitation|request)",
        r"(your|share your|send (me|us) your) availability", r"calendly\.com", r"goodtime\.io", r"\bavailable (times|slots)\b",
        r"(would|'d) (love|like) to (speak|talk|chat|connect) with you", r"wants? to interview you",
        r"interview (is )?(confirmed|scheduled)", r"set for your (\w+ )?interview",
    ]),
    ("consideration", [
        r"(received|reviewing|review) (your|the) application", r"thank(s| you) for (applying|your application|your interest)",
        r"application (was|has been) (received|submitted|sent|viewed)", r"your application was (sent|viewed)",
        r"(under|in) review", r"we('ll| will) (be in touch|review|get back)", r"application (status|update)",
    ]),
]
COMPILED = [(st, [re.compile(p, re.I) for p in ps]) for st, ps in RULES]

# Job-alert and marketing mail that mentions applying but isn't about one of your applications.
NOISE = re.compile(r"job alert|jobs? (you may|for you|recommended|matching)|new jobs|recommended for you|"
                   r"is hiring|people also viewed|daily digest|weekly digest|unsubscribe from job", re.I)
NOISE_SENDERS = re.compile(r"jobalerts|jobs-listings|jobs-noreply@linkedin|alert@indeed|newsletter|marketing", re.I)

ACTION = {"interview", "assessment", "offer"}
RANK = {"applied": 0, "consideration": 1, "assessment": 2, "interview": 3, "offer": 4, "rejected": 5}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def classify(email):
    """(status, matching phrase) for one email; status is None when it isn't about an application."""
    subject, body, sender = email.get("subject") or "", email.get("body") or "", email.get("from") or ""
    text = f"{subject}\n{body[:6000]}"
    for st, pats in COMPILED:
        for p in pats:
            for m in p.finditer(text):
                if st in ("interview", "assessment") and CONDITIONAL.search(sentence(text, m)):
                    continue  # "if you're a fit we'll reach out to schedule a call" is a receipt, not an invitation
                if st == "consideration" and (NOISE.search(subject) or NOISE_SENDERS.search(sender)):
                    return None, ""
                return st, m.group(0)
    return None, ""


CONDITIONAL = re.compile(r"\b(if|should|once|whether|may|might|in case|in the event)\b", re.I)


def sentence(text, m):
    """The sentence around a match."""
    start = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start()), text.rfind("!", 0, m.start())) + 1
    ends = [i for i in (text.find(".", m.end()), text.find("\n", m.end())) if i != -1]
    return text[start:min(ends) if ends else len(text)]


def _date_from(day, month, year, ref):
    try:
        d = datetime(year or ref.year, month, day)
    except ValueError:
        return None
    if not year and d.date() < ref.date() - timedelta(days=30):  # "Jan 5" written in December means next year
        d = d.replace(year=d.year + 1)
    return d.date()


DEADLINE_CUE = r"(?:deadline|due|by|before|no later than|expires?|until|complete[^.]{0,40}?(?:by|within)|within|in the next)"
MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"


def deadline(email, interview=False):
    """(date, the text it came from) for the first deadline-like phrase, else (None, '').
    For interviews this is the interview date ("on March 3, 2027 at 10:00AM")."""
    ref = parse_date(email.get("date")) or datetime.now()
    text = re.sub(r"\s+", " ", f"{email.get('subject') or ''}. {(email.get('body') or '')[:6000]}")
    cue = DEADLINE_CUE[:-1] + "|on|for|at)" if interview else DEADLINE_CUE
    for m in re.finditer(cue + r"\s*[:\-]?\s*(?:[a-z]+,?\s+)?(?:the\s+)?" + r"(" + MONTH_RE + r"|(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?)"
                         + r"((?:,? at)? \d{1,2}(?::\d\d)?\s?[ap]\.?m\.?(?: \w+ time)?)?", text, re.I):
        g = m.groups()
        if g[1]:
            d = _date_from(int(g[2]), MONTHS[g[1][:3].lower()], int(g[3]) if g[3] else None, ref)
        else:
            y = int(g[6]) if g[6] else None
            d = _date_from(int(g[5]), int(g[4]), (y + 2000 if y and y < 100 else y), ref)
        if d:
            return d, m.group(0).strip()
    m = re.search(r"(?:within|in the next)\s+(\d{1,2}|one|two|three|five|seven)\s+(business\s+)?(days?|hours?)", text, re.I)
    if m:
        n = {"one": 1, "two": 2, "three": 3, "five": 5, "seven": 7}.get(m.group(1).lower()) or int(m.group(1))
        days = n if m.group(3).lower().startswith("day") else max(1, n // 24)
        if not m.group(2):
            return (ref + timedelta(days=days)).date(), m.group(0).strip()
        d = ref.date()
        while days:  # business days: skip Saturdays and Sundays
            d += timedelta(days=1)
            days -= d.weekday() < 5
        return d, m.group(0).strip()
    return None, ""


def sender_parts(sender):
    """('Acme Hiring Team', 'acme.com') from 'Acme Hiring Team <no-reply@acme.com>'."""
    s = (sender or "").strip()
    if "<" not in s:
        return "", s.split("@")[-1].lower() if "@" in s else ""
    m = re.match(r'\s*"?([^"<]*)"?\s*<([^>]*)>', s)
    name, addr = (m.group(1).strip(), m.group(2).strip()) if m else ("", s)
    return name, addr.split("@")[-1].lower() if "@" in addr else ""


TITLE_STOP = {"product", "manager", "senior", "sr", "pm", "of", "and", "the", "a", "for", "i", "ii", "iii", "remote", "us", "lead"}
ATS_DOMAINS = re.compile(r"greenhouse|lever\.co|ashbyhq|myworkday|workable|rippling|smartrecruiters|icims|gem\.com|"
                         r"recruitee|jobvite|bamboohr|clearcompany|avahr|taleo|successfactors|gmail|outlook|"
                         r"rembrandt|hackerrank|codesignal|hirevue|testgorilla|criteriacorp|codility", re.I)  # last row: assessment vendors
ROLE_AT = re.compile(r"(?:role|position|opening) at ([A-Z0-9][\w&.' -]{1,40}?)!?$", re.I)  # "...the X role at Company": checked first
COMPANY_IN_SUBJECT = re.compile(r"(?:appl(?:ying|ication|ied)|interest)\s+(?:to|at|with|for)\s+(?:the\s+)?([A-Z0-9][\w&.' -]{1,40}?)(?:[!|:,.]| for | - |$)"
                                r"|^\[?([A-Z0-9][\w&.' ]{1,30}?)\]? (?:received your application|// )|update from ([\w&.' -]{2,40})$"
                                r"|your ([A-Z0-9][\w&.' -]{1,30}?) application\b| at ([A-Z0-9][\w&.' -]{1,30})$", re.I)
NOT_A_COMPANY = re.compile(r"^(we|we've|we have|you|your|thank you|thanks|our|the|a|an|my)$", re.I)
TITLE_IN_TEXT = re.compile(r"(?:for|to) the ([A-Z][^.\n]{3,80}?) (?:position|role|opening)|the (?:role|position) of ([A-Z][^.\n]{3,80}?) at ")


def guess_job(email):
    """(company, title) for an email about an application that isn't in the log."""
    subj, body = email.get("subject") or "", email.get("body") or ""
    m = ROLE_AT.search(subj) or COMPANY_IN_SUBJECT.search(subj)
    company = next((g for g in m.groups() if g), "").strip(" .") if m else ""
    if NOT_A_COMPANY.match(company):
        company = ""
    if not company:
        local = (email.get("from") or "").split("<")[-1].split("@")[0].strip(' "')
        name, domain = sender_parts(email.get("from"))
        if domain == "myworkday.com":  # Workday mails come from <company>@myworkday.com
            company = local.upper() if len(local) <= 5 else local.capitalize()
        elif domain and not ATS_DOMAINS.search(domain):
            company = domain.split(".")[-2].capitalize() if domain.count(".") >= 1 else domain
        elif name and not re.search(r"no.?reply|notification|talent|recruit|hiring|careers", name, re.I):
            company = name
    t = TITLE_IN_TEXT.search(body) or TITLE_IN_TEXT.search(subj)
    return company, (next(g for g in t.groups() if g).strip() if t else "")


def match(email, apps, state=None, status=None):
    """The submitted application this email is about, or None. Sender/subject matches count most;
    a body-only match also needs a word of the job title, since many company names are ordinary words.
    Same email thread as an earlier match wins outright; otherwise a role already in assessment/interview
    beats other roles at that company when the email doesn't name the role."""
    known = (state or {}).get("apps", {})
    tid = email.get("thread_id")
    for k, s in known.items():
        if tid and k in apps and any(h.get("thread_id") == tid for h in s.get("history", [])):
            return apps[k]
    name, domain = sender_parts(email.get("from"))
    subj, body = norm(email.get("subject")), norm((email.get("body") or "")[:6000])
    sender_n, dom = norm(name), domain.replace("-", "")
    sent = parse_date(email.get("date"))
    latest_applied = (sent + timedelta(days=2)).date().isoformat() if sent else "9999"
    best, best_score = None, 0
    for a in apps.values():
        c = norm(a["company"])
        if len(c) < 3 or (a["date"] or "") > latest_applied:
            continue  # can't be about an application logged after the email arrived (2 days' slack for late logging)
        pat = re.compile(r"\b" + re.escape(c) + r"\b")
        score = 0
        if pat.search(sender_n) or c.replace(" ", "") in dom:
            score = 3
        elif pat.search(subj):
            score = 2
        elif pat.search(body):
            title_words = {w for w in norm(a["title"]).split() if len(w) > 3 and w not in {"product", "manager", "senior"}}
            score = 1 if title_words & set(body.split()) or "product" in body else 0
        if not score:
            continue
        # several roles at one company: prefer the one whose title words the email uses
        tw = set(norm(a["title"]).split()) - TITLE_STOP - set(c.split())  # "Acme Payments": "acme" says nothing about the role
        if tw:
            score += 0.9 * len(tw & set(f"{subj} {body}".split())) / len(tw)
        score += len(c) / 1000  # prefer the longer, more specific company name
        cur = known.get(a["key"], {}).get("status")
        if status == "consideration":  # receipts arrive the day you apply: the role sent that day
            score += 0.2 if sent and a["date"] and abs((sent.date() - date.fromisoformat(a["date"])).days) <= 1 else 0
            score += 0.05 if sent and a["date"] and a["date"] <= sent.date().isoformat() else 0  # already sent by then
        elif cur in ACTION:  # news that doesn't name the role: the furthest-along role (interview beats assessment)
            score += 0.2 + 0.05 * RANK[cur]
        if score > best_score or (score == best_score and best and a["date"] > best["date"]):
            best, best_score = a, score
    return best if best_score >= 1 else None


def load(paths):
    try:
        return json.loads(paths.status.read_text())
    except (OSError, ValueError):
        return {"apps": {}, "unmatched": [], "seen": []}


def save(state, paths):
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    state["seen"] = state["seen"][-5000:]
    paths.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = paths.status.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(paths.status)


def resume_for(key, paths):
    import resumes
    for r in reversed(resumes.rows(paths)):
        if app_key(r["company"], r["title"]) == key:
            return {"name": r["resume"], "file": r["file"], "source": r["source"]}
    return None


def close_assessments(email, ev, state):
    """An assessment vendor's "completed" email (Rembrandt, HackerRank...) rarely names the employer. Close every open
    assessment whose request came from that vendor (some employers send one test for several roles). True if any."""
    vendor = sender_parts(email.get("from"))[1]
    closed, companies = False, set()
    for k, s in state["apps"].items():
        if s["status"] != "assessment":
            continue
        words = {w for h in s.get("history", []) for w in norm(h.get("subject", "") + " " + h.get("from", "")).split() if len(w) > 4}
        if any(w in vendor for w in words):
            companies.add(s["company"])
    for k, s in state["apps"].items():  # one test often covers every role at that company
        if s["status"] == "assessment" and s["company"] in companies:
            s["history"] = (s["history"] + [dict(ev, company=s["company"], title=s["title"], key=k)])[-30:]
            s.update(status="consideration", status_date=ev["date"], subject=ev["subject"], url=ev["url"], deadline=None, deadline_text="")
            closed = True
    if closed:
        ev.update(company="(assessment vendor)", title="assessment completed")
    return closed


def ingest(emails, paths=None):
    """Apply a batch of emails; returns the list of new events (one per job-related email)."""
    paths = paths or Paths()
    state, apps = load(paths), submitted_apps(paths)
    for k, s in state["apps"].items():  # applications known only from email (not in the log) must stay matchable in later runs
        if not s.get("in_log", True) and k not in apps:
            apps[k] = {"key": k, "company": s["company"], "title": s["title"], "date": "", "notes": "", "logged": False}
    seen, events = set(state["seen"]), []
    try:  # recruiter outreach threads are tracked by recruiters.py; their quoted text would mis-match applications here
        recruiter_threads = set(json.loads((paths.apps / "recruiter_leads.json").read_text()).get("leads", {}))
    except (OSError, ValueError):
        recruiter_threads = set()
    for e in sorted(emails, key=lambda e: parse_date(e.get("date")) or datetime.min):
        if e.get("id") in seen:
            continue
        state["seen"].append(e.get("id"))
        seen.add(e.get("id"))
        if e.get("thread_id") in recruiter_threads:
            continue
        st, why = classify(e)
        if not st:
            continue
        when = parse_date(e.get("date")) or datetime.now()
        dl, dl_text = deadline(e, interview=st == "interview") if st in ("assessment", "interview") else (None, "")
        ev = {"status": st, "date": when.isoformat(timespec="minutes"), "subject": e.get("subject", ""),
              "from": e.get("from", ""), "url": e.get("url", ""), "thread_id": e.get("thread_id", ""), "why": why,
              "deadline": dl.isoformat() if dl else None, "deadline_text": dl_text}
        done = st == "done"  # "assessment completed": stored as consideration, but may close an open assessment
        if done:
            st = ev["status"] = "consideration"
        a = match(e, apps, state, st)
        if done and not a and close_assessments(e, ev, state):
            events.append(ev)
            continue
        t = guess_job(e)[1] if a else ""
        known_company = a["company"] if a else ""
        if a and t and not a.get("logged", True) and norm(t) != norm(a["title"]):
            a = None  # an earlier guessed entry: a different role, or one that didn't name it (merged below)
        elif a and t and a.get("logged", True):
            # the email names a role sharing no distinctive word with the logged one: a different application at that company
            stop = TITLE_STOP | set(norm(a["company"]).split()) | {"position", "role", "opening", "multiple", "remote"}
            logged = set(norm(re.sub(r"\(.*?\)", " ", a["title"])).split()) - stop  # "(Seattle)", "(SF onsite)" are our notes
            mentioned = set(norm(f"{e.get('subject', '')} {(e.get('body') or '')[:6000]}").split())
            if logged and not (logged & mentioned):
                a = None
        if not a:
            # an application missing from the shared log (applied by hand, or before the log): track it anyway
            company, title = guess_job(e)
            company = company or known_company  # split off from a logged application at the same company
            if not company:
                ev["company"] = sender_parts(e.get("from"))[0]
                state["unmatched"] = ([u for u in state["unmatched"] if u.get("url") != ev["url"]] + [ev])[-200:]
                events.append(ev)
                continue
            a = {"key": app_key(company, title), "company": company, "title": title or "(role not named)", "date": "", "logged": False}
            unnamed = app_key(company, "(role not named)")
            if title and unnamed in state["apps"] and a["key"] not in state["apps"]:  # the receipt didn't name the role; this one does
                state["apps"][a["key"]] = dict(state["apps"].pop(unnamed), title=title)
                apps.pop(unnamed, None)
            apps[a["key"]] = dict(a, notes="")  # later emails about it in this batch match it too
        cur = state["apps"].setdefault(a["key"], {"company": a["company"], "title": a["title"], "applied": a["date"],
                                                  "status": "applied", "status_date": a["date"], "history": [],
                                                  "in_log": a.get("logged", True)})
        cur["history"] = (cur["history"] + [ev])[-30:]
        newer = ev["date"] >= (cur.get("status_date") or "")
        # a receipt never undoes real news, and nothing but an offer follows a rejection
        downgrade = (st == "consideration" and cur["status"] != "applied" and not (done and cur["status"] == "assessment")) or \
                    (cur["status"] in ("rejected", "offer") and RANK[st] < RANK[cur["status"]])
        if newer and not downgrade:
            cur.update(status=st, status_date=ev["date"], subject=ev["subject"], url=ev["url"],
                       deadline=ev["deadline"] or (cur.get("deadline") if cur["status"] == st else None),
                       deadline_text=ev["deadline_text"] or (cur.get("deadline_text", "") if cur["status"] == st else ""))
        cur["resume"] = resume_for(a["key"], paths)
        ev.update(company=a["company"], title=a["title"], key=a["key"], resume=cur["resume"])
        events.append(ev)
    save(state, paths)
    return events


def upcoming(state, on=None):
    """Interviews/assessments still open: deadline today or later, or no deadline and heard within 14 days."""
    on = on or today()
    out = []
    for k, a in state["apps"].items():
        if a["status"] not in ACTION:
            continue
        if a.get("deadline"):
            if a["deadline"] >= on.isoformat():
                out.append(dict(a, key=k))
        elif a["status_date"][:10] >= (on - timedelta(days=14)).isoformat():
            out.append(dict(a, key=k))
    return sorted(out, key=lambda a: (a.get("deadline") or "9999", a["company"]))


def digest(events, paths=None, on=None):
    """Markdown for the morning, plus the one-line notification text (empty when nothing needs action)."""
    paths, on = paths or Paths(), on or today()
    state = load(paths)
    todo = upcoming(state, on)
    new_unmatched = [e for e in events if "key" not in e and e["status"] in ACTION]
    counts = {}
    for a in state["apps"].values():
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    lines = [f"# Job inbox — {on:%A, %b %d}", ""]
    if todo or new_unmatched:
        lines += ["## Needs you", ""]
        for a in todo:
            due = f" — **{'on' if a['status'] == 'interview' else 'due'} {a['deadline']}**" if a.get("deadline") else ""
            res = a.get("resume") or {}
            lines.append(f"- **{a['status'].upper()}** · {a['company']} — {a['title']}{due}"
                         + ("" if a.get("in_log", True) else " _(not in the application log)_"))
            lines.append(f"  - Email: [{a.get('subject', '')}]({a.get('url', '')})"
                         + (f" · deadline text: “{a['deadline_text']}”" if a.get("deadline_text") else ""))
            lines.append(f"  - Resume you applied with: {res.get('name', 'not recorded')}" + (f" (`{res['file']}`)" if res.get("file") else "")
                         + (" — best guess from the date, not recorded at the time; check before the interview" if "by date" in res.get("source", "") else ""))
        for e in new_unmatched:
            due = f" — **due {e['deadline']}**" if e.get("deadline") else ""
            lines.append(f"- **{e['status'].upper()}** · {e['company'] or e['from']} (not matched to a logged application){due}")
            lines.append(f"  - Email: [{e['subject']}]({e['url']})")
        lines.append("")
    new = [e for e in events if "key" in e]
    if new:
        lines += ["## New since last check", ""]
        for e in new:
            lines.append(f"- {e['status']}: {e['company']} — {e['title']} · [{e['subject']}]({e['url']})")
        lines.append("")
    lines += ["## All applications heard from", "", " · ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda kv: RANK[kv[0]])) or "none yet", ""]
    md = "\n".join(lines)
    paths.digest.write_text(md)
    note = ""
    if todo or new_unmatched:
        groups = {}  # one line per company/status/date: some employers send one assessment per role but it's taken once
        for x in todo + new_unmatched:
            k = (x["status"], x.get("company") or "unknown", x.get("deadline") or "")
            groups[k] = groups.get(k, 0) + 1
        parts = [f"{st}: {co}" + (f" ×{n}" if n > 1 else "") + (f" ({'on' if st == 'interview' else 'due'} {dl[5:]})" if dl else "")
                 for (st, co, dl), n in groups.items()]
        note = f"{len(parts)} to act on — " + "; ".join(parts)
    return md, note


def should_notify(events, paths, on=None):
    """The scan runs every few hours: pop up once a day, and again only when a new interview/assessment/offer arrives."""
    on = (on or today()).isoformat()
    state = load(paths)
    fresh = any(e["status"] in ACTION for e in events)
    if fresh or state.get("notified_on") != on:
        state["notified_on"] = on
        save(state, paths)
        return True
    return False


def notify(text):
    """macOS banner; failures are ignored (the digest file still has everything)."""
    if not text or sys.platform != "darwin":  # macOS only; elsewhere the digest file is the notice
        return
    script = f'display notification {json.dumps(text[:230])} with title "Job inbox" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("file")
    i.add_argument("--notify", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("query", nargs="*")
    a = ap.parse_args(argv)
    paths = Paths()
    if a.cmd == "ingest":
        events = ingest(json.loads(Path(a.file).read_text()), paths)
        md, note = digest(events, paths)
        print(md)
        if a.notify and should_notify(events, paths):
            notify(note)
    else:
        q = norm(" ".join(a.query))
        for k, x in sorted(load(paths)["apps"].items(), key=lambda kv: kv[1]["status_date"], reverse=True):
            if q in k:
                print(f"{x['status']:13} {x['status_date'][:10]}  {x['company']} — {x['title']}"
                      + (f"  (due {x['deadline']})" if x.get("deadline") else ""))


if __name__ == "__main__":
    main()
