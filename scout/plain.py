"""Three plain lines for every item, in every surface (email, dashboard card, digest).

  problem    what's broken, in one sentence a non-expert understands
  sending    what you'd send: the PR with its size, a comment, or nothing yet
  your_part  what you do and how long it takes

`problem` is written by the routine (`plain_problem`); older briefings fall back to
the first sentence of `summary` with the code words taken out. The other two are
computed here from the diff, the kind, the mode and the failure record. No model.
"""

from __future__ import annotations

import re
from datetime import date

from . import briefings
from .failures import changed_lines, patch_files

READ_LINES_PER_MIN = 50   # how fast a diff gets read
MIN_READ_MINUTES = 2
SMALL_FIX_LINES = 30      # a change to existing files at or under this is "a small fix"

# a sentence ends at . ! ? followed by space or the end (so "tools/a.py" keeps its dot)
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
CODE_SPAN = re.compile(r"`[^`\n]*`")
PATH_WORD = re.compile(r"\S*/\S*|\S+\.(?:py|c|cc|cpp|h|hpp|js|ts|rs|go|java|kt|yml|yaml|toml|json|md|txt)\b")
DURATION = re.compile(r"(\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*(min(?:ute)?s?|h(?:ours?|rs?)?)\b", re.I)


def day(iso: str | None) -> str:
    """'2026-10-12' as 'Oct 12' ('' when it isn't a date)."""
    try:
        d = date.fromisoformat(str(iso))
    except ValueError:
        return ""
    return f"{d:%b} {d.day}"


# -- what's broken ------------------------------------------------------------------

def first_sentence(text: str) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    for m in SENTENCE_END.finditer(t):
        if not re.search(r"\b(?:e\.g|i\.e|vs)\.$", t[:m.end()], re.I):
            return t[:m.end()]
    return t


def strip_code(text: str) -> str:
    """Take backticked words and file paths out of a sentence, and tidy what is left."""
    t = PATH_WORD.sub("", CODE_SPAN.sub("", str(text or "")))
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"^\([^()]*\)\s*", "", t.strip())  # a sentence that opens with a bracketed aside
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;")
    t = t[:1].upper() + t[1:]
    return t + "." if t and t[-1] not in ".!?" else t


def problem(b: dict) -> str:
    """What's broken: the routine's `plain_problem`, else the cleaned first sentence of `summary`."""
    own = b.get("plain_problem")
    if isinstance(own, str) and own.strip():
        return own.strip()
    s = strip_code(first_sentence(b.get("summary", "")))
    return s if len(s) >= 20 else str(b.get("title") or s)


# -- what you'd send ----------------------------------------------------------------

def diff_stats(patch: str) -> dict:
    """Files and added+removed lines of a diff, split into new files and changes to existing ones."""
    new, new_tests, mod_lines = 0, 0, 0
    for part in re.split(r"(?m)^(?=diff --git )", patch):
        files = patch_files(part)
        if not files:
            continue
        if re.search(r"(?m)^new file mode", part):
            new += 1
            new_tests += bool(re.search(r"test", files[0], re.I))
        else:
            mod_lines += changed_lines(part)
    return {"files": len(patch_files(patch)), "new": new, "new_tests": new_tests,
            "changed": len(patch_files(patch)) - new, "mod_lines": mod_lines, "lines": changed_lines(patch)}


def approx(n: int) -> str:
    return str(n) if n < 20 else f"~{round(n, -1)}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def prepared(b: dict, patch_text: str) -> bool:
    """True when there is a finished draft to send: the briefing is `ready`, with a patch for a PR."""
    if briefings.mode_of(b) != "draft" or b.get("ready") is not True:
        return False
    return bool(patch_text.strip()) if b.get("kind", "pr") == "pr" else True


