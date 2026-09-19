# ProtocolLab — Trust Boundary & Evidence Specification

**Date:** 2026-09-18
**Component:** Inference-Request Lifecycle and Accounting Kernel
**Status:** Frozen Specification

---

## 1. Explicit Scope of Verification Claims

### What the Bend Proofs Formally Establish:
1. **Mathematical Transition Soundness:**
   The Bend functions `apply`, `fold`, and `summarize` satisfy the 14 declared laws in `LAWS.bend` under the type system, termination semantics, and reduction rules of the pinned Bend 2.0.16 compiler. Laws 13 and 14 universally quantify over validation transition inputs; the other non-reflexive laws are checked reference traces.
2. **Quota & Admission Soundness:**
   No well-formed trace can admit a reservation that violates declared stage or aggregate upper bounds without transitioning to a rejected or faulted state.
3. **Receipt Idempotence & Settlement Monotonicity:**
   Reprocessing an identical final usage receipt is a strict no-op that cannot increase confirmed charges, alter past stage allocations, or yield duplicate dispatch intentions. Confirmed spending monotonically increases upon valid settlements.
4. **Uncertainty Conservation:**
   A transport interruption, timeout, or uncertain delivery boundary retains its conservative pending charge until valid reconciliation evidence is presented.
5. **No Counter Drift:**
   Stage and aggregate accounting summaries are total mathematical reductions over unique `RequestRecord`s, eliminating desynchronization between stage and run counters.

---

## 2. What the Proofs Explicitly Do NOT Establish (External Trust Boundary)

The following aspects remain strictly outside the mathematical proof and depend on the integrity of the external environment, Python runtime, host OS, and hardware:

| Unverified Boundary | Nature of External Dependency |
|---|---|
| **Physical Transport Truth** | A `Sent` or `ResponseReceived` constructor in the kernel is a typed representation of shell-supplied evidence. The pure core cannot verify whether bytes physically crossed a network cable or socket. |
| **Provider Truthfulness** | The kernel verifies that reported tokens are correctly accounted for and compared against caps; it cannot prove whether an external model API accurately measured its actual FLOPs or prompt tokens. |
| **Persistence Durability** | SQLite transactions, WAL mode, file system fsync, and disk writes are managed by Python's `Store`. The kernel relies on the shell to atomically commit transitions. |
| **Cryptographic Authenticity** | Signatures, hashes, and Ed25519 identity checks are performed in Python via `cryptography`. The kernel compares hash strings by equality; it does not execute cryptographic primitives. |
| **Compiler & Runtime Equivalence** | The proof checks the Bend high-level definitions. Discrepancies between the Bend type checker, the C/JS code generation backends, or the Node/Bun JavaScript engines remain part of the trusted computing base. |
| **Task Progress & Semantic Utility** | Successfully accounting for a model call does not prove that the model's output was helpful, intelligent, or compliant with high-level user intentions. |

---

## 3. Ground Rules for Evidence & Claims

1. **No Untrusted `verified=True` Flags:**
   The Python shell and adapters must provide attributable receipts, SHA-256 payload digests, and explicit boundary grades (`local_generate_input`, `api_response_started`). A raw `TransportReport` cannot release or settle a charge; it must be attested by the invoked transport boundary or a configured trusted evidence adapter.
2. **Standardized Claim Sentence:**
   Any reporting on this system must state:
   *"The named lifecycle and accounting laws check for this Bend implementation under the stated input and toolchain assumptions; integration tests cover the persistence and transport boundaries."*
   Under no circumstances shall ProtocolLab or any report claim that "the entire agent is formally verified".
