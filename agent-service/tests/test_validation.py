"""Post-deploy validation: resolve only when the live check passes; otherwise follow-up fix PRs or hand-off."""
import pytest

from app import orchestrator, store
from app.agents import validate as validate_mod
from app.agents.diagnose import ProposedEdit
from app.agents.implement import ChangeProposal
from app.config import get_settings
from app.github_ops import PullRequestInfo, RunInfo
from app.playbooks import app_change
from tests.conftest import FakeGH, FakeSN, validation
from tests.test_orchestrator import FILES, ROW, triage_result


def use_validation(monkeypatch, *results_in_order):
    seq = list(results_in_order)

    async def fake(*args, **kwargs):
        return seq.pop(0)
    monkeypatch.setattr(orchestrator, "validate", fake)


def proposal_seq(monkeypatch, *css_values):
    """Each implementation call appends a different CSS line, so each PR has a real diff."""
    calls = list(css_values)

    async def fake(gh, ticket, app):
        v = calls.pop(0)
        return ChangeProposal(summary=f"set {v}", plan="- css", test_note="n/a", blocked_reason="",
                              edits=[ProposedEdit(path="apps/inventory/static/style.css", old_str="", new_str=v + "\n")])
    monkeypatch.setattr(app_change, "implement_app_change", fake)


async def deploy(sn, gh, pr_number, sha):
    """Human merges PR #pr_number, its deploy succeeds, then the watcher runs validation."""
    gh.pr_state[pr_number] = PullRequestInfo(pr_number, f"u{pr_number}", "closed", True, sha)
    await orchestrator.watch(sn, gh)  # -> deploying
    gh.runs[("app-deploy.yml", sha)] = RunInfo("completed", "success", f"run-{sha}")
    await orchestrator.watch(sn, gh)  # -> validating -> validated


async def start(monkeypatch, sn, gh):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_change", app="inventory"))
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "pr_open"


async def test_resolves_only_after_validation_passes(monkeypatch):
    proposal_seq(monkeypatch, "a{}")
    use_validation(monkeypatch, validation("pass", "unverifiable"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await start(monkeypatch, sn, gh)
    await deploy(sn, gh, 1, "sha1")
    assert store.get_ticket("abc").status == "resolved"
    assert sn.names()[-1] == "resolve"
    assert "validate" in [e.stage for e in store.events_for("abc")]


async def test_failed_validation_opens_follow_up_fix_then_resolves(monkeypatch):
    proposal_seq(monkeypatch, "a{}", "b{}")
    use_validation(monkeypatch, validation("pass", "fail", hint="add the column"), validation("pass", "pass"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await start(monkeypatch, sn, gh)
    await deploy(sn, gh, 1, "sha1")
    t = store.get_ticket("abc")
    assert (t.status, t.attempts, t.pr_number) == ("pr_open", 1, 2)
    assert gh.prs[1]["branch"] == "incidentflow/inc0010001-fix1" and "follow-up fix 1" in gh.prs[1]["title"]
    assert "resolve" not in sn.names()
    await deploy(sn, gh, 2, "sha2")
    assert store.get_ticket("abc").status == "resolved"


async def test_follow_up_prompt_carries_the_validation_report(monkeypatch):
    seen = []

    async def fake(gh, ticket, app):
        seen.append(ticket["description"])
        return ChangeProposal(summary="s", plan="p", test_note="n", blocked_reason="",
                              edits=[ProposedEdit(path="apps/inventory/static/style.css", old_str="",
                                                  new_str=f"x{len(seen)}{{}}\n")])
    monkeypatch.setattr(app_change, "implement_app_change", fake)
    use_validation(monkeypatch, validation("fail", hint="add the column"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await start(monkeypatch, sn, gh)
    await deploy(sn, gh, 1, "sha1")
    assert "FOLLOW-UP FIX 1" in seen[1] and "add the column" in seen[1] and "[FAIL]" in seen[1]


async def test_gives_up_after_max_attempts(monkeypatch):
    proposal_seq(monkeypatch, "a{}", "b{}", "c{}")
    use_validation(monkeypatch, *[validation("fail", hint="still wrong") for _ in range(3)])
    sn, gh = FakeSN(), FakeGH(FILES)
    await start(monkeypatch, sn, gh)
    for n in range(1, get_settings().max_fix_attempts + 2):
        await deploy(sn, gh, n, f"sha{n}")
    t = store.get_ticket("abc")
    assert t.status == "handed_off" and t.attempts == get_settings().max_fix_attempts
    assert len(gh.prs) == get_settings().max_fix_attempts + 1


async def test_not_fixable_in_code_hands_off(monkeypatch):
    proposal_seq(monkeypatch, "a{}")
    use_validation(monkeypatch, validation("fail", fixable=False, hint="tag the OS disk manually"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await start(monkeypatch, sn, gh)
    await deploy(sn, gh, 1, "sha1")
    assert store.get_ticket("abc").status == "handed_off" and len(gh.prs) == 1
    assert "tag the OS disk manually" in sn.calls[-1][-1]


def test_passed_is_computed_from_checks_not_claimed():
    assert validation("pass", "unverifiable").passed
    assert not validation("pass", "fail").passed
    assert not validation().passed  # no checks at all is not a pass


@pytest.mark.parametrize("url,ok", [
    ("http://app.example.com/api/stock", True),
    ("http://app.example.com:8081/api/stock", True),
    ("http://app.example.com:8082/", True),
    ("http://app.example.com:22/", False),
    ("http://evil.example.com:8081/", False),
    ("https://app.example.com/", False),
    ("http://user:pw@app.example.com/", False),
    ("http://169.254.169.254/metadata", False),
])
def test_http_get_only_reaches_pilot_apps(monkeypatch, url, ok):
    monkeypatch.setattr(get_settings(), "app_url", "http://app.example.com")
    assert validate_mod.allowed_url(url) is ok
