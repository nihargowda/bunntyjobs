"""Small, local-only work brief; avoids sending full queues/history to an agent."""
import argparse
import json
from pathlib import Path
import apply_control
from job_url import normalize

HERE = Path(__file__).resolve().parent

def brief(limit=5):
    state = apply_control.status()
    try:
        queue = json.loads((HERE / 'queue.json').read_text())
    except (OSError, ValueError):
        queue = []
    tried = set()
    for name in ('tried.txt', 'dead.txt'):
        try:
            tried.update(normalize(line.strip()) for line in (HERE / name).read_text().splitlines() if line.strip())
        except OSError:
            pass
    with apply_control.connect() as conn:
        claimed = {normalize(r['url']) for r in conn.execute('SELECT url FROM claims')}
    picked, seen = [], set()
    for job in queue:
        link = job.get('apply', '')
        key = normalize(link)
        if not link or key in tried or key in claimed or key in seen:
            continue
        seen.add(key)
        picked.append({k: job.get(k) for k in ('company', 'title', 'score', 'location', 'salary', 'apply', 'resume', 'saas_pm')})
        if len(picked) >= limit:
            break
    return dict(day=state['day'], submitted=state['submitted'], target=state['target'],
                remaining=state['remaining'], agents={r['agent']:r['state'] for r in state['requests']},
                jobs=picked, next='Claim each job before browser work.' if picked else 'No cached candidates. Rebuild the queue once; recover up to 3 links if still empty.')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error('--limit must be between 1 and 10')
    print(json.dumps(brief(args.limit), separators=(',', ':'), ensure_ascii=False))
