"""Preview the readable emails and dashboard card on real data, next to the old email.

  python3 scripts/before_after.py --data <copy of the data branch> --out <folder>

Writes email_old.html, email_new.html, email_stuck_new.html, weekly_new.html and
dashboard_new.html into --out. Nothing in --data is touched: the script works on a
temporary copy (without .git), so the preview can pretend the fusioncore item is ready
or stuck without saving that anywhere. Pass --pull to `git pull --ff-only` the data
first (read-only; this script never pushes).

The old email comes from the Worker as it was before the readable rewrite
(`git show 997bb76:worker/src/index.js`). The older briefings have no `plain_problem`
or `files_explained` yet, so scripts/before_after_overrides.json supplies them for the
two items the preview shows; nothing else reads that file.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scout import briefings, config, digest, render, state as statemod  # noqa: E402

OLD_WORKER_COMMIT = "997bb76"
READY_KEY = "manankharwar/fusioncore#161"
WEEKLY_KEY = "duckdb/duckdb#26144"
ENV = {"DASHBOARD_URL": "https://me.aadityad.dev", "OWNER_EMAIL": "owner@example.com"}

NODE = """
import { readFileSync } from "node:fs";
const [oldPath, newPath] = process.argv.slice(2);
const input = JSON.parse(readFileSync(0, "utf8"));
const oldMod = await import(oldPath), newMod = await import(newPath);
console.log(JSON.stringify({
  old: oldMod.buildEmail(input.old, input.env),
  ready: newMod.buildEmail(input.ready, input.env),
  stuck: newMod.buildEmail(input.stuck, input.env),
  weekly: newMod.buildWeeklyEmail(input.weekly, input.env),
}));
"""

PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<body style="margin:0;padding:16px;background:#f3f4f8;font:14px -apple-system,Segoe UI,sans-serif;color:#10121a">
<div style="max-width:600px;margin:0 auto">
<p style="margin:0 0 4px;color:#545869">{label}</p>
<p style="margin:0 0 16px"><b>Subject:</b> {subject}</p>
<div style="background:#ffffff;border-radius:12px;padding:16px">{body}</div>
<details style="margin-top:16px"><summary>Plain-text version</summary><pre style="white-space:pre-wrap">{text}</pre></details>
</div>
"""


def sh(*args, cwd=None, stdin=None) -> str:
    return subprocess.run(args, cwd=cwd, input=stdin, text=True, check=True, capture_output=True).stdout


def copy_data(src: Path, dest: Path, overrides: dict) -> None:
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    for key, extra in overrides.items():
        f = dest / "briefings" / briefings.slug(key) / "briefing.json"
        f.write_text(json.dumps({**json.loads(f.read_text()), **extra}, indent=2))


def old_digest(st: dict, data: Path, today: str) -> dict:
    """What the old Worker was given: the ready item, tonight's new briefings and the pairing queue."""
    sugg = st["suggestions"]
    picks = sorted((data / "picks").glob("*.json"))
    picked = json.loads(picks[-1].read_text()).get("picked", []) if picks else []
    fresh = [k for k in picked if sugg.get(k, {}).get("status") == "suggested"][:3]
    item = lambda k: {"key": k, "title": sugg[k]["title"], "kind": sugg[k].get("kind", "pr"), "slug": briefings.slug(k)}  # noqa: E731
    return {"date": today, "ready": [item(READY_KEY)], "waiting_on_you": [], "new_briefings": [item(k) for k in fresh],
            "pairing": [item(k) for k in sugg if sugg[k].get("pairing")], "token_age_days": 7, "token_warning": False, "send": True}


def page(title: str, label: str, mail: dict) -> str:
    return PAGE.format(title=html.escape(title), label=html.escape(label), subject=html.escape(mail["subject"]),
                       body=mail["html"], text=html.escape(mail["text"]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pull", action="store_true", help="git pull --ff-only the data first (read-only)")
    args = ap.parse_args()
    if args.pull:
        sh("git", "pull", "--ff-only", cwd=args.data)
    overrides = json.loads((ROOT / "scripts" / "before_after_overrides.json").read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cfg = config.load()

    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "data"
        copy_data(args.data, data, overrides)
        st = statemod.load(data)
        s = st["suggestions"][READY_KEY]

        # 1. the fusioncore item as ready to send
        for k in ("failure", "last_error"):
            s.pop(k, None)
        s["status"] = "ready"
        ready = digest.build(cfg, data, st, now)
        out_html = render.render(cfg, data, st)
        (args.out / "dashboard_new.html").write_text(out_html.read_text())
        old = old_digest(st, data, ready["date"])

        # 2. the same item after a failed send: the project changed the file the patch touches
        refresh_by = (now + timedelta(days=5)).date().isoformat()
        s.update(status="approved", failure={
            "code": "upstream_moved", "fix_action": "refresh", "refresh_by": refresh_by, "at": now.isoformat(timespec="seconds"),
            "plain": "Couldn't send: fusioncore changed ci.yml on Oct 5, after this draft was written.",
            "why": "The project edited the same workflow file this change touches, so the saved change no longer fits."})
        stuck = digest.build(cfg, data, st, now)

        # 3. the Saturday email: pretend the pair item was suggested a few days ago, show just that one
        st["suggestions"][WEEKLY_KEY]["suggested_at"] = (now - timedelta(days=3)).isoformat(timespec="seconds")
        weekly = digest.build(cfg, data, st, now)
        weekly["weekly"] = [i for i in weekly["weekly"] if i["key"] == WEEKLY_KEY]

    old_js = Path(tmp) / "old_index.mjs"
    with tempfile.TemporaryDirectory() as tmp2:
        old_js = Path(tmp2) / "old_index.mjs"
        old_js.write_text(sh("git", "show", f"{OLD_WORKER_COMMIT}:worker/src/index.js", cwd=ROOT))
        node = Path(tmp2) / "run.mjs"
        node.write_text(NODE)
        mails = json.loads(sh("node", str(node), str(old_js), str(ROOT / "worker" / "src" / "index.js"), cwd=ROOT,
                              stdin=json.dumps({"old": old, "ready": ready, "stuck": stuck, "weekly": weekly, "env": ENV})))

    (args.out / "email_old.html").write_text(page("Daily email, before", "Before: the 8am email as it was", mails["old"]))
    (args.out / "email_new.html").write_text(page("Daily email, after", "After: the 8am email, fusioncore#161 ready", mails["ready"]))
    (args.out / "email_stuck_new.html").write_text(page("Daily email, a failed send", "After: the 8am email, fusioncore#161 couldn't send", mails["stuck"]))
    (args.out / "weekly_new.html").write_text(page("Saturday email", "After: the Saturday email", mails["weekly"]))
    for name in ("old", "ready", "stuck", "weekly"):
        print(f"{name:7} {mails[name]['subject']}")
    print(f"wrote 5 files to {args.out}")


if __name__ == "__main__":
    main()
