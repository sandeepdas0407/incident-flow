from app import orchestrator, store
from app.agents.diagnose import Diagnosis, ProposedEdit
from app.agents.llm import LLMRefusal
from app.agents.triage import Triage
from app.github_ops import PullRequestInfo, RunInfo
from app.playbooks import app_bugfix
from app.playbooks.guardrails import GuardrailViolation
from app import pilot_apps
from tests.conftest import FakeGH, FakeSN

ORDERS = pilot_apps.get("orders")

ROW = {"sys_id": "abc", "number": "INC0010001", "short_description": "High CPU", "description": "cpu 99%"}
FILES = {
    "infra/terraform.tfvars": 'vm_size = "Standard_B1s"\n',
    "apps/orders/static/style.css": ":root { --app-background: #F4F1EA; }\n",
    "apps/orders/app.py": "def f(xs):\n    return sum(xs) / len(xs)\n",
    "apps/inventory/static/style.css": ":root { --app-background: #EAF4EE; }\n",
}


def triage_result(scenario, conf=0.95, size="", color="", app="orders", flags=()):
    async def fake(table, row):
        return Triage(scenario=scenario, confidence=conf, summary="s", reasoning="r",
                      target_vm_size=size, target_color=color, app=app, risk_flags=list(flags))
    return fake


async def test_vm_resize_happy_path(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("vm_resize", size="Standard_B2s"))
    sn, gh = FakeSN(), FakeGH(FILES)

    await orchestrator.process_new(sn, gh, "incident", ROW)

    t = store.get_ticket("abc")
    assert t.status == "pr_open" and t.pr_number == 1 and t.workflow == "terraform-apply.yml"
    assert gh.prs[0]["edits"][0].new_content == 'vm_size = "Standard_B2s"\n'
    assert sn.names() == ["claim", "work_note", "work_note", "awaiting_change"]

    # human merges -> deploying
    gh.pr_state[1] = PullRequestInfo(1, "u", "closed", True, "deadbeefcafe")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "deploying"
    assert sn.names()[-1] == "implementing"

    # workflow running -> still deploying
    gh.runs[("terraform-apply.yml", "deadbeefcafe")] = RunInfo("in_progress", None, "run-url")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "deploying"

    # workflow succeeds -> resolved
    gh.runs[("terraform-apply.yml", "deadbeefcafe")] = RunInfo("completed", "success", "run-url")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "resolved"
    assert sn.names()[-1] == "resolve"
    stages = [e.stage for e in store.events_for("abc")]
    assert stages == ["intake", "triage", "diagnosis", "pr", "merge", "deploy", "deploy", "validate", "resolved"]


async def test_ticket_processed_once(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("vm_resize", size="Standard_B2s"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert len(gh.prs) == 1


async def test_low_confidence_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("vm_resize", conf=0.4, size="Standard_B2s"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert store.get_ticket("abc").status == "handed_off"
    assert sn.names()[-1] == "hand_off" and not gh.prs


async def test_handoff_scenario(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("handoff"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert store.get_ticket("abc").status == "handed_off"


async def test_guardrail_violation_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="not-a-colour"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_refusal_hands_off(monkeypatch):
    async def refuse(table, row):
        raise LLMRefusal("declined")
    monkeypatch.setattr(orchestrator, "triage", refuse)
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert store.get_ticket("abc").status == "handed_off"


async def test_pr_closed_without_merge_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    gh.pr_state[1] = PullRequestInfo(1, "u", "closed", False, None)
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "handed_off"


async def test_failed_deploy_marks_failed(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    gh.pr_state[1] = PullRequestInfo(1, "u", "closed", True, "sha1")
    await orchestrator.watch(sn, gh)
    gh.runs[("app-deploy.yml", "sha1")] = RunInfo("completed", "failure", "run")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "failed"


async def test_superseded_deploy_follows_newer_run(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    gh.pr_state[1] = PullRequestInfo(1, "u", "closed", True, "sha1")
    await orchestrator.watch(sn, gh)
    gh.runs[("app-deploy.yml", "sha1")] = RunInfo("completed", "cancelled", "old-run")
    gh.runs[("app-deploy.yml", "superseded-by", "sha1")] = RunInfo("in_progress", None, "new-run")
    await orchestrator.watch(sn, gh)
    t = store.get_ticket("abc")
    assert (t.status, t.run_url) == ("deploying", "new-run")
    gh.runs[("app-deploy.yml", "superseded-by", "sha1")] = RunInfo("completed", "success", "new-run")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "resolved"


async def test_cancelled_without_newer_run_marks_failed(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    gh.pr_state[1] = PullRequestInfo(1, "u", "closed", True, "sha1")
    await orchestrator.watch(sn, gh)
    gh.runs[("app-deploy.yml", "sha1")] = RunInfo("completed", "cancelled", "old-run")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "failed"


async def test_bugfix_edits_validated(monkeypatch):
    gh = FakeGH(FILES)
    good = Diagnosis(root_cause="div by zero", fix_summary="guard empty list", test_note="added test",
                     edits=[ProposedEdit(path="apps/orders/app.py", old_str="sum(xs) / len(xs)",
                                         new_str="sum(xs) / len(xs) if xs else 0"),
                            ProposedEdit(path="apps/orders/tests/test_new.py", old_str="", new_str="def test_x():\n    pass\n")])
    _, edits = await app_bugfix.build_edits(gh, good, ORDERS)
    assert {e.path for e in edits} == {"apps/orders/app.py", "apps/orders/tests/test_new.py"}

    bad = Diagnosis(root_cause="x", fix_summary="x", test_note="x",
                    edits=[ProposedEdit(path="infra/main.tf", old_str="", new_str="oops")])
    try:
        await app_bugfix.build_edits(gh, bad, ORDERS)
        raise AssertionError("expected guardrail violation")
    except GuardrailViolation:
        pass


async def test_bugfix_cannot_touch_another_app():
    gh = FakeGH(FILES)
    d = Diagnosis(root_cause="x", fix_summary="x", test_note="x",
                  edits=[ProposedEdit(path="apps/inventory/static/style.css", old_str="", new_str="oops")])
    try:
        await app_bugfix.build_edits(gh, d, ORDERS)
        raise AssertionError("expected guardrail violation")
    except GuardrailViolation:
        pass


async def test_ui_change_edits_only_that_apps_stylesheet(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF", app="inventory"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    t = store.get_ticket("abc")
    assert t.status == "pr_open" and t.app == "inventory"
    assert [e.path for e in gh.prs[0]["edits"]] == ["apps/inventory/static/style.css"]
    assert "Contoso Inventory" in gh.prs[0]["title"]


async def test_ci_outside_pilot_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("ui_change", color="#0B5FFF", app="orders"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", {**ROW, "cmdb_ci.name": "Payroll"})
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_app_scoped_playbook_without_app_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_bugfix", app="none"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_vm_resize_needs_no_app(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("vm_resize", size="Standard_B2s", app="none"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    assert store.get_ticket("abc").status == "pr_open"
