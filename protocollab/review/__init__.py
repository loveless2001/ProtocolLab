"""Scoped appeals do not suspend binding corrections."""

from protocollab.contracts import uid


class ReviewService:
    def __init__(self, store, governance, max_appeals=4, deadline_ticks=10):
        self.store, self.governance = store, governance
        self.max_appeals, self.deadline_ticks = max_appeals, deadline_ticks
        governance.review_resolver = self.resolve_verified

    def submit(self, scope, reason, evidence_refs, tick, principal="actor", requested_scope=None):
        if scope not in self.governance.snapshot["statuses"]:
            return {"status": "REJECTED", "reason": "OUT_OF_SCOPE"}
        if requested_scope is not None and (scope != "agent_all" or requested_scope not in self.governance.snapshot["permissions"]):
            return {"status": "REJECTED", "reason": "NARROWING_MUST_BE_A_CONTAINED_RESOURCE"}
        appeals = self.store.get("review", "appeals", {})
        if len(appeals) >= self.max_appeals:
            return {"status": "REJECTED", "reason": "APPEAL_RATE_LIMIT"}
        for ref in evidence_refs:
            self.store.blob(ref, raw=True)  # attributable immutable evidence, not an invented citation
        appeal_id = uid("appeal")
        appeal = {"appeal_id": appeal_id, "scope": scope, "reason": reason,
                  "evidence_refs": list(evidence_refs), "status": "PENDING",
                  "deadline_tick": tick + self.deadline_ticks, "submitter": principal,
                  "binding_control_revision": self.governance.snapshot["revision"],
                  "binding_status": self.governance.snapshot["statuses"][scope]}
        appeal["requested_scope"] = requested_scope
        appeals[appeal_id] = appeal
        self.store.set("review", "appeals", appeals, "appeal.submitted", principal)
        return appeal

    def expire(self, tick):
        appeals = self.store.get("review", "appeals", {})
        changed = False
        for appeal in appeals.values():
            if appeal["status"] == "PENDING" and tick > appeal["deadline_tick"]:
                appeal["status"] = "TIMED_OUT"
                changed = True
        if changed:
            self.store.set("review", "appeals", appeals, "appeal.timeout")

    def resolve_verified(self, event, control, delegation):
        appeals = self.store.get("review", "appeals", {})
        appeal = appeals.get(event.appeal_id)
        if not appeal or appeal["status"] != "PENDING" or appeal["scope"] != event.scope_ref:
            raise PermissionError("NO_PENDING_SCOPED_APPEAL")
        if appeal["binding_control_revision"] != control["revision"]:
            raise PermissionError("STALE_APPEAL_CONTROL_REVISION")
        if event.resolution == "NARROW":
            requested = appeal.get("requested_scope")
            if (event.scope_ref != "agent_all" or not requested or requested not in delegation.scopes
                    or "RESUME" not in delegation.verbs or appeal["binding_status"] not in ("PAUSED", "HOLD")):
                raise PermissionError("NARROW_REQUIRES_EXPLICIT_DELEGATED_SCOPE")
            control["statuses"]["agent_all"] = "RUNNING"
            if control["statuses"][requested] != "HOLD":
                control["statuses"][requested] = appeal["binding_status"]
        if event.resolution == "LIFT":
            if "RESUME" not in delegation.verbs:
                raise PermissionError("REVIEWER_LACKS_DELEGATED_RESUME")
            if appeal["binding_status"] not in ("PAUSED", "HOLD"):
                raise PermissionError("LIFT_DOES_NOT_GRANT_PERMISSIONS")
            control["statuses"][event.scope_ref] = "RUNNING"
        appeal["status"] = "RESOLVED"
        appeal["resolution"] = event.resolution
        appeal["reviewer"] = delegation.principal
        self.store.set("review", "appeals", appeals, "appeal.resolved", delegation.principal)
