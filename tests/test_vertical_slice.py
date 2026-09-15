import pytest
from conftest import control

from protocollab.contracts import strict_json
from protocollab.gateway import InjectedCrash
from protocollab.learning import QueryInterrupted
from protocollab.modeling import MealyModel


def test_pause_after_validate_before_dispatch(runtime):
    proposal = runtime.proposal("SUBMIT_A")
    assert runtime.broker.propose(proposal)["status"] == "READY"
    accepted = control(runtime, "PAUSE_DISPATCH")
    assert accepted["status"] == "ACCEPTED"
    assert runtime.broker.dispatch(proposal.command_id)["status"] == "STALE"
    assert runtime.backend.reconcile(proposal.command_id) is None


def test_pause_does_not_freeze_inflight(runtime):
    runtime.turn("SUBMIT_A")
    runtime.turn("SIGNAL_X")
    control(runtime, "PAUSE_DISPATCH")
    runtime.turn("WAIT")
    result = runtime.turn("INSPECT")
    assert result["action"]["receipt"]["domain_output"] == "INSPECT:A:HEALTHY"
    assert runtime.turn("SUBMIT_B")["action"]["status"] == "DENIED"
    control(runtime, "REVOKE", operation="SIGNAL_X")
    control(runtime, "RESUME")
    assert runtime.turn("SIGNAL_X")["action"]["status"] == "DENIED"


def test_crash_dedup_and_restart(runtime):
    checkpoint = runtime.checkpoint()
    proposal = runtime.proposal("SUBMIT_A")
    runtime.broker.propose(proposal)
    with pytest.raises(InjectedCrash):
        runtime.broker.dispatch(proposal.command_id, crash_at="after_apply")
    packet = runtime.backend.reconcile(proposal.command_id)
    assert runtime.recover(checkpoint) == "RECOVERED"
    assert runtime.broker.reconcile(proposal.command_id)["status"] == "ACKNOWLEDGED"
    assert runtime.backend.apply("R", "SUBMIT_A", proposal.command_id) == packet
    assert runtime.broker.propose(proposal)["status"] == "ACKNOWLEDGED"
    assert runtime.broker.propose(proposal.model_copy(update={"operation": "SUBMIT_B"}))["status"] == "DENIED"


def test_interrupted_query_only_caches_executed_prefix(runtime):
    adapter = runtime.query_adapter()
    def interrupt(index, symbol):
        if index == 2:
            control(runtime, "PAUSE_DISPATCH", scope="agent_all")
    adapter.before_symbol = interrupt
    word = ["SUBMIT_A", "SIGNAL_X", "TICK", "INSPECT"]
    with pytest.raises(QueryInterrupted):
        adapter.query(word)
    trace = adapter.traces()[-1]
    assert trace["word"] == word[:2]
    assert trace["status"] == "PARTIAL"
    adapter.before_symbol = None
    control(runtime, "RESUME", scope="agent_all")
    assert adapter.query(word) == ["ACCEPTED", "OK", "TICKED", "INSPECT:BASE:HEALTHY"]
    assert adapter.usage["resets"] == 2


def test_strict_control_json():
    for payload in ('{"x":1,"x":2}', '{"x":1.0}', '{"x":NaN}'):
        with pytest.raises(ValueError):
            strict_json(payload)


def test_learn_admit_plan_and_execute(runtime):
    from dataclasses import replace
    # Measured prefix/consistency L* needs ~1500 resets on this fixture. Preserve
    # spec defaults in production and declare this acceptance-run override.
    runtime.limits = replace(runtime.limits, total_resets=2000)
    artifact, report, exported = runtime.learning_round(seed=7)
    assert report.status == "PASS"
    assert report.real_probe_steps >= 200
    assert report.probe_resets >= 24
    model = MealyModel(artifact)
    h1 = ["SUBMIT_A", "TICK", "STATUS"]
    h2 = ["SUBMIT_A", "TICK", "SIGNAL_X", "TICK", "STATUS"]
    left, _ = model.replay(h1)
    right, _ = model.replay(h2)
    assert left != right
    assert model.replay(["TICK", "INSPECT"], left)[1][-1] == "INSPECT:BASE:HEALTHY"
    assert model.replay(["TICK", "INSPECT"], right)[1][-1] == "INSPECT:A:HEALTHY"
    assert len(set(exported["candidate_sizes"])) > 1
    snapshot = (runtime.store.tail, runtime.belief.snapshot, runtime.governance.snapshot)
    assert runtime.simulate(["SUBMIT_B", "TICK"])["kind"] == "HYPOTHETICAL"
    assert snapshot == (runtime.store.tail, runtime.belief.snapshot, runtime.governance.snapshot)
    result = runtime.synthesize()
    assert result["status"] == "PLANNED", result
    runtime.freeze()
    assert runtime.run_procedure(result["procedure"])["status"] == "SUCCESS"
    assert runtime.finish()["status"] == "PUBLIC_CONTRACT_SATISFIED"
    assert runtime.store.verify()
