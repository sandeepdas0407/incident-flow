"""Ticket lifecycle:
intake -> triage -> plan -> PR -> (human merge) -> deploy -> VALIDATE on the live system ->
    pass: resolve / close
    fail: follow-up fix PR (up to max_fix_attempts) -> merge -> deploy -> validate again ... or hand off.
"""
from __future__ import annotations

import asyncio
import logging

from app import events, pilot_apps, store
from app.agents.llm import LLMError, LLMRefusal
from app.agents.triage import RISK_FLAGS, Triage, triage
from app.agents.validate import validate
from app.config import get_settings
from app.github_ops import GitHubOps
from app.playbooks import app_bugfix, app_change, infra_change, ui_change, vm_resize
from app.playbooks.guardrails import ChangePlan, GuardrailViolation, NeedsHuman
from app.servicenow import ServiceNow

log = logging.getLogger("incident_flow.orchestrator")

PLAYBOOKS = {"vm_resize": vm_resize.plan, "app_bugfix": app_bugfix.plan, "ui_change": ui_change.plan,
             "app_change": app_change.plan, "infra_change": infra_change.plan}
APP_SCOPED = {"app_bugfix", "ui_change", "app_change"}  # change one app's code: need a pilot app
# Which playbook makes a follow-up fix after a failed validation (the deterministic ones can't "try again").
REMEDIATE = {"app_bugfix": "app_bugfix", "app_change": "app_change", "ui_change": "app_change",
             "infra_change": "infra_change", "vm_resize": "infra_change"}
PLAN_CHECK = "terraform-plan.yml"  # PR check for infra changes; fails on any destroy/replace
TAG = "[AI Ops Agent]"


def resolve_app(row: dict, tri) -> tuple[pilot_apps.PilotApp | None, str]:
    """The ticket's configuration item is authoritative; otherwise use what triage read from the text."""
    ci = row.get("cmdb_ci.name")
    if ci:
        app = pilot_apps.by_name(ci)
        return (app, "") if app else (None, f"configuration item '{ci}' is not part of the pilot environment")
    app = pilot_apps.get(tri.app)
    return (app, "") if app else (None, "the ticket does not identify one of the pilot applications")


def _set(t: store.Ticket, **fields) -> store.Ticket:
    for k, v in fields.items():
        setattr(t, k, v)
    t = store.save_ticket(t)
    events.ticket_changed(t)
    return t


async def hand_off(sn: ServiceNow, t: store.Ticket, reason: str, status: str = "handed_off") -> None:
    group = get_settings().sn_human_group
    await sn.hand_off(t.table, t.sys_id, f"{TAG} Handing off to {group}: {reason}")
    _set(t, status=status)
    events.emit(t, "handoff", f"Reassigned to {group}: {reason}")


async def _guarded(sn: ServiceNow, t: store.Ticket, work) -> None:
    """Run one step of agent work; any failure ends in a hand-off with a trail on the ticket."""
    try:
        await work()
    except NeedsHuman as e:
        await hand_off(sn, t, f"{e}.")
    except GuardrailViolation as e:
        await hand_off(sn, t, f"proposed change was blocked by a guardrail ({e}).")
    except LLMRefusal as e:
        await hand_off(sn, t, f"the AI model declined this request ({e}).")
    except LLMError as e:
        await hand_off(sn, t, f"AI analysis failed ({e}).", status="failed")
    except Exception as e:  # keep the loop alive; leave a trail on the ticket
        log.exception("processing %s failed", t.number)
        events.emit(t, "error", f"{type(e).__name__}: {e}")
        try:
            await hand_off(sn, t, f"automation error ({type(e).__name__}).", status="failed")
        except Exception:
            log.exception("hand-off for %s also failed", t.number)
            _set(t, status="failed")


async def _open_pr(sn: ServiceNow, gh: GitHubOps, t: store.Ticket, plan: ChangePlan, branch: str) -> store.Ticket:
    events.emit(t, "diagnosis", plan.note)
    await sn.work_note(t.table, t.sys_id, f"{TAG} {plan.note}")
    pr = await asyncio.to_thread(gh.open_pr, branch, plan.title, plan.body, plan.edits, plan.commit_msg)
    t = _set(t, status="pr_open", pr_number=pr.number, pr_url=pr.url, workflow=plan.workflow, merge_sha=None)
    events.emit(t, "pr", f"Opened PR #{pr.number}: {plan.title}. Awaiting human review.", pr.url)
    await sn.awaiting_change(t.table, t.sys_id,
                             f"{TAG} Change proposed in GitHub PR #{pr.number}: {pr.url}\n"
                             "Awaiting human review and merge. The ticket will update automatically after deployment "
                             "and a live validation check.")
    return t


