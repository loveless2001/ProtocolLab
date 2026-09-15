import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from protocollab.contracts import ControlEvent, uid
from protocollab.governance import Delegation, sign_control
from protocollab.runtime import Runtime
from protocollab_environment.generator import ProtocolConfig
from protocollab_environment.service import EnvironmentService


@pytest.fixture
def runtime(tmp_path):
    key = Ed25519PrivateKey.generate()
    keys = {"operator-key": Delegation("operator", key.public_key().public_bytes_raw(),
        frozenset(("PAUSE_DISPATCH", "RESUME", "REVOKE", "GRANT", "REDIRECT", "REVIEW_RESOLUTION", "HOLD")),
        frozenset(("agent_all", "R", "replica")))}
    environment = EnvironmentService(tmp_path / "private.sqlite", ProtocolConfig())
    rt = Runtime(tmp_path / "runtime", environment, keys)
    rt.test_key = key
    yield rt
    rt.close()
    environment.close()


def control(runtime, verb, scope="R", **fields):
    event = ControlEvent(event_id=uid(), issuer_principal_ref="operator", issuer_seq=runtime.governance.snapshot["revision"],
        nonce=uid(), scope_ref=scope, expected_revision=runtime.governance.snapshot["revision"],
        verb=verb, auth_evidence_ref="signature-envelope", **fields)
    return runtime.governance.submit_authenticated(sign_control(event, runtime.test_key, "operator-key"))


@pytest.fixture
def modeled_runtime(runtime):
    """Privileged planner UNIT-test fixture; not a learner or admission result."""
    from protocollab.contracts import ALPHABET, ModelArtifact, Transition, protected_dependencies
    from protocollab_environment.generator import reachable, transition
    config = ProtocolConfig()
    states = list(reachable(config))
    names = {state: f"q{i}" for i, state in enumerate(states)}
    artifact = ModelArtifact(model_id="test-oracle", namespace="run", revision=1, initial_state="q0",
        states=list(names.values()), alphabet=list(ALPHABET),
        transitions=[Transition(from_state=names[s], input=a, to_state=names[transition(config, s, a)[0]],
                                output=transition(config, s, a)[1]) for s in states for a in ALPHABET],
        protected_dependencies=protected_dependencies())
    key = runtime.store.put_blob(artifact)
    runtime.store.set("model", "active", {"hash": key, "revision": 1}, "model.promoted")
    runtime.belief.rebase(runtime.models.current_snapshot(), [])
    return runtime
