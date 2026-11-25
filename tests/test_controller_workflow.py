import json
import re
from pathlib import Path

import pytest

from logic_apps_expressions import Evaluator, ExpressionError, _Parser

WORKFLOW = Path(__file__).resolve().parents[1] / "infra" / "controller" / "workflow.json"
DEFINITION = json.loads(WORKFLOW.read_text(encoding="utf-8"))

NOW = "2026-11-25T10:05:00Z"
OFF = ("PowerState/deallocated", "Stopped")
ON = ("PowerState/running", "Running")


def base_state(**overrides):
    return {
        "month": "2026-11",
        "usedSeconds": 0,
        "lastObservedUtc": "2026-11-25T10:00:00Z",
        "lastActive": False,
        "uncertain": False,
        "phase": "idle",
        "startedUtc": "2026-11-25T00:00:00Z",
        "runnerStartedUtc": "2026-11-25T00:00:00Z",
    } | overrides


def job(status, runner=None, tags=("image-publisher",)):
    return {"status": status, "runner": {"id": runner} if runner else None, "tag_list": list(tags)}


def walk(actions):
    for name, action in actions.items():
        yield name, action
        for child in (action.get("actions"), action.get("else", {}).get("actions")):
            if child:
                yield from walk(child)
        for case in action.get("cases", {}).values():
            yield from walk(case["actions"])
        if "default" in action:
            yield from walk(action["default"]["actions"])


def executed(actions, ev):
    """Return names of leaf actions that the workflow would execute."""
    names = []
    for name, action in actions.items():
        if action["type"] == "If":
            branch = action["actions"] if ev.condition(action["expression"]) else action["else"]["actions"]
            names += executed(branch, ev)
        elif action["type"] == "Switch":
            selected = ev.evaluate(action["expression"])
            case = next((c for c in action["cases"].values() if c["case"] == selected), action["default"])
            names += executed(case["actions"], ev)
        else:
            names.append(name)
    return names


def run(*, state=None, now=NOW, power=OFF, provisioning="Succeeded", jobs=(), vm_ok=True, aks_ok=True,
        jobs_ok=True, admission=True, **parameters):
    state = state or base_state()
    vm_code, aks_code = power
    bodies = {
        "Get_State": state,
        "Get_VM": {"statuses": [{"code": "ProvisioningState/succeeded"}, {"code": vm_code}]} if vm_ok else None,
        "Get_AKS": {"properties": {"provisioningState": provisioning, "powerState": {"code": aks_code}}} if aks_ok else None,
        "Get_Token": {"value": "token"},
        "Get_Jobs": list(jobs) if jobs_ok else None,
    }
    actions = {
        "Get_VM": {"status": "Succeeded" if vm_ok else "Failed"},
        "Get_AKS": {"status": "Succeeded" if aks_ok else "Failed"},
        "Get_Jobs": {"status": "Succeeded" if jobs_ok else "Failed"},
    }
    outputs = {"Get_State": {"headers": {"ETag": '"1"'}, "body": state}}
    params = {name: spec["defaultValue"] for name, spec in DEFINITION["parameters"].items()} | {
        "runnerId": 7,
        "publisherTag": "image-publisher",
        "admissionEnabled": admission,
    } | parameters
    ev = Evaluator(outputs, bodies, actions, params)
    ev.clock = now
    for name, action in DEFINITION["actions"].items():
        if action["type"] == "Compose":
            outputs[name] = ev.evaluate(action["inputs"])
        elif action["type"] == "Query":
            source = ev.evaluate(action["inputs"]["from"])
            if not isinstance(source, list):
                actions[name], bodies[name] = {"status": "Failed"}, None
                continue
            kept = []
            for ev.item in source:
                if ev.evaluate(action["inputs"]["where"]):
                    kept.append(ev.item)
            ev.item = None
            actions[name], bodies[name] = {"status": "Succeeded"}, kept
        elif action["type"] == "If":
            break
    decision_applied = ev.condition(DEFINITION["actions"]["Apply_Decision"]["expression"])
    steps = executed(DEFINITION["actions"]["Apply_Decision"]["actions"], ev) if decision_applied else []
    signal = ev.condition(DEFINITION["actions"]["Signal_attention"]["expression"])
    return {
        "decision": outputs["Decision"],
        "state": outputs["NewState"],
        "steps": steps,
        "failed": signal,
        "outputs": outputs,
    }


