#!/usr/bin/env bun
import readline from "readline";
import * as KMod from "../../verified/lifecycle/Kernel.bend";
const K = KMod.default;

// Conversion helpers between JSON wire format and Bend ADT structures.
export function arrayToBendList(arr) {
  let res = { $: "Nil" };
  if (!arr) return res;
  for (let i = arr.length - 1; i >= 0; i--) {
    res = { $: "Con", head: arr[i], tail: res };
  }
  return res;
}

export function bendListToArray(list) {
  const arr = [];
  let curr = list;
  while (curr && (curr.$ === "Con" || curr.$ === "Cons")) {
    arr.push(curr.head);
    curr = curr.tail;
  }
  return arr;
}

export function maybeToBend(val) {
  if (val === null || val === undefined) {
    return { $: "None" };
  }
  return { $: "Some", value: val };
}

export function bendToMaybe(m) {
  if (!m || m.$ === "None") return null;
  if (m.$ === "Some") return m.value;
  return null;
}

export function safeNumber(val) {
  if (val === null || val === undefined) return val;
  const bi = BigInt(val);
  if (bi > BigInt(Number.MAX_SAFE_INTEGER) || bi < -BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new RangeError(`Integer ${bi} exceeds JS Number.MAX_SAFE_INTEGER (${Number.MAX_SAFE_INTEGER})`);
  }
  return Number(bi);
}

export function stageLimitToBend(sl) {
  return {
    $: "SLimit",
    stage: sl.stage,
    max_calls: BigInt(sl.max_calls),
    max_tokens: BigInt(sl.max_tokens)
  };
}

export function stageLimitFromBend(sl) {
  return {
    stage: sl.stage,
    max_calls: safeNumber(sl.max_calls),
    max_tokens: safeNumber(sl.max_tokens)
  };
}

// Detect if Bend runtime prefixes imported ADT constructors with module name
let transportPrefix = "Types.";
try {
  const dummyState = {
    $: "LState",
    run_id: "",
    config_hash: "",
    agg_max_calls: 1n,
    agg_max_tokens: 1n,
    stage_limits: { $: "Con", head: { $: "SLimit", stage: "_s", max_calls: 1n, max_tokens: 1n }, tail: { $: "Nil" } },
    requests: { $: "Nil" },
    fault: { $: "None" }
  };
  const dummyEv = {
    $: "EvReserve",
    req_id: "_probe",
    stage: "_s",
    basis_ref: "",
    config_hash: "",
    max_input: 1n,
    max_output: 0n
  };
  const r = K.apply(dummyState, dummyEv);
  if (r && r.state && r.state.requests && r.state.requests.head && r.state.requests.head.transport) {
    transportPrefix = r.state.requests.head.transport.$.startsWith("Types.") ? "Types." : "";
  }
} catch (e) {}

export function transportToBend(tr) {
  let tag = typeof tr === "string" ? tr : (tr.$ || "Prepared");
  if (tag.startsWith("Types.")) tag = tag.slice(6);
  return { $: transportPrefix + tag };
}

export function transportFromBend(tr) {
  if (!tr) return "Prepared";
  let tag = tr.$;
  if (tag.startsWith("Types.")) tag = tag.slice(6);
  return tag;
}

export function chargeToBend(ch) {
  if (ch.$) {
    let tag = ch.$;
    if (tag.startsWith("Types.")) tag = tag.slice(6);
    return { ...ch, $: tag };
  }
  let kind = ch.kind || "Pending";
  if (kind.startsWith("Charge")) kind = kind.slice(6);
  if (kind === "Pending") {
    return { $: "ChargePending", tokens: BigInt(ch.tokens) };
  }
  if (kind === "Settled") {
    return {
      $: "ChargeSettled",
      input_tokens: BigInt(ch.input_tokens),
      output_tokens: BigInt(ch.output_tokens),
      receipt_hash: ch.receipt_hash
    };
  }
  if (kind === "Released") {
    return { $: "ChargeReleased", evidence_hash: ch.evidence_hash };
  }
  throw new Error(`Unknown charge kind: ${JSON.stringify(ch)}`);
}

