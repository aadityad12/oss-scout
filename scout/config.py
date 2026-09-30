from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Tier:
    name: str
    weight: float
    repos: list[str]


@dataclass
class Config:
    login: str
    strong_languages: list[str]
    familiar_languages: list[str]
    settings: dict
    discovery: dict
    label_groups: dict[str, list[re.Pattern]]
    tiers: list[Tier]
    submit: dict = field(default_factory=dict)
    name: str = ""
    repo_labels: dict[str, list[str]] = field(default_factory=dict)

    def tier_of(self, repo: str) -> Tier | None:
        low = repo.lower()
        for t in self.tiers:
            if low in (r.lower() for r in t.repos):
                return t
        return None

    def wide_phase(self, today: date | None = None) -> bool:
        until = self.settings.get("wide_phase_until")
        return bool(until) and (today or date.today()) < date.fromisoformat(until)


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "targets.toml"
    raw = tomllib.loads(path.read_text())
    return Config(
        login=raw["user"]["login"],
        strong_languages=raw["user"].get("strong_languages", []),
        familiar_languages=raw["user"].get("familiar_languages", []),
        settings=raw.get("settings", {}),
        discovery=raw.get("discovery", {"enabled": False}),
        label_groups={k: [re.compile(p, re.I) for p in v]
                      for k, v in raw.get("labels", {}).items()},
        tiers=[Tier(t["name"], float(t["weight"]), t["repos"]) for t in raw.get("tier", [])],
        submit=raw.get("submit", {}),
        name=raw["user"].get("name", ""),
        repo_labels=raw.get("repo_labels", {}),
    )


def data_dir() -> Path:
    """Where state, candidates and briefings live.

    Locally this defaults to ./data (gitignored). In the cloud routine it points
    at the clone of the private oss-scout-data repo.
    """
    d = Path(os.environ.get("SCOUT_DATA_DIR", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d