def test_idle_observes_without_mutation_when_admission_disabled():
    result = run(admission=False, jobs=[job("pending")])
    assert result["decision"] == "none"
    assert result["steps"] == ["Write_State"]
    assert result["state"]["lastObservedUtc"] == NOW
    assert result["state"]["usedSeconds"] == 0
    assert not result["failed"]


def test_idle_admits_pending_tagged_job_and_starts_both_resources():
    result = run(jobs=[job("pending"), job("pending", tags=("other",))])
    assert result["decision"] == "start"
    assert result["steps"] == ["Write_State", "Start_VM", "Start_AKS"]
    assert result["state"]["phase"] == "starting" and result["state"]["startedUtc"] == NOW


@pytest.mark.parametrize(
    "case",
    [
        {"jobs": []},
        {"jobs": [job("running", runner=7)]},
        {"jobs": [job("pending")], "state": base_state(uncertain=True)},
        {"jobs": [job("pending")], "state": base_state(usedSeconds=72000 - 4799)},
        {"jobs": [job("pending")], "jobs_ok": False},
        {"jobs": [job("pending")] * 100},
        {"jobs": [job("pending")], "now": "2026-11-30T22:40:00Z"},
    ],
)
def test_idle_does_not_start_when_admission_is_not_safe(case):
    result = run(**case)
    assert result["decision"] in ("none", "frozen")
    assert not any(step.startswith(("Start", "Retry")) for step in result["steps"])


def test_admission_boundary_is_exactly_eighty_minutes():
    assert run(jobs=[job("pending")], state=base_state(usedSeconds=72000 - 4800))["decision"] == "start"


def test_month_end_deferral_uses_berlin_month_across_dst():
    for now, expected in (("2026-03-31T20:39:00Z", "start"), ("2026-03-31T20:41:00Z", "none")):
        state = base_state(month="2026-03", lastObservedUtc="2026-03-31T20:30:00Z")
        assert run(jobs=[job("pending")], now=now, state=state)["decision"] == expected


def test_new_berlin_month_resets_usage_but_counts_current_activity():
    state = base_state(usedSeconds=60000, lastObservedUtc="2026-11-30T22:58:00Z", lastActive=True)
    result = run(now="2026-11-30T23:03:00Z", state=state, power=ON)
    assert result["state"]["month"] == "2026-12"
    assert result["state"]["usedSeconds"] == 300


def test_active_resource_time_is_added_and_idle_gap_is_not_counted():
    active = run(power=ON, state=base_state(lastActive=True, usedSeconds=100))
    assert active["state"]["usedSeconds"] == 400
    idle = run(state=base_state(usedSeconds=100))
    assert idle["state"]["usedSeconds"] == 100


def test_unobserved_active_gap_freezes_admission_until_manual_reset():
    state = base_state(lastActive=True, lastObservedUtc="2026-11-25T09:00:00Z", usedSeconds=100)
    result = run(state=state, jobs=[job("pending")])
    assert result["state"]["uncertain"] is True
    assert result["state"]["usedSeconds"] == 100
    assert result["decision"] == "frozen" and result["failed"]
    assert not any(step.startswith("Start") for step in result["steps"])


def test_manual_or_foreign_activity_is_counted_alerted_and_never_stopped():
    for power in (ON, ("PowerState/running", "Stopped"), OFF[:1] + ("Running",)):
        result = run(power=power, state=base_state(lastActive=True))
        assert result["decision"] == "foreign" and result["failed"]
        assert result["steps"] == ["Write_State"]
        assert result["state"]["usedSeconds"] == 300