export function chargeFromBend(ch) {
  let tag = ch.$;
  if (tag.startsWith("Types.")) tag = tag.slice(6);
  if (tag === "ChargePending") {
    return { kind: "Pending", tokens: safeNumber(ch.tokens) };
  }
  if (tag === "ChargeSettled") {
    return {
      kind: "Settled",
      input_tokens: safeNumber(ch.input_tokens),
      output_tokens: safeNumber(ch.output_tokens),
      receipt_hash: ch.receipt_hash
    };
  }
  if (tag === "ChargeReleased") {
    return { kind: "Released", evidence_hash: ch.evidence_hash };
  }
  throw new Error(`Unknown Bend charge: ${JSON.stringify(ch)}`);
}

export function requestRecordToBend(rec) {
  return {
    $: "ReqRecord",
    req_id: rec.req_id,
    stage: rec.stage,
    basis_ref: rec.basis_ref,
    config_hash: rec.config_hash,
    max_input: BigInt(rec.max_input),
    max_output: BigInt(rec.max_output),
    transport: transportToBend(rec.transport),
    charge: chargeToBend(rec.charge)
  };
}

export function requestRecordFromBend(rec) {
  return {
    req_id: rec.req_id,
    stage: rec.stage,
    basis_ref: rec.basis_ref,
    config_hash: rec.config_hash,
    max_input: safeNumber(rec.max_input),
    max_output: safeNumber(rec.max_output),
    transport: transportFromBend(rec.transport),
    charge: chargeFromBend(rec.charge)
  };
}

export function stateToBend(state) {
  return {
    $: "LState",
    run_id: state.run_id,
    config_hash: state.config_hash,
    agg_max_calls: BigInt(state.agg_max_calls),
    agg_max_tokens: BigInt(state.agg_max_tokens),
    stage_limits: arrayToBendList((state.stage_limits || []).map(stageLimitToBend)),
    requests: arrayToBendList((state.requests || []).map(requestRecordToBend)),
    fault: maybeToBend(state.fault)
  };
}

export function stateFromBend(state) {
  return {
    run_id: state.run_id,
    config_hash: state.config_hash,
    agg_max_calls: safeNumber(state.agg_max_calls),
    agg_max_tokens: safeNumber(state.agg_max_tokens),
    stage_limits: bendListToArray(state.stage_limits).map(stageLimitFromBend),
    requests: bendListToArray(state.requests).map(requestRecordFromBend),
    fault: bendToMaybe(state.fault)
  };
}

export function eventToBend(ev) {
  let kind = ev.kind || ev.$;
  if (kind.startsWith("Types.")) kind = kind.slice(6);
  if (kind === "Reserve" || kind === "EvReserve") {
    return {
      $: "EvReserve",
      req_id: ev.req_id,
      stage: ev.stage,
      basis_ref: ev.basis_ref,
      config_hash: ev.config_hash,
      max_input: BigInt(ev.max_input),
      max_output: BigInt(ev.max_output)
    };
  }
  if (kind === "DispatchIntent" || kind === "EvDispatchIntent") {
    return { $: "EvDispatchIntent", req_id: ev.req_id };
  }
  if (kind === "TransportObserved" || kind === "EvTransportObserved") {
    return { $: "EvTransportObserved", req_id: ev.req_id, boundary: ev.boundary };
  }
  if (kind === "SettleUsage" || kind === "EvSettleUsage") {
    return {
      $: "EvSettleUsage",
      req_id: ev.req_id,
      input_tokens: BigInt(ev.input_tokens),
      output_tokens: BigInt(ev.output_tokens),
      receipt_hash: ev.receipt_hash
    };
  }
  if (kind === "FailureConclusive" || kind === "EvFailureConclusive") {
    return { $: "EvFailureConclusive", req_id: ev.req_id, evidence_hash: ev.evidence_hash };
  }
  if (kind === "TimeoutUnknown" || kind === "EvTimeoutUnknown") {
    return { $: "EvTimeoutUnknown", req_id: ev.req_id, reason: ev.reason };
  }
  throw new Error(`Unknown lifecycle event kind: ${JSON.stringify(ev)}`);
}

