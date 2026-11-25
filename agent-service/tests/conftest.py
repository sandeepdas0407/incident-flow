import pytest

from app import store


class FakeGH:
    """In-memory stand-in for GitHubOps."""

    def __init__(self, files: dict[str, str] | None = None):
        self.files = dict(files or {})
        self.prs: list[dict] = []
        self.pr_state: dict[int, object] = {}
        self.runs: dict[tuple[str, str], object] = {}

    def read_file(self, path, ref=None):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_files(self, prefix=""):
        return [p for p in self.files if p.startswith(prefix)]

    def open_pr(self, branch, title, body, edits, commit_msg):
        from app.github_ops import PullRequestInfo
        n = len(self.prs) + 1
        self.prs.append({"branch": branch, "title": title, "body": body, "edits": edits})
        return PullRequestInfo(n, f"https://github.com/o/r/pull/{n}", "open", False, None)

    def pr_info(self, number):
        return self.pr_state[number]

    def workflow_run(self, workflow, sha):
        return self.runs.get((workflow, sha))

    def superseding_run(self, workflow, sha):
        return self.runs.get((workflow, "superseded-by", sha))


class FakeSN:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        async def rec(*args, **kwargs):
            self.calls.append((name, *args))
        return rec

    def names(self):
        return [c[0] for c in self.calls]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    store.reset_engine(f"sqlite:///{tmp_path / 'test.db'}")
    yield


def validation(*results, fixable=True, hint=""):
    from app.agents.validate import Check, Validation
    return Validation(summary="checked", fixable_in_code=fixable, fix_hint=hint,
                      checks=[Check(criterion=f"c{i}", result=r, evidence="seen") for i, r in enumerate(results)])


@pytest.fixture(autouse=True)
def passing_validation(monkeypatch):
    """Post-deploy validation passes unless a test says otherwise (no live calls in tests)."""
    from app import orchestrator

    async def ok(*args, **kwargs):
        return validation("pass")
    monkeypatch.setattr(orchestrator, "validate", ok)