@pytest.mark.parametrize("vm", [True, False])
def test_unknown_resource_state_makes_no_write_or_mutation(vm):
    result = run(vm_ok=vm, aks_ok=not vm, power=ON, state=base_state(phase="running"))
    assert result["decision"] == "unknown" and result["failed"]
    assert result["steps"] == []


def test_starting_waits_then_starts_runner_when_both_resources_are_ready():
    starting = base_state(phase="starting", startedUtc="2026-11-25T10:00:00Z")
    assert run(state=starting, power=("PowerState/running", "Transitioning"))["decision"] == "wait"
    ready = run(state=starting, power=ON, provisioning="Succeeded")
    assert ready["decision"] == "startRunner"
    assert ready["steps"] == ["Write_State", "Run_command"]
    assert ready["state"]["phase"] == "running" and ready["state"]["runnerStartedUtc"] == NOW


def test_starting_retries_only_the_resource_that_is_off_after_ten_minutes():
    starting = base_state(phase="starting", startedUtc="2026-11-25T09:50:00Z")
    result = run(state=starting, power=("PowerState/running", "Stopped"))
    assert result["decision"] == "retryStart"
    assert result["steps"] == ["Write_State", "Retry_AKS_start"]
    soon = base_state(phase="starting", startedUtc="2026-11-25T10:00:00Z")
    assert run(state=soon, power=("PowerState/running", "Stopped"))["decision"] == "wait"


def test_startup_timeout_aborts_and_stops_owned_resources():
    starting = base_state(phase="starting", startedUtc="2026-11-25T09:19:00Z")
    result = run(state=starting, power=("PowerState/running", "Stopped"))
    assert result["decision"] == "abort" and result["failed"]
    assert result["steps"] == ["Write_State", "Deallocate_VM"]
    assert result["state"]["phase"] == "stopping"


def running_state(started="2026-11-25T09:00:00Z", **overrides):
    return base_state(phase="running", runnerStartedUtc=started, lastActive=True, **overrides)


def test_running_never_stops_while_runner_has_an_active_job():
    for status in ("created", "pending", "preparing", "running", "canceling", "waiting_for_resource"):
        result = run(state=running_state(), power=ON, jobs=[job(status, runner=7)])
        assert result["decision"] == "wait"
        assert not any("Stop" in step or "Deallocate" in step for step in result["steps"])


def test_running_ignores_other_runner_and_waits_for_grace_period():
    other = run(state=running_state(), power=ON, jobs=[job("running", runner=8)])
    assert other["decision"] == "stop"
    early = run(state=running_state(started="2026-11-25T09:55:00Z"), power=ON)
    assert early["decision"] == "wait"


def test_idle_runner_after_grace_stops_both_resources_in_stopping_phase():
    result = run(state=running_state(), power=ON)
    assert result["decision"] == "stop" and not result["failed"]
    assert result["steps"] == ["Write_State", "Deallocate_VM", "Stop_AKS"]
    assert result["state"]["phase"] == "stopping"


@pytest.mark.parametrize("kwargs", [{"jobs_ok": False}, {"jobs": [job("running", runner=7)] * 100}])
def test_ambiguous_job_state_blocks_idle_shutdown(kwargs):
    assert run(state=running_state(), power=ON, **kwargs)["decision"] == "wait"


def test_overrun_stops_even_when_gitlab_state_is_unknown():
    state = running_state(started="2026-11-25T08:24:00Z")
    result = run(state=state, power=ON, jobs_ok=False)
    assert result["decision"] == "overrun" and result["failed"]
    assert result["steps"] == ["Write_State", "Deallocate_VM", "Stop_AKS"]


def test_running_with_missing_resource_moves_to_stopping():
    result = run(state=running_state(), power=("PowerState/deallocated", "Running"))
    assert result["decision"] == "stopped" and result["failed"]
    assert result["steps"] == ["Write_State", "Stop_AKS"]


def test_stopping_stops_stopped_but_billed_vm_and_finishes_when_off():
    stopping = base_state(phase="stopping", lastActive=True)
    assert run(state=stopping, power=("PowerState/stopped", "Stopped"))["steps"] == ["Write_State", "Deallocate_VM"]
    assert run(state=stopping, power=("PowerState/deallocating", "Stopped"))["decision"] == "wait"
    done = run(state=stopping, power=OFF)
    assert done["decision"] == "finished" and done["state"]["phase"] == "idle"


