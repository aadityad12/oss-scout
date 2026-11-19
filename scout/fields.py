"""Which field ("specialization") a contribution counts toward, for the owner's career.

One fixed list, so the email, the dashboard and the routine all use the same tags.
Each field has a short label (the tag) and a one-sentence plain explanation of what
work in that field is. The routine writes the final choice per pick in the briefing
(`field: {"id", "also"?, "why"}`); before that, the scanner guesses a hint from the
tier in targets.toml, a per-repo override, or the repo's GitHub topics. No model.
"""

from __future__ import annotations

import re

# id -> (tag label, what the field is, in one plain sentence)
FIELDS: dict[str, tuple[str, str]] = {
    "embedded": ("Embedded & firmware",
                 "Software that runs directly on small chips inside devices like sensors, wearables and routers, "
                 "usually in C with very little memory."),
    "systems": ("Systems & OS",
                "The low-level software other programs stand on: operating systems, compilers, runtimes, "
                "memory and threads."),
    "databases": ("Databases & data",
                  "Software that stores, searches and moves large amounts of data quickly and safely."),
    "ai-ml": ("AI / ML engineering",
              "Tools that train, run or serve machine-learning models in real products, often tuned for speed."),
    "ml-research": ("AI / ML research",
                    "Code researchers use to try new model ideas, run experiments and reproduce papers."),
    "cloud": ("Cloud & infrastructure",
              "Software that runs, connects and watches services spread across many servers."),
    "security": ("Security & crypto",
                 "Code that keeps data and systems safe: encryption, certificates, sign-in and safe handling "
                 "of untrusted input."),
    "networking": ("Networking & protocols",
                   "Code that lets machines talk to each other: network stacks, wireless and messaging protocols."),
    "devtools": ("Developer tools",
                 "Tools programmers use to write, build, test and debug their own code."),
    "data-eng": ("Data engineering",
                 "Pipelines and tools that collect, clean and schedule data jobs for companies."),
    "mobile": ("Mobile",
               "Apps and libraries for phones and tablets."),
    "web": ("Web & backend",
            "Websites, web servers and the services behind them."),
    "graphics": ("Graphics & robotics",
                 "Code that draws 3D scenes, processes images or moves robots."),
}

# GitHub topic / description words -> field, checked in order (first match wins)
_HINTS: list[tuple[str, str]] = [
    ("embedded", r"\b(rtos|embedded|firmware|microcontrollers?|mcu|zephyr|esp32|arduino|iot|bare.?metal|nuttx)\b"),
    ("security", r"\b(security|crypto(graphy)?|tls|ssl|x509|fuzz(ing)?|authentication|vulnerabilit\w*)\b"),
    ("ml-research", r"\b(research|papers?|reproduc\w+|experiments?)\b"),
    ("ai-ml", r"\b(machine.?learning|deep.?learning|llms?|ai|ml|neural|inference|pytorch|transformers?|gpu|cuda)\b"),
    ("databases", r"\b(databases?|sql|olap|oltp|storage.?engine|query|columnar|key.?value)\b"),
    ("data-eng", r"\b(etl|elt|data.?pipelines?|orchestration|workflows?|data.?engineering|airflow)\b"),
    ("cloud", r"\b(cloud|kubernetes|k8s|proxy|service.?mesh|observability|telemetry|distributed|serverless|grpc)\b"),
    ("networking", r"\b(network(ing)?|tcp|udp|http2|quic|bluetooth|thread|matter|wifi|protocols?)\b"),
    ("systems", r"\b(kernel|operating.?system|compilers?|runtime|llvm|linux|allocator|virtual.?machine)\b"),
    ("mobile", r"\b(android|ios|kotlin.?multiplatform|mobile)\b"),
    ("graphics", r"\b(graphics|rendering|opengl|vulkan|3d|robotics?|ros2?|computer.?vision)\b"),
    ("devtools", r"\b(developer.?tools?|linter|formatter|ide|testing|debugger|build.?system|cli)\b"),
    ("web", r"\b(web|frontend|backend|framework|http|api)\b"),
]


def label(field_id: str | None) -> str:
    return FIELDS.get(field_id or "", ("", ""))[0]


def describe(field_id: str | None) -> str:
    return FIELDS.get(field_id or "", ("", ""))[1]


def valid(field_id) -> bool:
    return isinstance(field_id, str) and field_id in FIELDS


def guess(topics: list[str] | None, description: str | None = None) -> str | None:
    """A field from a repo's GitHub topics and description, or None when nothing matches."""
    text = " ".join([*(t.replace("-", " ") for t in (topics or [])), description or ""]).lower()
    for fid, pat in _HINTS:
        if re.search(pat, text):
            return fid
    return None


def hint(repo: str, tier_field: str | None, repo_fields: dict[str, str] | None,
         topics: list[str] | None = None, description: str | None = None) -> dict:
    """The scanner's best guess for a repo: {id, label, source}; id is None when unknown.

    Order: a per-repo override in targets.toml, then the tier's field, then topics/description.
    """
    over = (repo_fields or {}).get(repo)
    if valid(over):
        return {"id": over, "label": label(over), "source": "repo"}
    if valid(tier_field):
        return {"id": tier_field, "label": label(tier_field), "source": "tier"}
    g = guess(topics, description)
    return {"id": g, "label": label(g), "source": "topics" if g else "none"}


def resolve(briefing_field, fallback: dict | None = None) -> dict | None:
    """What every surface shows: {id, label, also, also_label, why}, or None when there is nothing.

    Uses the briefing's own `field` when it names a known id; else the scanner's hint, with
    the field's generic description as the "why".
    """
    if isinstance(briefing_field, dict) and valid(briefing_field.get("id")):
        fid = briefing_field["id"]
        also = briefing_field.get("also") if valid(briefing_field.get("also")) and briefing_field.get("also") != fid else None
        why = briefing_field.get("why")
        return {"id": fid, "label": label(fid), "also": also, "also_label": label(also) if also else None,
                "why": why.strip() if isinstance(why, str) and why.strip() else describe(fid)}
    if isinstance(fallback, dict) and valid(fallback.get("id")):
        fid = fallback["id"]
        return {"id": fid, "label": label(fid), "also": None, "also_label": None, "why": describe(fid)}
    return None
