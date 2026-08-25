#!/usr/bin/env python3
"""
assistant.py
============
The conversational layer — offline, deterministic, no model behind it.

Everything it says is read straight out of the data the planner already
produced: the ranked positions and their scores, the reason strings, the
review findings, the strand assignments. It never estimates and never
paraphrases a number it did not read, so an answer here is exactly as true as
the plan it describes.

That is the point. A language model could phrase these answers more fluently,
but it could also invent a rack that has no space. This layer is designed so
that when a model is eventually put in front of it, the model does the
WORDING and this code keeps doing the FACTS.

WHAT IT UNDERSTANDS
-------------------
  explain     why a device was placed where it was, why a route took the path
              it did, why a row failed
  locate      where a serial, port or device lives
  status      how full a rack or pod is, what is running out
  instruct    "not in D5", "prefer pod A3" — real constraints, fed back into
              the planner rather than just acknowledged

Hebrew and English are both accepted, because that is how the sheets and the
people around them are written.
"""

import re
from collections import Counter

import placement
import serials

RACK_RE = re.compile(r"\b([A-D])\s*-?\s*(\d)\s*-?\s*([SN])\s*(\d{1,2})\b", re.I)
POD_RE = re.compile(r"\b([A-D])\s*(\d)\b", re.I)
ROOM_RE = re.compile(r"\b(AB|CD)\b", re.I)
ROW_RE = re.compile(r"\b(?:row|שורה)\s*(\d+)\b", re.I)


class Ask:
    """One question, plus whatever the UI was showing when it was asked."""

    def __init__(self, text, plan=None, topology=None, constraints=None):
        self.raw = (text or "").strip()
        self.text = self.raw.lower()
        self.plan = plan or {}
        self.topology = topology or {}
        self.constraints = {**placement.EMPTY_CONSTRAINTS, **(constraints or {})}


# --------------------------------------------------------------------------
# reading the question
# --------------------------------------------------------------------------

def _norm_rack(m):
    return f"{m.group(1).upper()}{m.group(2)}-{m.group(3).upper()}{int(m.group(4)):02d}"


def find_racks(text):
    return [_norm_rack(m) for m in RACK_RE.finditer(text)]


def find_pods(text, exclude_racks=()):
    """Pod ids, minus anything that was really part of a rack id — otherwise
    'A1-S05' would also read as a mention of pod A1 and of pod 05."""
    stripped = RACK_RE.sub(" ", text)
    return [f"{m.group(1).upper()}{m.group(2)}" for m in POD_RE.finditer(stripped)]


def find_serials(text):
    out = []
    for word in re.split(r"[\s,;]+", text):
        if word.upper().startswith(serials.PREFIX):
            out.append(serials.normalise(word.strip(".,:;")))
    return out


WORDS = {
    "why": ("why", "למה", "מדוע", "explain", "תסביר", "הסבר"),
    "where": ("where", "איפה", "where is", "locate", "מיקום", "find", "חפש"),
    "full": ("full", "occupancy", "תפוסה", "מלא", "space", "מקום", "free", "פנוי"),
    "avoid": ("avoid", "don't", "dont", "not in", "אל תשתמש", "לא ב", "בלי",
              "תימנע", "אל תיגע", "exclude"),
    "prefer": ("prefer", "תעדיף", "עדיף", "prioritise", "prioritize", "רצוי"),
    "clear": ("clear", "reset", "נקה", "אפס", "בטל"),
    "list": ("list", "show", "רשימה", "הצג", "מה יש"),
    "failed": ("fail", "failed", "נכשל", "כשל", "error", "שגיאה", "בעיה"),
    "capacity": ("capacity", "running out", "נגמר", "יגמר", "עומס", "busiest"),
    "help": ("help", "עזרה", "what can", "מה אפשר", "?"),
}


def _has(text, key):
    return any(w in text for w in WORDS[key])


def classify(ask):
    """Which question is this? Ordered most-specific first, because an
    instruction like "don't use D5" also mentions a rack and would otherwise
    be read as "where is D5"."""
    t = ask.text
    if not t:
        return "help"
    if _has(t, "clear") and (ask.constraints_mentioned or _has(t, "avoid") or _has(t, "prefer")):
        return "clear_constraints"
    if _has(t, "avoid"):
        return "avoid"
    if _has(t, "prefer"):
        return "prefer"
    if _has(t, "list") and (_has(t, "avoid") or "דגש" in t or "constraint" in t):
        return "list_constraints"
    if _has(t, "why"):
        if ROW_RE.search(t) or _has(t, "failed"):
            return "why_failed"
        if find_serials(t) or "רכיב" in t or "device" in t or "מיקום" in t:
            return "why_placed"
        return "why_route"
    if _has(t, "capacity"):
        return "capacity"
    if _has(t, "full"):
        return "rack_status"
    if _has(t, "where") or find_serials(t):
        return "locate"
    if _has(t, "failed"):
        return "why_failed"
    if _has(t, "help"):
        return "help"
    return "unknown"