@pytest.mark.parametrize("vm", ["PowerState/running", "PowerState/deallocated", "PowerState/stopped", "PowerState/starting"])
@pytest.mark.parametrize("aks", ["Running", "Stopped", "Transitioning"])
def test_idle_phase_never_stops_or_deallocates_anything(vm, aks):
    result = run(power=(vm, aks), jobs=[job("pending")])
    assert not any("Stop" in step or "Deallocate" in step or step == "Run_command" for step in result["steps"])
    if (vm, aks) != OFF:
        assert result["decision"] == "foreign" and result["steps"] == ["Write_State"]


def test_unknown_phase_is_not_acted_on():
    result = run(state=base_state(phase="corrupt"))
    assert result["decision"] == "unknown" and result["steps"] == []


def test_state_write_is_conditional_and_precedes_every_mutation():
    apply_actions = DEFINITION["actions"]["Apply_Decision"]["actions"]
    assert apply_actions["Write_State"]["inputs"]["headers"]["If-Match"].startswith("@coalesce(")
    assert apply_actions["Mutate"]["runAfter"] == {"Write_State": ["Succeeded"]}


def test_all_expressions_parse_and_reference_existing_actions_and_parameters():
    names = {name for name, _ in walk(DEFINITION["actions"])}
    for name, action in walk(DEFINITION["actions"]):
        for target in action["runAfter"] if "runAfter" in action else []:
            assert target in names, (name, target)
    text = json.dumps(DEFINITION["actions"])
    for referenced in re.findall(r"(?:outputs|body|actions)\(\\?'([^'\\]+)\\?'\)", text):
        assert referenced in names, referenced
    for parameter in re.findall(r"parameters\(\\?'([^'\\]+)\\?'\)", text):
        assert parameter in DEFINITION["parameters"], parameter

    def expressions(value):
        if isinstance(value, str):
            if value.startswith("@") and not value.startswith("@{"):
                yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from expressions(child)
        elif isinstance(value, list):
            for child in value:
                yield from expressions(child)

    for expression in expressions(DEFINITION["actions"]):
        try:
            _Parser(expression[1:]).parse()
        except ExpressionError as error:
            pytest.fail(f"{expression[:80]}: {error}")


def test_example_bootstrap_state_fails_closed_until_operator_verifies_it():
    example = json.loads((WORKFLOW.parent / "state.example.json").read_text(encoding="utf-8"))
    result = run(state=example, jobs=[job("pending")])
    assert result["decision"] == "frozen" and result["failed"]
    assert result["steps"] == ["Write_State"]


def test_workflow_is_serialized_and_sensitive_data_is_protected():
    trigger = DEFINITION["triggers"]["Every_5_minutes"]
    assert trigger["recurrence"] == {"frequency": "Minute", "interval": 5}
    assert trigger["runtimeConfiguration"]["concurrency"]["runs"] == 1
    actions = DEFINITION["actions"]
    assert "outputs" in actions["Get_Token"]["runtimeConfiguration"]["secureData"]["properties"]
    for name in ("Get_Jobs", "Filter_Active", "Filter_Pending"):
        assert actions[name]["runtimeConfiguration"]["secureData"]["properties"] == ["inputs", "outputs"]
    assert DEFINITION["parameters"]["admissionEnabled"]["defaultValue"] is False
    assert not re.search(r"(?i)password|client_secret|bearer ", WORKFLOW.read_text(encoding="utf-8"))


def test_azure_mutations_do_not_wait_for_long_operations_or_retry():
    posts = [a for _, a in walk(DEFINITION["actions"]) if a["type"] == "Http" and a["inputs"]["method"] == "POST"]
    assert len(posts) == 7
    for action in posts:
        assert action["operationOptions"] == "DisableAsyncPattern"
        assert action["inputs"]["retryPolicy"] == {"type": "none"}
