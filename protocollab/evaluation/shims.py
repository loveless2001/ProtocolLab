"""Deliberately unenforced G0/G1 controls, ONLY on disposable simulator studies."""

from protocollab.gateway import ActionBroker


class LogOnlyBroker(ActionBroker):
    def __init__(self, *args, disposable_benchmark=False, **kwargs):
        if not disposable_benchmark:
            raise PermissionError("LOG_ONLY_SHIM_FORBIDDEN_OUTSIDE_DISPOSABLE_BENCHMARK")
        super().__init__(*args, **kwargs)

    def _authorize(self, proposal):
        # Syntax and adapter bindings remain fixed; authority/epoch are only logged.
        if proposal.namespace != self.store.namespace or proposal.resource_id != self.governance.task.resource_id:
            return "RESOURCE_OUT_OF_SCOPE"
        would_deny = super()._authorize(proposal)
        if would_deny:
            self.store.append("benchmark_shim", "governance.would_deny", {
                "command_id": proposal.command_id, "reason": would_deny, "threat_model": "log_only_simulator"})
        return None


def install_log_only(runtime, condition, *, disposable_benchmark=False):
    if condition not in ("G0", "G1"):
        raise ValueError("NOT_LOG_ONLY_CONDITION")
    runtime.broker = LogOnlyBroker(runtime.store, runtime.governance, runtime.backend, runtime.capture,
                                   runtime.belief, runtime.models.current_snapshot,
                                   disposable_benchmark=disposable_benchmark)
    runtime.store.set("evaluation", "governance_condition", condition, "benchmark.shim_installed")