Ask.constraints_mentioned = property(
    lambda self: any(self.constraints[k] for k in self.constraints))


# --------------------------------------------------------------------------
# answering
# --------------------------------------------------------------------------

def _fmt_u(site):
    return (f"U{site['u_end']}" if site["u_start"] == site["u_end"]
            else f"U{site['u_end']}–U{site['u_start']}")


def answer_why_placed(ask):
    sites = (ask.plan.get("siting") or [])
    if not sites:
        return {"text": "אין תוכנית פתוחה עם רכיבים חדשים, אז אין מיקום להסביר. "
                        "העלה קובץ עם לשונית Devices וסמן 'הקובץ כולל רכיבים חדשים'."}

    wanted = find_serials(ask.raw)
    chosen = [s for s in sites if s["serial"] in wanted] or sites
    parts = []
    for s in chosen[:3]:
        if s["status"] != "ok":
            parts.append(f"**{s['serial']}** לא שובץ: {s.get('reason', '')}")
            continue
        opts = s.get("options") or []
        head = (f"**{s['serial']}** ({s['label']}, {s['u_size']}U) → "
                f"**{s['rack']}** {_fmt_u(s)}")
        why = f"הסיבה: {s['reason']}."
        if s.get("connects_to"):
            why += f" הוא מתחבר ל־{', '.join(s['connects_to'])}."
        runner = ""
        if len(opts) > 1:
            b, n = opts[0], opts[1]
            gap = round(n["score"] - b["score"], 1)
            runner = (f" המועמד הבא היה {n['rack']} עם ציון {n['score']} "
                      f"מול {b['score']} — פער של {gap} לטובת הנבחר, בעיקר בגלל "
                      f"{'הקרבה' if n['proximity'] > b['proximity'] else 'מקום פנוי במסד'}.")
        parts.append(head + "\n" + why + runner)
    return {"text": "\n\n".join(parts),
            "hint": "אפשר לומר לי 'אל תשתמש ב-<מסד>' או 'תעדיף פוד <X>' ואתכנן מחדש."}


def answer_why_route(ask):
    rows = [r for r in ask.plan.get("results", []) if r.get("status") in ("ok", "committed")]
    if not rows:
        return {"text": "אין מסלולים מתוכננים כרגע להסביר."}

    n = ROW_RE.search(ask.text)
    picked = [r for r in rows if str(r["row"]) == n.group(1)] if n else rows[:1]
    if not picked:
        return {"text": f"שורה {n.group(1)} לא שובצה בתוכנית הזו."}

    r = picked[0]
    if r.get("hops", 0) == 0:
        return {"text": f"שורה {r['row']}: שני הקצוות באותו ארון, אז זה **כבל גישור אחד** "
                        f"— אפס טראנקים, אפס גידים, אפס פורטי גישור. "
                        f"זה החיבור הזול ביותר שיש."}

    strands = ", ".join(f"{s['edge_id']}#{s['index']}" for s in r.get("strands", [])[:3])
    return {"text": (
        f"שורה {r['row']}: {r['hops']} קפיצות, ~{r['total_length_m']} מ', "
        f"דומיין {r['domain']}.\n"
        f"הבחירה היא לפי **מספר קפיצות לפני מטרים** — כל קפיצה עולה גישור, פורט "
        f"בפאנל ועבודת כפיים, בעוד שהמרחקים במפה הזו הם הנחות. "
        f"מסלול ארוך יותר במטרים אך עם פחות קפיצות ינצח.\n"
        f"גידים שנתפסו: {strands}{'…' if len(r.get('strands', [])) > 3 else ''}")}


