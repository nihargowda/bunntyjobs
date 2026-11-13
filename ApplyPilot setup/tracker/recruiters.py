#!/usr/bin/env python3
"""Recruiter outreach: find recruiter emails about roles, screen them, and track replies.

Each thread is one lead in Applications/recruiter_leads.json with a status:
  read        new pitch (or a new recruiter message) that passed the hard screen; waiting for a fit check
  not_match   failed the screen or the fit check (reason saved)
  match       good fit; a reply should be drafted
  drafted     reply drafted in Gmail and ready for you to review and send
  sent        the last message in the thread is yours
  needs_you   something only you can do (e.g. sign an RTR / right-to-represent)

  recruiters.py scan RAW.json [...]   update leads from Gmail connector get_thread output
  recruiters.py brief                 leads waiting for a fit check, with what the recruiter asked
  recruiters.py facts                 the approved facts and standard answers for replies
  recruiters.py assess THREAD match|not_match --why "..."
  recruiters.py lint DRAFT.txt --thread THREAD   check a draft uses only approved facts
  recruiters.py drafted THREAD --draft-id ID [--url URL]
  recruiters.py list [--status S]
  recruiters.py notify                pop up a banner for newly drafted replies / things that need you
Nothing here sends email.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import Paths, parse_date  # noqa: E402
from gmail_import import ME, messages  # noqa: E402

# the only facts drafts may use: config/candidate_facts.json (falls back to the committed example)
_CONFIG = Path(__file__).resolve().parent.parent / "config"
FACTS = next((p for p in (_CONFIG / "candidate_facts.json", _CONFIG / "candidate_facts.example.json") if p.exists()),
             _CONFIG / "candidate_facts.json")
STATUSES = ("read", "not_match", "match", "drafted", "sent", "needs_you")

# --- recognising a recruiter pitch -------------------------------------------------------------
NOISE_SENDER = re.compile(r"no.?reply|do.?not.?reply|notification|jobalerts?|alerts?\.|newsletter|digest|marketing|"
                          r"hi\.wellfound|wonsulting|jobscan|resumeworded|greenhouse|lever\.co|ashbyhq|myworkday|"
                          r"workable|rippling|smartrecruiters|icims|applytojob|hirehive|clearcompany", re.I)
JD_CUES = re.compile(r"job (title|description|summary)|position (summary|details|description)|\bduration\b|pay range|"
                     r"\brate\b|\bW-?2\b|\bC2C\b|corp[- ]to[- ]corp|\bcontract\b|requirement|\blocation\s*:|"
                     r"responsibilities|qualifications|my client|our client", re.I)
ASKS = re.compile(r"(updated|your) (resume|cv)|let me know|are you interested|if (you are|you're) interested|"
                  r"best time to (reach|call)|call me|reply with|share your|send (me )?your|interested\?|"
                  r"good fit|right to represent|\bRTR\b", re.I)
RTR = re.compile(r"right to represent|\bRTR\b", re.I)

# --- hard screen (your standing rules) ---------------------------------------------------------
TARGET_TITLE = re.compile(r"product (manager|owner|management|operations|analyst|specialist)|\bproduct owner\b|"
                          r"program manager|project manager|\btpm\b|technical program|program & project|"
                          r"business (systems |process )?analyst|\bscrum\b|agile coach|release (manager|train)", re.I)
FAILS = [
    (re.compile(r"\b(USC|US citizens?|U\.S\. citizens?|green ?card|GC)\b[^.\n]{0,40}\b(only|holders? only|required|must)|"
                r"\bonly\b[^.\n]{0,20}\b(USC|US citizens?|green ?card|GC)\b|need (a )?(GC|USC)\b", re.I),
     "US citizen / green card only"),
    (re.compile(r"no (visa )?sponsorship|(cannot|can't|unable to|will not|won't) sponsor|security clearance|"
                r"\bclearance (required|needed)|active (secret|ts)", re.I), "no sponsorship / clearance required"),
    (re.compile(r"\b(C2C|corp[- ]to[- ]corp|1099) only\b|only (on )?(C2C|corp[- ]to[- ]corp)", re.I), "C2C only (you need W2)"),
    (re.compile(r"\b(christian|faith[- ]based|church|ministry|community college|construction)\b", re.I),
     "excluded industry (faith-based, community college or construction)"),
    (re.compile(r"\b(india|canada|united kingdom|\buk\b|philippines|mexico)\b[^.\n]{0,15}(only|based|location)", re.I),
     "outside the US"),
]
SENIOR = re.compile(r"\b(director|principal|vp|vice president|head of|chief)\b", re.I)
LEAD = re.compile(r"\blead\b", re.I)


def clean(text):
    """Strip link junk and table pipes so the job description reads as text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[|*#]+", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def field(text, *names, n=80):
    for name in names:
        m = re.search(rf"{name}[ \t]*[:\-–][ \t]*([^\n]{{2,{n}}})", text, re.I)  # value on the same line only
        if m:
            v = re.split(r"\s{2,}| (?=[A-Z][a-z]+ ?[A-Za-z]*\s*:)", m.group(1).strip(" .*"))[0]
            return v.strip(" .*")
    return ""


def extract(subject, body):
    """Role, location, pay, employment type and duration from a recruiter pitch."""
    t = clean(body)
    title = field(t, "job title", "position title", "position", "role", n=70)
    if not title or len(title.split()) > 10:
        m = re.search(r"(?:position|role|opening|opportunity) (?:for|as) (?:an? |the )?([A-Z][^,.\n]{3,70}?)(?: with| at| in| located|,|\.|\s{2})", t)
        title = m.group(1).strip() if m else ""
    if not title:
        s = re.sub(r"(?i)^(re:|fw:|fwd:)\s*|w2 position:|employment opportunity|job opening|new requirement|urgent need|immediate position|"
                   r"complete job details|\|\|.*?\|\||[-–:|]+", " ", subject or "")
        title = re.sub(r"\s+", " ", s).strip(" -")[:70]
    location = field(t, "location", n=60)
    if not location:
        m = re.search(r"located in ([A-Z][\w .,-]{2,40}?)(?:,? that|\.|\n)", t)
        location = m.group(1).strip() if m else ""
    pay = ""
    m = re.search(r"\$\s?\d[\d,.]*\s*[kK]?(?:\s*[-–]\s*\$?\s?\d[\d,.]*\s*[kK]?)?\s*(?:/|per\s*)?\s*(?:hr|hour|h\b|yr|year|annum)(?:\s*(?:on\s+)?(?:W-?2|C2C|1099)\b)?", t, re.I)
    if m:
        pay = m.group(0).strip()
    kinds = [k for k, p in (("W2", r"\bW-?2\b"), ("C2C", r"\bC2C\b|corp[- ]to[- ]corp"), ("1099", r"\b1099\b"),
                            ("contract", r"\bcontract\b|duration"), ("full-time", r"full[- ]time|permanent|\bFTE\b")) if re.search(p, t, re.I)]
    return {"title": title, "location": location, "pay": pay, "type": ", ".join(kinds), "duration": field(t, "duration", n=40)}


def screen(lead, body):
    """(passed, reasons). Hard rules only; the fit check against the resume happens later."""
    t = clean(body)
    why = [msg for pat, msg in FAILS if pat.search(f"{lead['title']}\n{t}")]
    title = lead["title"] or ""
    if LEAD.search(title):
        why.append("title has \"Lead\" (your rule: no Lead roles)")
    if SENIOR.search(title):
        why.append("too senior (director / principal / head)")
    if title and not TARGET_TITLE.search(title):
        why.append("not a product / program / project / BA / Scrum role")
    yrs = [int(a) for a, _ in re.findall(r"(\d{1,2})\s*\+?\s*(?:[-–]|to)?\s*(\d{1,2})?\s*\+?\s*years", t)]
    if yrs and max(yrs) >= 8:  # the headline requirement ("6 to 8 years ... 2 as Scrum Master" -> 6), not the smallest
        why.append(f"asks for {max(yrs)}+ years (you have 5+)")
    hourly = [float(x) for x in re.findall(r"\$\s?(\d{2,3}(?:\.\d+)?)\s*(?:[-–]\s*\$?\s?\d{2,3}\s*)?(?:/|per\s*)\s*(?:hr|hour|h\b)", t)]
    hi = re.findall(r"\$\s?\d{2,3}\s*[-–]\s*\$?\s?(\d{2,3})\s*(?:/|per\s*)\s*(?:hr|hour)", t)
    top = max([float(x) for x in hi] + hourly) if (hi or hourly) else None
    if top is not None and top < 38:
        why.append(f"pays ${top:g}/hr (under your $80k floor, about $38/hr)")
    return (not why), why


# --- state -------------------------------------------------------------------------------------
def path(paths):
    return paths.apps / "recruiter_leads.json"


def load(paths):
    try:
        return json.loads(path(paths).read_text())
    except (OSError, ValueError):
        return {"leads": {}}


def save(state, paths):
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    p = path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(p)


def set_status(lead, status, note=""):
    lead["history"] = (lead.get("history", []) + [{"at": datetime.now().isoformat(timespec="minutes"), "status": status, "note": note}])[-20:]
    lead["status"], lead["note"] = status, note


def thread_messages(obj):
    """Messages of one get_thread result, oldest first, each tagged mine=True/False."""
    out = []
    for m in messages(obj):
        mine = "SENT" in (m.get("labelIds") or []) or (m.get("sender") or "").lower().replace(".", "") == ME.replace(".", "")
        out.append({"id": m["id"], "thread_id": m.get("threadId", ""), "date": m.get("date", ""), "from": m.get("sender", ""),
                    "subject": m.get("subject") or "", "body": m.get("plaintextBody") or m.get("snippet") or "",
                    "url": m.get("viewUrl", ""), "mine": mine})
    return sorted(out, key=lambda x: parse_date(x["date"]) or datetime.min)


TITLE_FIELD = re.compile(r"(job title|position title|^\s*position|^\s*role)[ \t]*[:\-–]", re.I | re.M)


def is_pitch(msg):
    """A person (not an ATS or alert robot) describing a job and asking for interest; a "Job Title:" field counts as asking."""
    text = clean(msg["body"])
    return bool(not NOISE_SENDER.search(msg["from"]) and JD_CUES.search(text)
                and (ASKS.search(f"{msg['subject']}\n{msg['body']}") or TITLE_FIELD.search(text)))


def scan_thread(state, msgs):
    """Update (or create) the lead for one thread. Returns the lead, or None if it isn't recruiter outreach."""
    if not msgs:
        return None
    tid = msgs[0]["thread_id"]
    inbound = [m for m in msgs if not m["mine"]]
    lead = state["leads"].get(tid)
    if lead is None:
        pitch = next((m for m in inbound if is_pitch(m)), None)
        if not pitch:
            return None
        everything = "\n\n".join(m["body"] for m in inbound)  # the job details may be in an earlier message than the ask
        info = extract(inbound[0]["subject"], everything)
        lead = state["leads"][tid] = dict(info, thread_id=tid, recruiter=pitch["from"], subject=pitch["subject"],
                                          received=pitch["date"], url=pitch["url"], last_seen="", status="", note="", history=[])
        lead["jd"] = re.sub(r"\n\s*\n+", "\n", clean(everything)).strip()[:2500]  # enough to judge fit without refetching
        ok, why = screen(lead, everything)
        lead["screen"] = "passed" if ok else "; ".join(why)
        if RTR.search(f"{pitch['subject']}\n{pitch['body'][:3000]}"):  # an RTR often arrives as its own thread
            set_status(lead, "needs_you", "RTR (right to represent) to review and sign")
        else:
            set_status(lead, "read" if ok else "not_match", "" if ok else lead["screen"])
    last = msgs[-1]
    if last["date"] <= lead.get("last_seen", ""):
        return lead  # nothing new in this thread
    lead["last_seen"] = last["date"]
    lead["reply_to"] = inbound[-1]["id"] if inbound else ""
    lead["recruiter_email"] = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", inbound[-1]["from"] if inbound else lead["recruiter"])[-1:] or [""]
    lead["recruiter_email"] = lead["recruiter_email"][0]
    lead["asks"] = asks(inbound[-1]["body"]) if inbound else []
    lead["latest"] = re.sub(r"\n\s*\n+", "\n", clean(inbound[-1]["body"])).strip()[:800] if inbound else ""
    if last["mine"]:
        if lead["status"] != "sent":
            set_status(lead, "sent", "you replied" if lead["status"] != "drafted" else "you sent the draft")
    elif RTR.search(f"{last['subject']}\n{last['body'][:3000]}") and any(m["mine"] for m in msgs):
        set_status(lead, "needs_you", "RTR (right to represent) to review and sign")
    elif any(m["mine"] for m in msgs):  # you're already talking to them: their new message needs a look, whatever the screen said
        flag = f" (screen flagged: {lead['screen']})" if lead.get("screen", "passed") != "passed" else ""
        set_status(lead, "read", "recruiter replied; check what they asked" + flag)
    return lead


def scan(objs, paths=None):
    paths = paths or Paths()
    state = load(paths)
    touched = []
    for obj in objs:
        for thread in (obj if isinstance(obj, list) else [obj]):
            lead = scan_thread(state, thread_messages(thread))
            if lead:
                touched.append(lead["thread_id"])
    save(state, paths)
    return touched


# --- helpers for the scheduled run -------------------------------------------------------------
def asks(body):
    """Questions or form fields the recruiter wants answered ("Total Experience:", "W2 or C2C?")."""
    t = clean(body)
    lines = [l.strip(" -•\t") for l in t.splitlines()]
    q = [l for l in lines if 3 < len(l) < 120 and (l.endswith("?") or re.match(r"^[A-Z][\w /()&,-]{2,60}:\s*$", l))]
    return q[:15]


def facts():
    return {k: v for k, v in json.loads(FACTS.read_text()).items() if not k.startswith("_")}


def allowed_numbers(f, extra_text=""):
    nums = set(re.findall(r"\d[\d,./]*", json.dumps(f)))
    nums |= set(re.findall(r"\d[\d,./]*", extra_text))  # the recruiter's own numbers (their rate, req ID) may be quoted back
    return {n.strip(".,") for n in nums}


def lint(text, lead_text=""):
    """Problems with a draft: facts not in candidate_facts.json, banned words, wrong title. Empty list = OK."""
    f = facts()
    problems = []
    ok_nums = allowed_numbers(f, lead_text)
    for n in re.findall(r"\d[\d,./]*", text):
        n = n.strip(".,")
        if n and n not in ok_nums:  # exact only: "10" must not pass because "100,000" is allowed
            problems.append(f"number not in approved facts: {n}")
    low = text.lower()
    for w in f["never_say"]:
        if w.lower() in low:
            problems.append(f"says \"{w}\"")
    if re.search(r"\b(US citizen|green card holder|permanent resident)\b", text, re.I) and "not" not in low:
        problems.append("claims citizenship / green card")
    return problems


ANSWER_FOR = [  # recruiter question -> which standard answer fills it
    (r"total experience|years of experience|experience\s*:", "experience"),
    (r"work authori|authorized to work|visa", "work_authorization"),
    (r"sponsor", "sponsorship"),
    (r"\bw-?2\b|c2c|corp to corp|tax term|employment type", "employment_type"),
    (r"\brate\b|hourly", "w2_rate"),
    (r"salary|compensation|ctc", "salary"),
    (r"relocat|preferred location", "relocation"),  # before "zip": "Preferred Location (Zip Code)" wants this
    (r"current location|zip|where are you", "location"),
    (r"notice|start date|availability to start|when can you start", "notice"),
    (r"interview|call|availability", "interview_availability"),
]


def skeleton(lead, question_lines):
    """A starting draft: thanks, a placeholder for 2-3 fit lines, and approved answers to whatever they asked.
    Questions with no approved answer come out as [YOU: ...] so they're never guessed."""
    f, a = facts(), facts()["answers"]
    name = re.sub(r"[^A-Za-z].*$", "", (lead.get("recruiter") or "").split("<")[0].strip().split(" ")[0]) if "<" in (lead.get("recruiter") or "") else ""
    lines = [f"Hi {name}," if name else "Hi,", "",
             f"Thank you for reaching out about the {lead.get('title') or 'role'}. I'm interested and would like to learn more.", "",
             "[FIT: 2-3 sentences from candidate_facts highlights that match this role]", ""]
    for q in question_lines:
        key = next((k for pat, k in ANSWER_FOR if re.search(pat, q, re.I)), None)
        val = (f["experience"] if key == "experience" else (f"{f['location']}, {f['zip']}" if key == "location" else a.get(key))) if key else None
        lines.append(f"{q.rstrip(':?').strip()}: {val}" if val else f"{q.rstrip(':?').strip()}: [YOU: no approved answer]")
    lines += ["" if question_lines else "", "I've attached my resume. Happy to set up a call at your convenience.", "",
              "Best regards,", f["name"], f["phone"]]
    return "\n".join(l for i, l in enumerate(lines) if not (l == "" and i and lines[i - 1] == ""))


def brief(paths=None):
    """Leads waiting for a fit check, compact enough to read in one go."""
    paths = paths or Paths()
    out = []
    for tid, l in load(paths)["leads"].items():
        if l["status"] == "read":
            out.append({k: l.get(k) for k in ("thread_id", "reply_to", "recruiter_email", "subject", "title", "location", "pay", "type",
                                               "duration", "note", "asks", "latest", "jd", "url")})
    return out


def notify_text(paths=None, mark=True):
    paths = paths or Paths()
    state = load(paths)
    new = [l for l in state["leads"].values() if l["status"] in ("drafted", "needs_you") and not l.get("notified")]
    if not new:
        return ""
    if mark:
        for l in new:
            l["notified"] = True
        save(state, paths)
    d = [l for l in new if l["status"] == "drafted"]
    n = [l for l in new if l["status"] == "needs_you"]
    parts = []
    if d:
        parts.append(f"{len(d)} recruiter repl{'y' if len(d) == 1 else 'ies'} drafted: " + "; ".join(l["title"][:40] for l in d))
    if n:
        what = lambda l: "sign RTR" if "RTR" in l["note"] else l["note"][:40]
        parts.append("needs you: " + "; ".join(dict.fromkeys(f"{what(l)} ({l['title'][:45]})" for l in n)))
    return " · ".join(parts)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("files", nargs="+")
    sub.add_parser("brief")
    sub.add_parser("facts")
    a = sub.add_parser("assess")
    a.add_argument("thread")
    a.add_argument("verdict", choices=["match", "not_match"])
    a.add_argument("--why", default="")
    li = sub.add_parser("lint")
    li.add_argument("file")
    li.add_argument("--thread", default="")
    d = sub.add_parser("drafted")
    d.add_argument("thread")
    d.add_argument("--draft-id", required=True)
    d.add_argument("--url", default="")
    ls = sub.add_parser("list")
    ls.add_argument("--status", choices=STATUSES)
    sub.add_parser("notify")
    sk = sub.add_parser("skeleton")
    sk.add_argument("thread")
    args = ap.parse_args(argv)
    paths = Paths()

    if args.cmd == "scan":
        objs = [json.loads(Path(p).read_text()) for p in args.files]
        print(f"{len(set(scan(objs, paths)))} recruiter threads updated")
    elif args.cmd == "brief":
        print(json.dumps(brief(paths), indent=1, ensure_ascii=False))
    elif args.cmd == "facts":
        print(FACTS.read_text())
    elif args.cmd in ("assess", "drafted"):
        state = load(paths)
        lead = state["leads"].get(args.thread)
        if not lead:
            sys.exit(f"no lead for thread {args.thread}")
        if args.cmd == "assess":
            set_status(lead, args.verdict, args.why)
        else:
            lead.update(draft_id=args.draft_id, draft_url=args.url or "https://mail.google.com/mail/u/0/#drafts")
            set_status(lead, "drafted", "ready for you to review and send")
            lead.pop("notified", None)
        save(state, paths)
        print(f"{lead['title']}: {lead['status']}")
    elif args.cmd == "lint":
        lead = load(paths)["leads"].get(args.thread, {})
        probs = lint(Path(args.file).read_text(), json.dumps(lead))
        print("OK" if not probs else "\n".join(probs))
        sys.exit(1 if probs else 0)
    elif args.cmd == "list":
        for l in sorted(load(paths)["leads"].values(), key=lambda l: l["last_seen"], reverse=True):
            if not args.status or l["status"] == args.status:
                print(f"{l['status']:10} {l['last_seen'][:10]}  {l['title'][:45]:45} | {l['recruiter'][:30]:30} | {l.get('note', '')[:60]}")
    elif args.cmd == "skeleton":
        lead = load(paths)["leads"].get(args.thread)
        if not lead:
            sys.exit(f"no lead for thread {args.thread}")
        print(skeleton(lead, lead.get("asks", [])))
    elif args.cmd == "notify":
        text = notify_text(paths)
        if text and sys.platform == "darwin":  # macOS banner only; on Windows/WSL/Linux the printed text is the notice
            subprocess.run(["osascript", "-e", f'display notification {json.dumps(text[:230])} with title "Recruiter replies" sound name "Glass"'],
                           capture_output=True)
        print(text or "nothing new to notify")


if __name__ == "__main__":
    main()
