"""General change requests: app_change and infra_change playbooks, risk-flag hand-offs, the plan check."""
from app import orchestrator, store
from app.agents.diagnose import ProposedEdit
from app.agents.implement import ChangeProposal
from app.github_ops import PullRequestInfo, RunInfo
from app.playbooks import app_change, infra_change
from tests.conftest import FakeGH, FakeSN
from tests.test_orchestrator import FILES, ROW, triage_result

INFRA_MAIN = 'resource "azurerm_network_security_group" "nsg" {\n  name = "nsg"\n}\n'


def proposal(edits, blocked=""):
    async def fake(*args):
        return ChangeProposal(summary="Add a supplier column", plan="- template + test", edits=edits,
                              test_note="added test", blocked_reason=blocked)
    return fake


async def test_app_change_opens_pr_with_new_file(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_change", app="inventory"))
    monkeypatch.setattr(app_change, "implement_app_change", proposal([
        ProposedEdit(path="apps/inventory/static/style.css", old_str="#EAF4EE", new_str="#EAF4EE; --accent: #123456"),
        ProposedEdit(path="apps/inventory/tests/test_supplier.py", old_str="", new_str="def test_x():\n    pass\n"),
    ]))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    t = store.get_ticket("abc")
    assert t.status == "pr_open" and t.scenario == "app_change" and t.workflow == "app-deploy.yml"
    assert {e.path for e in gh.prs[0]["edits"]} == {"apps/inventory/static/style.css",
                                                    "apps/inventory/tests/test_supplier.py"}


async def test_app_change_cannot_add_dependencies(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_change", app="inventory"))
    monkeypatch.setattr(app_change, "implement_app_change", proposal([
        ProposedEdit(path="apps/inventory/requirements.txt", old_str="", new_str="requests\n")]))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_app_change_cannot_touch_another_app(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_change", app="inventory"))
    monkeypatch.setattr(app_change, "implement_app_change", proposal([
        ProposedEdit(path="apps/orders/static/style.css", old_str="#F4F1EA", new_str="#000000")]))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_implementation_agent_can_decline(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("app_change", app="inventory"))
    monkeypatch.setattr(app_change, "implement_app_change", proposal([], blocked="needs a login page"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs
    assert "needs a login page" in sn.calls[-1][-1]


async def test_risk_flags_hand_off_before_any_work(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage",
                        triage_result("app_change", app="orders", flags=["security_or_access", "too_vague"]))
    called = []
    monkeypatch.setattr(app_change, "implement_app_change", lambda *a: called.append(a))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not called and not gh.prs
    assert "security or access" in sn.calls[-1][-1] and "too vague" in sn.calls[-1][-1]


async def test_infra_change_opens_pr(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("infra_change", app="none"))
    monkeypatch.setattr(infra_change, "terraform_fmt", lambda path, content: content)
    monkeypatch.setattr(infra_change, "implement_infra_change", proposal([
        ProposedEdit(path="infra/main.tf", old_str='  name = "nsg"', new_str='  name = "nsg"\n  tags = { owner = "ops" }')]))
    sn, gh = FakeSN(), FakeGH({**FILES, "infra/main.tf": INFRA_MAIN})
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    t = store.get_ticket("abc")
    assert t.status == "pr_open" and t.workflow == "terraform-apply.yml"
    assert gh.prs[0]["title"].startswith("[INC0010001] Infra:")


async def test_infra_change_new_paid_resource_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("infra_change", app="none"))
    monkeypatch.setattr(infra_change, "terraform_fmt", lambda path, content: content)
    monkeypatch.setattr(infra_change, "implement_infra_change", proposal([
        ProposedEdit(path="infra/main.tf", old_str="", new_str='resource "azurerm_mssql_server" "db" {\n}\n')]))
    sn, gh = FakeSN(), FakeGH({**FILES, "infra/main.tf": INFRA_MAIN})
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_infra_change_cannot_touch_backend(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("infra_change", app="none"))
    monkeypatch.setattr(infra_change, "implement_infra_change", proposal([
        ProposedEdit(path="infra/backend.tf", old_str="", new_str="# x\n")]))
    sn, gh = FakeSN(), FakeGH({**FILES, "infra/main.tf": INFRA_MAIN})
    await orchestrator.process_new(sn, gh, "change_request", ROW)
    assert store.get_ticket("abc").status == "handed_off" and not gh.prs


async def test_failed_plan_check_hands_off(monkeypatch):
    monkeypatch.setattr(orchestrator, "triage", triage_result("vm_resize", size="Standard_B2s"))
    sn, gh = FakeSN(), FakeGH(FILES)
    await orchestrator.process_new(sn, gh, "incident", ROW)
    gh.pr_state[1] = PullRequestInfo(1, "u", "open", False, None, "headsha")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "pr_open"  # plan check not finished yet
    gh.runs[("terraform-plan.yml", "headsha")] = RunInfo("completed", "failure", "plan-run")
    await orchestrator.watch(sn, gh)
    assert store.get_ticket("abc").status == "handed_off"


def test_terraform_fmt_formats_or_passes_through():
    from app.playbooks.common import terraform_fmt
    out = terraform_fmt("infra/x.tf", 'variable "a" {\ndefault="x"\n}\n')
    assert "default" in out  # formatted by the CLI when installed, unchanged otherwise
    assert terraform_fmt("apps/x.py", "x=1") == "x=1"