def answer_why_failed(ask):
    review = ask.plan.get("review") or {}
    dev_review = ask.plan.get("device_review") or {}
    issues = list(review.get("issues", [])) + list(dev_review.get("issues", []))
    failed_rows = [r for r in ask.plan.get("results", []) if r.get("status") == "failed"]

    n = ROW_RE.search(ask.text)
    if n:
        want = int(n.group(1))
        hit = ([i for i in issues if i["row"] == want]
               + [r for r in failed_rows if r["row"] == want])
        if not hit:
            return {"text": f"שורה {want} לא נכשלה — היא עברה."}
        h = hit[0]
        return {"text": f"שורה {want} נכשלה: {h.get('message') or h.get('reason')}"}

    if not issues and not failed_rows:
        return {"text": "לא נכשלה אף שורה בהרצה הזו."}
    lines = [f"• שורה {i['row']}: {i.get('message') or i.get('reason')}"
             for i in (issues + failed_rows)[:8]]
    return {"text": f"{len(issues) + len(failed_rows)} שורות בעייתיות:\n" + "\n".join(lines)}


def answer_locate(ask):
    T = ask.topology
    found = []
    for s in find_serials(ask.raw):
        dev_id = serials.index(T).get(s)
        if not dev_id:
            found.append(f"סיריאל **{s}** לא קיים במפה.")
            continue
        d = T["devices"][dev_id]
        free = sum(1 for p in T["ports"].values()
                   if p["device"] == dev_id and p["status"] == "free")
        total = d["fiber_ports"] + d["copper_ports"]
        found.append(f"**{s}** — {d['label']} ({d['type']}), במסד **{d['rack']}** "
                     f"ב־U{d['u_start']}"
                     + (f"–U{d['u_start'] - d['u_size'] + 1}" if d["u_size"] > 1 else "")
                     + f". פורטים פנויים: {free} מתוך {total}.")
    for rack in find_racks(ask.raw):
        if rack in T["racks"]:
            found.append(_rack_summary(T, rack))
    if not found:
        return {"text": "לא זיהיתי סיריאל או מסד בשאלה. אפשר לכתוב למשל "
                        "`SN-P7T25SMTDS` או `A1-S05`."}
    return {"text": "\n\n".join(found)}


def _rack_summary(T, rack):
    used = placement.occupancy(T, rack)
    gaps = placement.free_gaps(T, rack)
    biggest = max((h for _, h in gaps), default=0)
    ports = [p for p in T["ports"].values() if p["rack"] == rack]
    free_ports = sum(1 for p in ports if p["status"] == "free")
    meta = T["racks"][rack]
    role = "EOR/hub — לא מרכיבים בו ציוד" if meta["is_eor"] else "מסד מחשוב"
    return (f"**{rack}** ({role}, פוד {meta['pod']}) — "
            f"{len(used)}/42U תפוסים ({round(len(used) / 42 * 100)}%). "
            f"החור הרציף הגדול ביותר: {biggest}U. "
            f"פורטים פנויים: {free_ports} מתוך {len(ports)}.")


def answer_rack_status(ask):
    T = ask.topology
    racks = find_racks(ask.raw)
    if racks:
        return {"text": "\n\n".join(_rack_summary(T, r) for r in racks if r in T["racks"])
                        or "לא מצאתי את המסד הזה."}
    pods = find_pods(ask.raw)
    if pods:
        out = []
        for pod in pods:
            members = [r for r, m in T["racks"].items() if m["pod"] == pod]
            if not members:
                continue
            pct = [len(placement.occupancy(T, r)) / 42 for r in members]
            emptiest = min(members, key=lambda r: len(placement.occupancy(T, r)))
            out.append(f"**פוד {pod}** — {len(members)} מסדים, תפוסה ממוצעת "
                       f"{round(sum(pct) / len(pct) * 100)}%. "
                       f"הכי פנוי: {emptiest} "
                       f"({round(len(placement.occupancy(T, emptiest)) / 42 * 100)}%).")
        if out:
            return {"text": "\n\n".join(out)}
    return {"text": "על איזה מסד או פוד? למשל `כמה מקום יש ב-A1-S05` או `תפוסה של פוד B3`."}


def answer_capacity(ask):
    T = ask.topology
    tight = []
    for e in T["edges"]:
        for ct, d in e["cable_types"].items():
            used = len(d.get("strands") or {})
            if d["capacity"]:
                tight.append((used / d["capacity"], e["id"], ct, used, d["capacity"]))
    tight.sort(reverse=True)
    lines = [f"• `{eid}` ({ct}) — {u}/{cap} ({round(pct * 100)}%)"
             for pct, eid, ct, u, cap in tight[:6]]
    busiest = ask.plan.get("summary", {}).get("busiest_trunks") or []
    extra = ""
    if busiest:
        extra = ("\n\nהתוכנית הנוכחית מוסיפה עומס בעיקר על: "
                 + ", ".join(f"`{t}` ×{n}" for t, n in busiest[:3]))
    return {"text": "הטראנקים הקרובים ביותר להתמלא:\n" + "\n".join(lines) + extra}