# ---------------------------------------------------------------- intake
async def process_new(sn: ServiceNow, gh: GitHubOps, table: str, row: dict) -> None:
    if store.get_ticket(row["sys_id"]):
        return
    s = get_settings()
    t = _set(store.Ticket(sys_id=row["sys_id"], table=table, number=row["number"],
                          short_description=row.get("short_description", ""), status="triaging"))
    events.emit(t, "intake", f"Picked up {table} {t.number}: {t.short_description}")

    async def work():
        nonlocal t
        await sn.claim(table, t.sys_id, f"{TAG} Picked up for automated triage.")

        tri = await triage(table, row)
        app, why_not = resolve_app(row, tri)
        t = _set(t, scenario=tri.scenario, app=app.id if app else None)
        app_label = app.name if app else "no pilot app"
        events.emit(t, "triage", f"{tri.scenario} · {app_label} (confidence {tri.confidence:.2f}): {tri.summary}")
        await sn.work_note(table, t.sys_id,
                           f"{TAG} Triage: {tri.summary}\nApplication: {app_label}\n"
                           f"Playbook: {tri.scenario} (confidence {tri.confidence:.2f})\nReasoning: {tri.reasoning}")

        flags = [f for f in tri.risk_flags if f in RISK_FLAGS]
        if flags:
            await hand_off(sn, t, "this needs a person because " + "; ".join(RISK_FLAGS[f] for f in flags) + ".")
            return
        if tri.scenario not in PLAYBOOKS:
            await hand_off(sn, t, "no automated playbook applies.")
            return
        if tri.confidence < s.triage_min_confidence:
            await hand_off(sn, t, f"triage confidence {tri.confidence:.2f} is below {s.triage_min_confidence}.")
            return
        if tri.scenario in APP_SCOPED and app is None:
            await hand_off(sn, t, f"{why_not}; automated changes are limited to the pilot apps "
                                  f"({', '.join(a.name for a in pilot_apps.all_apps())}).")
            return

        t = _set(t, status="diagnosing")
        plan = await PLAYBOOKS[tri.scenario](gh, t.number, row, tri, app)
        t = await _open_pr(sn, gh, t, plan, f"incidentflow/{t.number.lower()}")

    await _guarded(sn, t, work)


# ---------------------------------------------------------------- validation + follow-up fixes
async def _validate(sn: ServiceNow, gh: GitHubOps, t: store.Ticket) -> None:
    s = get_settings()
    row = await sn.get(t.table, t.sys_id) or {}
    result = await validate(gh, {**row, "number": t.number}, t.table, t.scenario, t.app, t.pr_url)
    verdict = "PASSED" if result.passed else "FAILED"
    events.emit(t, "validate", f"Live validation {verdict}. {result.report()}")
    await sn.work_note(t.table, t.sys_id, f"{TAG} Post-deployment validation {verdict}:\n{result.report()}")

    if result.passed:
        notes = (f"Change from PR #{t.pr_number} ({t.pr_url}) was deployed by {t.workflow} ({t.run_url}) and "
                 f"validated on the live system: {result.summary}")
        await sn.resolve(t.table, t.sys_id, notes)
        _set(t, status="resolved")
        events.emit(t, "resolved", f"Validated and {'closed' if t.table == 'change_request' else 'resolved'}.", t.run_url)
        return

    failed = "; ".join(f"{c.criterion} ({c.evidence})" for c in result.failures)
    attempts = t.attempts or 0
    if not result.fixable_in_code:
        await hand_off(sn, t, f"post-deployment validation failed and code changes can't fix it: {failed}. "
                              f"What's needed: {result.fix_hint}")
        return
    if attempts >= s.max_fix_attempts:
        await hand_off(sn, t, f"post-deployment validation still fails after {attempts} follow-up fix(es): {failed}.")
        return
    await _remediate(sn, gh, t, row, result, attempts + 1)


