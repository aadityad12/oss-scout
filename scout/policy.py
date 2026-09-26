"""Read a repo's contributing docs for AI-contribution rules and CLA requirements.

This is a heuristic first pass. The Claude step re-reads the actual policy for
anything it picks, so a false "restrictive" here costs ranking, not correctness.
"""

from __future__ import annotations

import re

from .github import GitHub

POLICY_FILES = [
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "AI_POLICY.md",
    ".github/AI_POLICY.md",
    "docs/AI_POLICY.md",
    "AGENTS.md",
]

AI = r"(?:\bAI\b|\bLLMs?\b|generative|machine.generated|copilot|chatgpt|claude code|agentic|coding agents?)"

RESTRICTIVE = [
    re.compile(rf"(?:do not|don't|must not|may not|will not|won't|not)\s+(?:\w+\s+){{0,4}}"
               rf"(?:accept|allow|permit|merge|submit|open|send|contribute)\w*[^.\n]{{0,80}}{AI}", re.I),
    re.compile(rf"(?:pull requests?|PRs?|contributions?|patches|code)\s+(?:\w+\s+){{0,2}}"
               rf"(?:generated|written|created|authored)\s+(?:\w+\s+){{0,2}}(?:by|with|using)\s+{AI}"
               rf"[^.\n]{{0,60}}(?:not|never)\b", re.I),
    re.compile(rf"{AI}[^.\n]{{0,60}}(?:is|are)\s+(?:not (?:accepted|allowed|permitted|welcome)"
               rf"|prohibited|forbidden|banned)", re.I),
    re.compile(rf"(?:ban|prohibit|forbid)\w*[^.\n]{{0,40}}{AI}", re.I),
    re.compile(rf"{AI}[^.\n]{{0,60}}(?:will be|are|get)\s+(?:closed|rejected)", re.I),
]

DISCLOSURE = [
    re.compile(rf"(?:disclose|disclosure|declare|indicate|mention|state|note)\w*[^.\n]{{0,80}}{AI}", re.I),
    re.compile(rf"{AI}[^.\n]{{0,80}}(?:must|should)\s+be\s+(?:disclosed|declared|noted|mentioned)", re.I),
    re.compile(rf"{AI}[^.\n]{{0,80}}(?:disclose|declare|flag)\w*", re.I),
]

NEGATED = re.compile(r"(?:don't|do not|doesn't|does not|no need to|not required to|needn't)\s+(?:\w+\s+){0,2}"
                     r"(?:disclose|declare|mention|state|indicate|note)", re.I)
AI_MENTION = re.compile(AI, re.I)
CLA = re.compile(r"\bCLA\b|contributor license agreement|\bDCO\b|signed-off-by", re.I)


def _snippet(text: str, m: re.Match) -> str:
    start = max(0, m.start() - 60)
    return " ".join(text[start:m.end() + 60].split())


def classify(text: str) -> dict:
    """Return {'ai': restrictive|disclosure|mentioned|none, 'evidence': str, 'cla': bool}."""
    out = {"ai": "none", "evidence": "", "cla": bool(CLA.search(text))}
    for pat in RESTRICTIVE:
        if m := pat.search(text):
            return {**out, "ai": "restrictive", "evidence": _snippet(text, m)}
    for pat in DISCLOSURE:
        for m in pat.finditer(text):
            before = text[max(0, m.start() - 40):m.end()]
            if NEGATED.search(before):
                continue  # "You don't have to disclose..."
            return {**out, "ai": "disclosure", "evidence": _snippet(text, m)}
    if m := AI_MENTION.search(text):
        return {**out, "ai": "mentioned", "evidence": _snippet(text, m)}
    return out


def fetch(gh: GitHub, repo: str) -> dict:
    texts, files = [], []
    for f in POLICY_FILES:
        t = gh.file_text(repo, f)
        if t:
            texts.append(t)
            files.append(f)
    result = classify("\n\n".join(texts))
    result["files"] = files
    return result