# --------------------------------------------------------------------------
# instructions that change the plan
# --------------------------------------------------------------------------

def _collect_targets(ask):
    racks = find_racks(ask.raw)
    pods = [p for p in find_pods(ask.raw) if p not in {r.split("-")[0] for r in racks}]
    rooms = [m.group(1).upper() for m in ROOM_RE.finditer(ask.raw)]
    return racks, pods, rooms


def answer_constraint(ask, kind):
    racks, pods, rooms = _collect_targets(ask)
    if not (racks or pods or rooms):
        what = "ממה להימנע" if kind == "avoid" else "מה להעדיף"
        return {"text": f"לא זיהיתי {what}. אפשר לכתוב מסד (`A1-S05`), "
                        f"פוד (`A1`) או חדר (`AB`)."}

    c = {k: set(v) for k, v in ask.constraints.items()}
    if kind == "avoid":
        c["avoid_racks"] |= set(racks)
        c["avoid_pods"] |= set(pods)
        c["avoid_rooms"] |= set(rooms)
        word = "לא אשתמש ב"
    else:
        if rooms:
            return {"text": "העדפה עובדת על מסד או פוד, לא על חדר שלם."}
        c["prefer_racks"] |= set(racks)
        c["prefer_pods"] |= set(pods)
        word = "אעדיף את"

    named = ", ".join(racks + pods + rooms)
    return {
        "text": f"{word}־**{named}**. מתכנן מחדש עם הדגש הזה.",
        "constraints": {k: sorted(v) for k, v in c.items()},
        "replan": True,
    }


def answer_list_constraints(ask):
    c = ask.constraints
    bits = []
    for key, label in (("avoid_racks", "להימנע ממסדים"), ("avoid_pods", "להימנע מפודים"),
                       ("avoid_rooms", "להימנע מחדרים"), ("prefer_racks", "להעדיף מסדים"),
                       ("prefer_pods", "להעדיף פודים")):
        if c.get(key):
            bits.append(f"• {label}: {', '.join(sorted(c[key]))}")
    if not bits:
        return {"text": "אין כרגע דגשים פעילים."}
    return {"text": "הדגשים הפעילים:\n" + "\n".join(bits)}


def answer_clear(ask):
    return {"text": "ניקיתי את כל הדגשים. מתכנן מחדש לפי הקריטריונים הרגילים בלבד.",
            "constraints": {k: [] for k in placement.EMPTY_CONSTRAINTS},
            "replan": True}


HELP = """אני עוזר מקומי — כל מה שאני אומר נקרא ישירות מהתוכנית, בלי ניחושים.

**להבין החלטות**
• `למה הרכיב הוצב שם?` — הציון, הנימוק, והמועמד שהיה שני
• `למה המסלול הזה?` / `למה שורה 4 ככה?`
• `למה שורה 7 נכשלה?`

**לברר מצב**
• `איפה SN-P7T25SMTDS` — מסד, U, פורטים פנויים
• `כמה מקום יש ב-A1-S05` / `תפוסה של פוד B3`
• `מה עומד להיגמר?` — הטראנקים הכי עמוסים

**לתת דגשים** (משנים את התכנון בפועל)
• `אל תשתמש ב-D5-N06` / `בלי פוד A1`
• `תעדיף פוד A3`
• `הצג דגשים` · `נקה דגשים`"""


HANDLERS = {
    "why_placed": answer_why_placed,
    "why_route": answer_why_route,
    "why_failed": answer_why_failed,
    "locate": answer_locate,
    "rack_status": answer_rack_status,
    "capacity": answer_capacity,
    "list_constraints": answer_list_constraints,
    "clear_constraints": answer_clear,
}


def respond(text, plan=None, topology=None, constraints=None):
    """One turn. Returns {text, constraints?, replan?, hint?}."""
    ask = Ask(text, plan, topology, constraints)
    intent = classify(ask)

    if intent in ("avoid", "prefer"):
        out = answer_constraint(ask, intent)
    elif intent == "help":
        out = {"text": HELP}
    elif intent == "unknown":
        out = {"text": "לא הבנתי את זה. אני יודע להסביר מיקומים ומסלולים, "
                       "לאתר סיריאלים ומסדים, לדווח על תפוסה, ולקבל דגשים.\n\n"
                       "כתוב `עזרה` לרשימה מלאה."}
    else:
        out = HANDLERS[intent](ask)

    out["intent"] = intent
    return out
