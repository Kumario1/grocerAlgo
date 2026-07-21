"""Fuzzy-match free-text list lines against directory entries (plan.md §8.2)."""
import re
from rapidfuzz import process, fuzz, utils

THRESHOLD = 80
_QTY = re.compile(r"^\s*\d+\s*(x|lbs?|oz|kg|g|pack|cans?|bottles?)?\s+", re.I)

def normalize(line):
    return _QTY.sub("", line).strip()

def resolve(lines, directory):
    entries = list(directory)
    matched, unmatched = [], []
    for line in lines:
        q = normalize(line)
        if not q:
            continue
        # processor lowercases both sides — "rice" vs "Rice" must be exact, not 75
        hits = process.extract(q, entries, scorer=fuzz.token_set_ratio, limit=3,
                               processor=utils.default_process)
        best = hits[0] if hits else None
        if best and best[1] >= THRESHOLD:
            matched.append({"query": line, "entry": best[0],
                            "anchors": directory[best[0]], "score": best[1]})
        else:
            unmatched.append({"query": line,
                              "suggestions": [h[0] for h in hits]})
    return matched, unmatched
