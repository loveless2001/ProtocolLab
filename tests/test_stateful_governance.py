import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from protocollab.contracts import MUTATIONS
from protocollab.evaluation.interventions import signed_control
from protocollab.evaluation.runner import demo_authorities
from protocollab.runtime import Runtime
from protocollab_environment.generator import ProtocolConfig
from protocollab_environment.service import EnvironmentService


class GovernedActions(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.directory = tempfile.TemporaryDirectory(prefix="protocollab-stateful-")
        root = Path(self.directory.name)
        self.backend = EnvironmentService(root / "private.sqlite", ProtocolConfig())
        keys, self.credentials = demo_authorities()
        self.runtime = Runtime(root / "owner", self.backend, keys)
        self.runtime.max_turns = 1000

    @rule(verb=st.sampled_from(["PAUSE_DISPATCH", "RESUME"]))
    def control(self, verb):
        key, key_id = self.credentials["operator"]
        assert signed_control(self.runtime, key, key_id, "operator", verb)["status"] == "ACCEPTED"

    @rule(operation=st.sampled_from(sorted(MUTATIONS)), grant=st.booleans())
    def permission(self, operation, grant):
        key, key_id = self.credentials["owner"]
        assert signed_control(self.runtime, key, key_id, "owner", "GRANT" if grant else "REVOKE",
                              operation=operation)["status"] == "ACCEPTED"

    @rule(operation=st.sampled_from(["SUBMIT_A", "SUBMIT_B", "SIGNAL_X", "SIGNAL_Y", "CANCEL", "INSPECT", "WAIT"]))
    def action(self, operation):
        before = self.runtime.governance.snapshot
        result = self.runtime.turn(operation)
        if operation in MUTATIONS:
            permitted = before["statuses"]["R"] == "RUNNING" and operation in before["permissions"]["R"]
            assert (result["action"]["status"] == "ACKNOWLEDGED") == permitted

    @invariant()
    def journal_is_valid(self):
        self.runtime.store.verify()

    def teardown(self):
        self.runtime.close()
        self.backend.close()
        self.directory.cleanup()


TestGovernedActions = GovernedActions.TestCase
TestGovernedActions.settings = settings(max_examples=12, stateful_step_count=18, deadline=None)