export function verdictFromBend(verdict) {
  let tag = verdict.$;
  if (tag.startsWith("Types.")) tag = tag.slice(6);
  if (tag === "Accepted") {
    const rawIntent = bendToMaybe(verdict.intent);
    let intent = null;
    if (rawIntent) {
      intent = {
        req_id: rawIntent.req_id,
        stage: rawIntent.stage,
        basis_ref: rawIntent.basis_ref,
        config_hash: rawIntent.config_hash,
        max_input: safeNumber(rawIntent.max_input),
        max_output: safeNumber(rawIntent.max_output)
      };
    }
    return { kind: "Accepted", intent };
  }
  if (tag === "DuplicateNoop") {
    return { kind: "DuplicateNoop" };
  }
  if (tag === "Rejected") {
    return { kind: "Rejected", reason: verdict.reason };
  }
  if (tag === "ConflictFault") {
    return { kind: "ConflictFault", reason: verdict.reason };
  }
  throw new Error(`Unknown verdict: ${JSON.stringify(verdict)}`);
}

export function summaryFromBend(sum) {
  const stageSummaries = bendListToArray(sum.stage_summaries).map(ss => ({
    stage: ss.stage,
    calls_admitted: safeNumber(ss.calls_admitted),
    spent_tokens: safeNumber(ss.spent_tokens),
    held_tokens: safeNumber(ss.held_tokens),
    committed_tokens: safeNumber(ss.committed_tokens)
  }));
  return {
    total_spent: safeNumber(sum.total_spent),
    total_held: safeNumber(sum.total_held),
    total_committed: safeNumber(sum.total_committed),
    stage_summaries: stageSummaries,
    fault: bendToMaybe(sum.fault)
  };
}

export function handleCommand(msg) {
  const cmd = msg.cmd;
  if (cmd === "init") {
    const s0 = K["Definitions.empty_state"](
      msg.run_id,
      msg.config_hash,
      BigInt(msg.agg_max_calls),
      BigInt(msg.agg_max_tokens),
      arrayToBendList((msg.stage_limits || []).map(stageLimitToBend))
    );
    return { ok: true, state: stateFromBend(s0) };
  }
  if (cmd === "apply") {
    const bendState = stateToBend(msg.state);
    const bendEv = eventToBend(msg.event);
    const res = K.apply(bendState, bendEv);
    return {
      ok: true,
      state: stateFromBend(res.state),
      verdict: verdictFromBend(res.verdict)
    };
  }
  if (cmd === "fold") {
    const bendState = stateToBend(msg.state);
    const bendEvents = arrayToBendList((msg.events || []).map(eventToBend));
    const nextState = K.fold(bendEvents, bendState);
    return { ok: true, state: stateFromBend(nextState) };
  }
  if (cmd === "summarize") {
    const bendState = stateToBend(msg.state);
    const sum = K.summarize(bendState);
    return { ok: true, summary: summaryFromBend(sum) };
  }
  throw new Error(`Unknown command: ${cmd}`);
}

async function main() {
  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: false
  });

  for await (const line of rl) {
    if (!line.trim()) continue;
    try {
      const msg = JSON.parse(line);
      const res = handleCommand(msg);
      process.stdout.write(JSON.stringify(res) + "\n");
    } catch (err) {
      process.stdout.write(JSON.stringify({ ok: false, error: err.message || String(err) }) + "\n");
    }
  }
}

if (import.meta.main) {
  main();
}