def sending(b: dict, patch_text: str = "") -> str:
    """What you'd send, from the diff (files, lines, new file or change) or the kind and mode."""
    kind = b.get("kind", "pr")
    if not prepared(b, patch_text):
        if briefings.mode_of(b) == "own":
            return "Nothing prepared yet: a laptop session where you write the fix and we check it"
        return "Nothing prepared yet: a laptop session where we write it together"
    if kind != "pr":
        pull = "/pull/" in str(b.get("post_target", ""))
        return {"repro": "A comment on the issue: the steps that reproduce it",
                "triage": "A comment on the issue: what we found about the cause",
                "review": "A review comment on someone else's pull request"}.get(kind) or f"A comment on the {'pull request' if pull else 'issue'}"
    st = diff_stats(patch_text)
    if not st["files"]:
        return "A pull request"
    parts = []
    if st["new"]:
        tests = " test" if st["new_tests"] == st["new"] else ""
        parts.append(f"{st['new']} new{tests} file{'s' if st['new'] > 1 else ''}")
    if st["changed"]:
        parts.append("a small fix" if st["mod_lines"] <= SMALL_FIX_LINES else "a fix")
    size = "1 line" if st["lines"] == 1 else f"{approx(st['lines'])} lines"
    return f"A pull request: {' and '.join(parts)} ({plural(st['files'], 'file')}, {size})"


# -- what you do, and how long ------------------------------------------------------

def minutes_of(text: str) -> int | None:
    """Total minutes in a time estimate like '20 min first build, then 60-120 min': the upper ends, added."""
    total, found = 0.0, False
    for lo, hi, unit in DURATION.findall(str(text or "")):
        n = float(hi or lo)
        total += n * 60 if unit.lower().startswith("h") else n
        found = True
    return round(total) if found else None


def spoken(minutes: int) -> str:
    if minutes < 60:
        return f"{max(5, round(minutes / 5) * 5)} minutes"
    hours = round(minutes / 30) / 2
    return "1 hour" if hours == 1 else f"{hours:g} hours"


def laptop_time(b: dict) -> str:
    text = str(b.get("time_estimate", ""))
    m = minutes_of(text)
    if m is None:
        return ""
    return f", about {spoken(m)}{' including the build' if 'build' in text.lower() else ''}"


def read_minutes(b: dict) -> int:
    """About a minute per 50 changed lines (a comment counts its words as lines of 12), at least 2."""
    lines = changed_lines(b.get("_patch", "")) or len(str(b.get("_post", "")).split()) // 12
    return max(MIN_READ_MINUTES, round(lines / READ_LINES_PER_MIN))


def failed_part(f: dict) -> str:
    by = day(f.get("refresh_by"))
    return {
        "refresh": f"Tap Refresh & send{f' by {by}' if by else ' soon'}",
        "token": "Replace your GitHub key, then tap Submit PR again",
        "edit": "Fix the text on the card, then tap Submit PR again",
        "skip": "Tap Skip: someone else already sent a fix",
    }.get(f.get("fix_action"), "Nothing to do right now: it will try again")


def your_part(item: dict, b: dict) -> str:
    """What you do and how long, for a suggestion `item` (from state) and its briefing `b`."""
    f = item.get("failure")
    if isinstance(f, dict) and f.get("plain"):
        return "Nothing to do: it is being refreshed now" if item.get("refresh_pending") else failed_part(f)
    if item.get("status") in ("ready", "approved") and prepared(b, b.get("_patch", "")):
        button = "Submit PR" if b.get("kind", "pr") == "pr" else "Post comment"
        return f"Read it and tap {button}, about {read_minutes(b)} minutes"
    return f"Laptop: run /contribute{laptop_time(b)}"


def lines(item: dict, b: dict) -> dict:
    """The three lines for one suggestion; `b` is a briefing from `briefings.load_all`."""
    return {"problem": problem(b) if b else strip_code(item.get("title", "")),
            "sending": sending(b, b.get("_patch", "")) if b else "Nothing prepared yet: a laptop session where we write it together",
            "your_part": your_part(item, b or {})}


def reply_lines(pr_title: str, followup_kind: str | None) -> dict:
    """The three lines for a PR a maintainer has replied on."""
    what = f"A maintainer replied on your pull request: {pr_title}" if pr_title else "A maintainer replied on your pull request"
    if followup_kind == "small":
        return {"problem": what, "sending": "A reply to the maintainer, plus a small fix if one is drafted",
                "your_part": "Read the draft and tap Push fix & reply, about 2 minutes"}
    if followup_kind == "discuss":
        return {"problem": what, "sending": "Nothing prepared yet: they ask why, so it needs your own words",
                "your_part": "Laptop: run /contribute with the pull request link to talk it through"}
    return {"problem": what, "sending": "A reply you write yourself (a draft is on its way, usually within the hour)",
            "your_part": "Open the pull request and read what they asked"}
