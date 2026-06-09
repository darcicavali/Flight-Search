#!/usr/bin/env python3
"""
UserPromptSubmit hook — nudges toward starting a fresh session when the
current one has grown large (token-efficiency).

Claude Code re-sends the whole transcript every turn, so cost grows with
session length. This warns (it never blocks) once the transcript passes a
size threshold, recommending: finish + log the current change in
docs/PROJECT_LOG.md, then /clear (or start a new session) and resume.

Token figure is a rough estimate (bytes/4) of the on-disk transcript, which
overestimates live context after auto-compaction — that's fine; a long
transcript is a good signal to checkpoint regardless.
"""
import sys, json, os

WARN_TOKENS = 120_000    # ~60% of a 200k window
STRONG_TOKENS = 160_000  # ~80%

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    tp = data.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return
    try:
        est = os.path.getsize(tp) // 4
    except OSError:
        return
    if est < WARN_TOKENS:
        return
    k = est // 1000
    level = "🟥 Session is very large" if est >= STRONG_TOKENS else "🟧 Session is getting large"
    print(
        f"{level} (~{k}k tokens of history; cost is re-paid every turn). "
        "For token-efficiency: wrap up the current change, make sure it's recorded in "
        "docs/PROJECT_LOG.md, then run /clear (or start a new session) and resume from the log."
    )

main()
