#!/usr/bin/env python3
"""
fuzzy.py
========
Tolerating the typos that real sheets and real chat messages contain, without
ever quietly guessing something important wrong.

Everything the tool reads is typed by a person: column headers, zone colours,
device types, tab names, chat commands. `SRC_PROT`, `gren`, `swich`, `Devicez`
should all be understood. But a zone is a hard boundary and a device type
decides where equipment may live, so a correction that is merely plausible is
worse than an honest refusal.

THE RULE
--------
A correction is applied only when it is both close AND unambiguous:

  * close       — within a budget of single-character edits that scales with
                  word length. "gren" is one edit from "green" and accepted;
                  "grey" is two and refused, however similar it looks. An edit
                  budget is far more predictable here than a similarity ratio,
                  which scores short words harshly and would reject "typ" for
                  "type" while accepting worse matches on long ones.
  * unambiguous — no rival answer is equally close. "brn" would sit one edit
                  from more than one colour; guessing there would put
                  equipment in the wrong zone, so it is refused with the near
                  misses named instead.

And a correction is never silent: callers get the original back so they can
say "read 'gren' as green" rather than pretending the sheet said green.

Built on difflib from the standard library — no new dependency, same as the
rest of this tool.
"""

import difflib
import functools
import re

# Hebrew final forms are the same letter; a word typed with the wrong one is a
# typo of spacing, not of meaning, so they are folded before comparing.
HEBREW_FINALS = str.maketrans("םןךףץ", "מנכפצ")

# How many single-character slips to forgive, by the length of the longer
# word. A budget of edits is far more predictable than a similarity ratio:
# "typ" -> "type" is one missing letter and obviously intended, but a ratio
# scores it 0.86 and a short-word floor rejects it, while "grey" -> "green"
# is two edits and must NOT be accepted however similar it looks.
def budget(n, lenient=False):
    """`lenient` forgives one slip even in a very short word.

    Off by default: at three characters a single edit can turn one real word
    into another, and for a zone or a device type that means silently putting
    equipment somewhere it does not belong. It is worth switching on where a
    wrong guess costs nothing — a misread chat command just gets asked again.
    """
    if n <= 3:
        return 1 if lenient else 0
    if n <= 5:
        return 1
    if n <= 9:
        return 2
    return 3


@functools.lru_cache(maxsize=8192)
def _normalised(text):
    t = text.strip().lower().translate(HEBREW_FINALS)
    return re.sub(r"[\s_\-./]+", "", t)


def normalise(text):
    """Fold away everything that is never meaningful: case, spacing,
    separators, and Hebrew final forms.

    Cached: the same few dozen column headers and alias spellings are folded
    over and over — reading one sheet did it a million times, and the regex
    dominated the cost of reading a file.
    """
    return _normalised(str(text or ""))


def distance(a, b):
    """Damerau-Levenshtein: insertions, deletions, substitutions AND
    transpositions. Transpositions matter — 'zoen' for 'zone' and 'bleu' for
    'blue' are single slips of the fingers, and counting them as two edits
    would reject exactly the mistakes people make most.
    """
    a, b = normalise(a), normalise(b)
    if a == b:
        return 0
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1,               # delete
                         cur[j - 1] + 1,            # insert
                         prev[j - 1] + (ca != cb))  # substitute
            if (i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb):
                cur[j] = min(cur[j], prev2[j - 2] + 1)   # transpose
        prev2, prev = prev, cur
    return prev[len(b)]


def score(a, b):
    """Similarity, only for ordering suggestions in an error message."""
    return difflib.SequenceMatcher(None, normalise(a), normalise(b)).ratio()


def match(text, candidates, lenient=False):
    """Best candidate for `text`, or None.

    `candidates` may be a sequence, or a mapping of accepted spelling -> value;
    with a mapping the VALUE is returned.

    Returns (value, exact) where `exact` says whether it matched outright.
    Returns None when nothing is close enough, or when two candidates are too
    close to each other to choose between.
    """
    if text is None or normalise(text) == "":
        return None
    mapping = candidates if isinstance(candidates, dict) else {c: c for c in candidates}

    want = normalise(text)
    for spelling, value in mapping.items():
        if normalise(spelling) == want:
            return (value, True)

    scored = []
    for spelling, value in mapping.items():
        other = normalise(spelling)
        allowed = budget(max(len(want), len(other)), lenient)
        # Every edit changes the length by at most one, so two strings whose
        # lengths differ by more than the budget cannot possibly be within it.
        # Checking that first skips the quadratic distance matrix for the great
        # majority of pairs, which is where reading a sheet spent its time.
        if abs(len(want) - len(other)) > allowed:
            continue
        d = distance(want, other)
        if d <= allowed:
            scored.append((d, spelling, value))
    if not scored:
        return None

    scored.sort(key=lambda t: (t[0], t[1]))
    best_d, _, best_value = scored[0]

    # A rival answer that is just as close means there is no safe choice —
    # correcting on a coin toss is worse than asking.
    for other_d, _, other_value in scored[1:]:
        if other_value == best_value:
            continue
        if other_d == best_d:
            return None
        break
    return (best_value, False)


def suggestions(text, candidates, limit=3):
    """The closest spellings, for an error message. Deliberately looser than
    match(): here we are offering options to a person, not deciding."""
    mapping = candidates if isinstance(candidates, dict) else {c: c for c in candidates}
    ranked = sorted(((score(text, s), s) for s in mapping), reverse=True)
    return [s for sc, s in ranked[:limit] if sc >= 0.45]
