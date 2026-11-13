#!/usr/bin/env python3
"""Queue an application batch every time 10 more jobs are ready to apply to.

Run by ~/.applypilot/pipeline.sh after each scoring pass (free: no browser, no AI calls).
  0. recover_links.py: finds company apply links for up to 40 LinkedIn postings (no browser, no AI)
  1. rebuilds the filtered apply queue (build_queue.py: your settings, dead links dropped)
  2. counts jobs nobody has claimed, tried, or found dead yet
  3. if at least BATCH are ready and no batch is already queued or in progress, queues a request for
     both agents, exactly like the dashboard's Apply now button (each agent applies to up to 10)
The daily target in apply_control.request() still applies.
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agent_brief  # noqa: E402  (ready = queue minus claimed / tried / dead)
import apply_control  # noqa: E402

BATCH = 10


def main():
    # free link recovery first (public LinkedIn pages + company job-board APIs), so agents spend
    # browser time applying instead of hunting for links; capped so a pass stays a few minutes
    found = subprocess.run([sys.executable, str(HERE / "recover_links.py"), "--limit", "40"],
                           capture_output=True, text=True, cwd=HERE)
    lines = (found.stdout or found.stderr).strip().splitlines()
    print(lines[-1] if lines else "recover links: no output")
    built = subprocess.run([sys.executable, str(HERE / "build_queue.py"), "0"], capture_output=True, text=True, cwd=HERE)
    if built.returncode != 0:
        print(f"apply trigger: queue rebuild failed: {built.stderr.strip()[-300:]}")
        return
    ready = agent_brief.brief(limit=10_000)["jobs"]  # count them all
    pending = [r["agent"] for r in apply_control.status()["requests"] if r["state"] in ("queued", "active")]
    stamp = datetime.now().strftime("%H:%M:%S")
    if pending:
        print(f"{stamp} apply trigger: {len(ready)} ready; batch already waiting/running for {', '.join(pending)}")
    elif len(ready) < BATCH:
        print(f"{stamp} apply trigger: {len(ready)} of {BATCH} ready; waiting for more scores")
    else:
        print(f"{stamp} apply trigger: {len(ready)} ready -> {apply_control.request()}")
        print(json.dumps([f"{j['company']} | {j['title']}" for j in ready]))


if __name__ == "__main__":
    main()
