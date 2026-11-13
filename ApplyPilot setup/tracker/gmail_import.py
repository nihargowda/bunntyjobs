#!/usr/bin/env python3
"""Turn Gmail connector output (search_threads / get_thread JSON) into mail_status.py's email list.

  gmail_import.py OUT.json IN1.json [IN2.json ...]

Each input is what the connector returned: {"threads": [...]} from search_threads, a single thread from
get_thread, or a list of either. Messages you sent yourself are dropped; when get_thread output for a
message is included, its full plain-text body replaces the search snippet. Repeat runs merge into OUT.
"""
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import SETTINGS  # noqa: E402

ME = SETTINGS["gmail_address"].lower()  # your own address (config/settings.json); your sent mail is skipped


def messages(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from messages(x)
    elif isinstance(obj, dict):
        if "threads" in obj:
            yield from messages(obj["threads"])
        elif "messages" in obj:
            for m in obj["messages"]:
                yield m


def convert(objs, me=ME):
    out = {}
    for obj in objs:
        for m in messages(obj):
            if "SENT" in (m.get("labelIds") or []) or (m.get("sender") or "").lower() == me:
                continue
            body = m.get("plaintextBody") or m.get("plaintext_body") or m.get("snippet") or ""
            prev = out.get(m["id"])
            if prev and len(prev["body"]) >= len(body):
                continue
            out[m["id"]] = {"id": m["id"], "thread_id": m.get("threadId", ""), "date": m.get("date", ""),
                            "from": m.get("sender", ""), "subject": html.unescape(m.get("subject") or ""),
                            "body": html.unescape(body), "url": m.get("viewUrl", "")}
    return list(out.values())


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    out = Path(argv[0])
    objs = [json.loads(Path(p).read_text()) for p in argv[1:]]
    if out.exists():
        objs.insert(0, json.loads(out.read_text()))  # earlier batch: already in our format
        objs[0] = [{"messages": [{"id": e["id"], "threadId": e["thread_id"], "date": e["date"], "sender": e["from"],
                                  "subject": e["subject"], "plaintextBody": e["body"], "viewUrl": e["url"]}]} for e in objs[0]]
    emails = convert(objs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(emails, indent=1, ensure_ascii=False))
    print(f"{len(emails)} emails -> {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