async def _remediate(sn: ServiceNow, gh: GitHubOps, t: store.Ticket, row: dict, result, attempt: int) -> None:
    scenario = REMEDIATE.get(t.scenario or "", "")
    if scenario not in PLAYBOOKS:
        await hand_off(sn, t, f"validation failed and there is no automated way to fix a {t.scenario} change.")
        return
    t = _set(t, status="diagnosing", attempts=attempt)
    events.emit(t, "remediate", f"Validation failed: making follow-up fix {attempt} of "
                                f"{get_settings().max_fix_attempts}. Still needed: {result.fix_hint}")
    ticket = {**row, "number": t.number, "description": (
        f"{row.get('description') or ''}\n\n"
        f"--- FOLLOW-UP FIX {attempt} ---\nPR #{t.pr_number} ({t.pr_url}) was merged and deployed, but validating the "
        f"live system afterwards FAILED:\n{result.report()}\n\nWhat still needs to change: {result.fix_hint}\n"
        "The previous change is already on main: build on it, and change only what is needed to make the failed "
        "checks pass.")}
    stub = Triage(scenario=scenario, confidence=1.0, summary="follow-up fix", reasoning="validation failed",
                  target_vm_size="", target_color="", app=t.app or "none")
    plan = await PLAYBOOKS[scenario](gh, t.number, ticket, stub, pilot_apps.get(t.app))
    plan.title = f"{plan.title} (follow-up fix {attempt})"
    await _open_pr(sn, gh, t, plan, f"incidentflow/{t.number.lower()}-fix{attempt}")


# ---------------------------------------------------------------- watcher
async def watch(sn: ServiceNow, gh: GitHubOps) -> None:
    for t in store.tickets_in_status("pr_open"):
        try:
            pr = await asyncio.to_thread(gh.pr_info, t.pr_number)
            if pr.merged:
                t = _set(t, status="deploying", merge_sha=pr.merge_sha)
                events.emit(t, "merge", f"PR #{t.pr_number} approved and merged ({pr.merge_sha[:7]}). Waiting for {t.workflow}.", t.pr_url)
                await sn.implementing(t.table, t.sys_id,
                                      f"{TAG} PR #{t.pr_number} was approved and merged. Deploying via GitHub Actions ({t.workflow}).")
            elif pr.state == "closed":
                await hand_off(sn, t, f"PR #{t.pr_number} was closed without merging. A human should decide next steps.")
            elif t.workflow == "terraform-apply.yml" and pr.head_sha:
                # infra PRs: if the plan check fails (destroy/replace, or an invalid plan), a person takes over
                check = await asyncio.to_thread(gh.workflow_run, PLAN_CHECK, pr.head_sha)
                if check and check.status == "completed" and check.conclusion == "failure":
                    await hand_off(sn, t, f"the Terraform plan check on PR #{t.pr_number} failed (it would destroy or "
                                          f"replace resources, or the plan is invalid): {check.url}. "
                                          "The PR is left open for a human to review.")
        except Exception:
            log.exception("watching PR for %s failed", t.number)

    for t in store.tickets_in_status("deploying"):
        try:
            run = await asyncio.to_thread(gh.workflow_run, t.workflow, t.merge_sha)
            if run is None:
                continue
            if run.conclusion == "cancelled":
                # a newer push may have replaced our queued run; that run deploys our change too
                newer = await asyncio.to_thread(gh.superseding_run, t.workflow, t.merge_sha)
                if newer is not None:
                    if t.run_url != newer.url:
                        events.emit(t, "deploy", f"{t.workflow} run was superseded by a newer run that includes this change.", newer.url)
                    run = newer
            if t.run_url != run.url:
                t = _set(t, run_url=run.url)
                events.emit(t, "deploy", f"{t.workflow} started.", run.url)
            if run.status != "completed":
                continue
            if run.conclusion == "success":
                _set(t, status="validating")
                events.emit(t, "deploy", "Deployment succeeded. Validating the result on the live system.", run.url)
            else:
                await hand_off(sn, t, f"deployment workflow ended with '{run.conclusion}': {run.url}", status="failed")
        except Exception:
            log.exception("watching deployment for %s failed", t.number)

    for t in store.tickets_in_status("validating"):
        await _guarded(sn, t, lambda t=t: _validate(sn, gh, t))
