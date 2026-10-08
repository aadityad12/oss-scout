"""Read a repo's contributing docs for AI-contribution rules and CLA requirements.

The result decides how a suggestion is worked (its *mode*):

  draft  AI may write code and posts. The nightly routine may prepare a one-tap item.
  pair   AI-assisted coding is fine, but posts, replies and the PR body must be human-written
         and/or autonomous agents are banned. Done on the laptop with /contribute, never one-tap.
  own    AI-written code is banned outright. The owner writes it; Claude only explains and reviews.

This is a heuristic first pass. The Claude step re-reads the actual policy for anything it picks,
so a wrong guess here costs ranking, not correctness.
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

# Wording that bans AI work. Applied one sentence at a time (see classify), after sentences about
# posts, agents and missing disclosure have been set aside.
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

# -- rules for the mode ----------------------------------------------------
# Sentence by sentence, with wrapped lines joined, bullets standing alone and code fences skipped.
# Sentences under a heading like "Prohibited ..." count as restricted even without a negation.

# something is forbidden, discouraged or punished
RESTRICT = re.compile(r"\bnot\b|\bnever\b|\bno\b|n't|prohibit|forbid|\bban|unacceptable|disallow"
                      r"|\bhidden\b|\bclosed\b|\bdeleted\b|\brejected\b", re.I)
HEADING_RESTRICT = re.compile(r"prohibit|forbidden|not allowed|banned|unacceptable|restricted|do not|don't", re.I)

# prose that goes to people (not code)
POST_NOUN = (r"(?:\bposts?\b|(?<!code )\bcomments?\b|\bissues?\b|bug reports?|feature requests?|"
             r"(?:pull request|PR) (?:descriptions?|bod(?:y|ies)|text)|\bdescriptions?\b|commit messages?|"
             r"\breplies\b|\breplying\b|\bresponses?\b|\bdiscussions?\b|communicat\w+|\bchat\b|\bmessages?\b|\btext\b)")
WRITE_VERB = r"(?:generat\w+|writ\w+|written|draft\w*|compos\w+|authored|copy\w*|past\w+|produc\w+|creat\w+)"

# Posts rule (a): AI + a write verb + a post noun + a restriction in one sentence, e.g.
#   "AI should not be used to generate comments", "strictly prohibited to use AI to write your posts".
# Posts rule (b): the prose must be the human's: "written by humans", "in your own words".
HUMAN_PROSE = re.compile(
    r"\b(?:comments?|posts?|descriptions?|bod(?:y|ies)|replies|responses?|issues?|text)\b[^.]{0,60}"
    r"\b(?:written|authored|composed)\s+by\s+(?:a\s+)?(?:humans?|people|person|you|the contributor)\b"
    r"|\bhuman[- ]written\b|\b(?:in )?your own (?:words|voice)\b|\b(?:think|talk|speak|write)\s+for\s+you\b", re.I)

# Agents rule: autonomous or automated contributing, or vibe coding with nobody reading it.
AGENTS = re.compile(
    r"\b(?:autonomous|agentic|fully[- ]automated|automated)\s+(?:\w+\s+){0,2}"
    r"(?:agents?|AI|tools?|bots?|commits?|PRs?|pull requests?|submissions?|contributions?)\b"
    r"|\bvibe[- ]?cod\w+|\bagents?\b[^.]{0,60}(?:not allowed|prohibited|forbidden|banned|not permitted)", re.I)

# Allowance rule: AI-assisted code is welcome. Ignored when a negation sits in the matched span.
ALLOW = [
    # "Using AI (i.e., LLMs) as tools for coding is welcome"
    re.compile(rf"{AI}[^.]{{0,60}}\b(?:tools?|assistants?|assistance|assisted|coding)\b[^.]{{0,40}}"
               r"\b(?:welcome|allowed|permitted|acceptable|fine|ok|okay|encouraged)\b", re.I),
    # "AI-generated code is allowed", "AI-assisted contributions are welcome"
    re.compile(rf"{AI}[- ](?:generated|assisted|written|powered)\s+(?:\w+\s+){{0,2}}(?:is|are)\s+"
               r"(?:also\s+|generally\s+|always\s+)?(?:allowed|welcome|acceptable|permitted|ok|okay|fine)", re.I),
    # "you may use AI", "contributors can use LLMs"
    re.compile(rf"\b(?:you|contributors?|developers?|users?|people)\s+(?:may|can|are free to|are welcome to|are allowed to)\s+"
               rf"(?:\w+\s+){{0,3}}use\s+(?:\w+\s+){{0,3}}{AI}", re.I),
]
# A disclosure rule also counts as an allowance: asking you to disclose AI use presupposes it is allowed.

# "do not submit pull requests generated by AI": next to an allowance this means "the human must be
# the author" (pair); on its own it is a plain ban (own).
PR_GENERATED = re.compile(
    rf"(?:pull requests?|PRs?)\s+(?:\w+\s+){{0,2}}(?:generated|written|created|authored)\s+(?:\w+\s+){{0,2}}"
    rf"(?:by|with|using)\s+{AI}", re.I)

# not a ban on code: it targets work with no human in the loop, or a missing disclosure
NOT_A_CODE_BAN = re.compile(r"undisclosed|without (?:any )?(?:disclos|understanding|oversight)|not disclos"
                            r"|unreviewed|vibe|autonomous|agentic", re.I)
# a disclosure about something other than the contribution itself
DISCLOSURE_ASIDE = re.compile(r"quote|quoting|citing|translat|research", re.I)

_ABBREV = re.compile(r"\b(i\.e\.|e\.g\.)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def _clean(s: str) -> str:
    s = _ABBREV.sub(lambda m: m.group(1).replace(".", ""), s)
    return " ".join(re.sub(r"[*_`>]+", " ", s).split())


def _sentences(text: str) -> list[tuple[str, bool]]:
    """(sentence, under_a_restricted_heading) for the whole text.

    An "I use AI to ..." example is joined to the verdict that follows it, so f3d's
    "I use AI to generate issues. This is unacceptable." reads as one sentence.
    """
    units: list[tuple[str, bool]] = []
    para: list[str] = []
    restricted = False
    fenced = False

    def flush() -> None:
        if para:
            raw = _clean(" ".join(para))
            units.extend((x, restricted) for x in re.split(r"(?<=[.!?:])\s+", raw) if x)
            para.clear()

    for line in text.splitlines():
        if line.strip().startswith("```"):
            flush()
            fenced = not fenced
        elif fenced:
            continue
        elif _HEADING.match(line):
            flush()
            restricted = bool(HEADING_RESTRICT.search(line))
        elif not line.strip():
            flush()
        elif _BULLET.match(line):
            flush()
            para.append(line)
        else:
            para.append(line)
    flush()
    merged: list[tuple[str, bool]] = []
    i = 0
    while i < len(units):
        s, r = units[i]
        if re.match(r"I use\b", s) and i + 1 < len(units):
            s = f"{s} {units[i + 1][0]}"
            i += 1
        merged.append((s, r))
        i += 1
    return merged


def _forbids_posts(s: str, restricted: bool) -> bool:
    if HUMAN_PROSE.search(s):
        return True
    return bool(AI_MENTION.search(s) and re.search(POST_NOUN, s, re.I) and re.search(WRITE_VERB, s, re.I)
                and (restricted or RESTRICT.search(s)))


def classify(text: str) -> dict:
    """Read a project's AI rules.

    Returns:
      mode                 draft | pair | own
      ai_posts_forbidden   posts, comments, replies or the PR body must be human-written
      disclosure_required  the project asks for AI use to be disclosed
      ai                   label used for ranking: restrictive (own) | disclosure | mentioned | none
      evidence             the sentence that decided it
      cla                  a CLA or DCO is needed

    Rules, in order:
      1. Outright ban on AI-written code (and no allowance next to a PR-only ban) -> own.
      2. An allowance (AI tools welcome, AI code allowed, you may use AI, disclose your AI use)
         together with a restriction on posts, on autonomous agents, or on PRs "generated by AI" -> pair.
      3. A restriction on posts or agents with nothing said about code -> pair.
      4. Anything else (or nothing at all) -> draft. A disclosure rule alone stays draft; the
         one-sentence disclosure goes into the PR as before.
    """
    out = {"mode": "draft", "ai_posts_forbidden": False, "disclosure_required": False,
           "ai": "none", "evidence": "", "cla": bool(CLA.search(text))}
    if not AI_MENTION.search(text):
        return out

    allowance = disclosure = posts = agents = hard_ban = soft_ban = mention = ""
    for s, restricted in _sentences(text):
        about_ai = AI_MENTION.search(s) or HUMAN_PROSE.search(s) or AGENTS.search(s)
        if not about_ai:
            continue
        mention = mention or s
        is_posts = _forbids_posts(s, restricted)
        is_agents = bool(AGENTS.search(s) and (restricted or RESTRICT.search(s)))
        if is_posts:
            posts = posts or s
        if is_agents:
            agents = agents or s
        for pat in ALLOW:
            if (m := pat.search(s)) and not re.search(r"\bnot\b|\bnever\b|n't", m.group(0), re.I):
                allowance = allowance or s
        if not DISCLOSURE_ASIDE.search(s):
            for pat in DISCLOSURE:
                if pat.search(s):
                    allowance = allowance or s
                    if not NEGATED.search(s):
                        disclosure = disclosure or s
                    break
        if is_posts or is_agents or NOT_A_CODE_BAN.search(s):
            continue
        if any(pat.search(s) for pat in RESTRICTIVE):
            if PR_GENERATED.search(s):
                soft_ban = soft_ban or s
            else:
                hard_ban = hard_ban or s

    label = "disclosure" if disclosure else "mentioned"
    out["disclosure_required"] = bool(disclosure)
    if hard_ban or (soft_ban and not allowance):
        out.update(mode="own", ai="restrictive", ai_posts_forbidden=True, evidence=hard_ban or soft_ban)
    elif posts or agents or (allowance and soft_ban):
        out.update(mode="pair", ai=label, ai_posts_forbidden=bool(posts), evidence=posts or agents or soft_ban)
    else:
        out.update(ai=label, evidence=disclosure or mention)
    out["evidence"] = out["evidence"][:240]
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
