"""Reset the demo to its starting point so it can be run again.

- Closes open agent PRs and deletes their incidentflow/* branches
- Restores the seeded starting state on main (VM size B1s; each pilot app's original colour, code and tests) via one PR
  that you merge (branch protection requires review), or directly with --direct if allowed
- Clears the local SQLite timeline

    uv run python -m scripts.reset_demo [--direct]
"""
import os
import sys
from pathlib import Path

from app.config import get_settings
from app.github_ops import FileEdit, GitHubOps

SEED_DIR = Path(__file__).resolve().parent / "seed_state"  # original demo state, before any agent change
# every file in seed_state/ (all three pilot apps + infra/terraform.tfvars), as repo paths
SEED_FILES = sorted(p.relative_to(SEED_DIR).as_posix() for p in SEED_DIR.rglob("*") if p.is_file())


def main(direct: bool) -> None:
    s = get_settings()
    gh = GitHubOps(s)
    repo = gh.repo

    for pr in repo.get_pulls(state="open"):
        if pr.head.ref.startswith("incidentflow/"):
            pr.edit(state="closed")
            print(f"closed PR #{pr.number}")
    for ref in repo.get_git_refs():
        if ref.ref.startswith("refs/heads/incidentflow/"):
            ref.delete()
            print(f"deleted {ref.ref}")

    edits = []
    for path in SEED_FILES:
        seed = (SEED_DIR / path).read_text(encoding="utf-8")
        try:
            current = gh.read_file(path)
        except Exception:
            current = None
        if current != seed:
            edits.append(FileEdit(path, seed))
    if edits:
        if direct:
            for e in edits:
                f = repo.get_contents(e.path, ref=s.github_base_branch)
                repo.update_file(e.path, "demo reset", e.new_content, f.sha, branch=s.github_base_branch)
                print(f"reset {e.path} on {s.github_base_branch}")
        else:
            pr = gh.open_pr("demo-reset", "Demo reset: restore starting state",
                            "Restores the seeded demo state.", edits, "demo reset")
            print(f"opened reset PR #{pr.number}: {pr.url}  (merge it to finish the reset)")
    else:
        print("repo already in starting state")

    db = s.database_url.removeprefix("sqlite:///")
    if os.path.exists(db):
        os.remove(db)
        print(f"removed {db}")
    print("Resolve/close leftover demo tickets in ServiceNow if needed, then re-run scripts.seed_tickets.")


if __name__ == "__main__":
    main("--direct" in sys.argv)
