"""Bounded, body-free cross-process QQ group ingress watchdog."""

import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import time
from collections import defaultdict
from pathlib import Path


DB = "/var/lib/agent-bridge/assistant.sqlite3"
STATE = Path("/var/lib/agent-bridge/group-ingress-monitor-state.json")
GROUP = re.compile(r"\((\d{6,})\)")
STAGE = re.compile(r"group_ingress_stage stage=(rate_entry|rate_wait|plugin|bridge|rate_wait_overrun) group_id=(\d{6,})")
BRIDGE_WAIT_TIMEOUT = re.compile(
    r"codex_agent group model failed group=(\d{6,}) kind=bridge_wait_timeout"
)
VOICE_STAGE = re.compile(
    r"group_voice_stage stage=(receipt_claim|ingress_write|ingress_transaction|candidate_claim|candidate_selected|receipt_settle|source_check|model_text) "
    r"group_id=(\d{6,}|all) elapsed_ms=\d+ status=(ok|failed|sqlite_busy|sqlite_error|required_source_unavailable)"
)
VOICE_HANDOFF = re.compile(r"group_voice_handoff group_id=(\d{6,}) outcome=(requeued|lost)")


def evaluate_voice_funnel(counts, enabled, state, last_complete):
    """Body-free opportunity alarms; no reply quota or policy mutation."""
    active = {key: set(value) for key, value in (state.get("voice_active") or {}).items()}
    events = []
    for group in sorted(enabled):
        def total(stage, buckets):
            values = counts.get(stage, {}).get(group, {})
            return sum(int(values.get(bucket, values.get(str(bucket), 0))) for bucket in buckets)

        recent = (last_complete, last_complete + 1)
        last_two = (last_complete - 1, last_complete)
        last_six = range(last_complete - 5, last_complete + 1)
        ack = total("ack", (last_complete - 1, last_complete, last_complete + 1))
        reasons = set()
        if total("sqlite_busy", recent):
            reasons.add("sqlite_busy")
        if total("required_source_unavailable", recent):
            reasons.add("required_source_unavailable")
        if total("handoff_requeued", last_six) >= 3 and total("ack", last_six) == 0:
            reasons.add("handoff_without_ack")
        if all(
            total("inbound", (bucket,)) >= 30
            and total("model_text", (bucket,)) >= 5
            and total("ack", (bucket,)) == 0
            for bucket in last_two
        ):
            reasons.add("quality_zero_natural_ack")
        previous = active.get(group, set())
        for reason in sorted(reasons - previous):
            events.append(("alert", group, reason))
        # A quiet group is not proof of recovery. An actual new natural ACK is.
        for reason in sorted(previous - reasons):
            if ack:
                events.append(("recovery", group, "natural_ack_observed"))
            else:
                reasons.add(reason)
        if reasons:
            active[group] = reasons
        else:
            active.pop(group, None)
    global_busy = counts.get("sqlite_busy", {}).get("all", {})
    global_recent = sum(int(global_busy.get(bucket, global_busy.get(str(bucket), 0)))
                        for bucket in (last_complete, last_complete + 1))
    global_previous = active.get("all", set())
    if global_recent and "sqlite_busy" not in global_previous:
        active["all"] = {"sqlite_busy"}
        events.append(("alert", "all", "sqlite_busy"))
    elif not global_recent and "sqlite_busy" in global_previous:
        if any(counts.get("ack", {}).get(group, {}).get(last_complete, 0) > 0 for group in enabled):
            active.pop("all", None)
            events.append(("recovery", "all", "natural_ack_observed"))
    return events, {**state, "voice_active": {group: sorted(reasons) for group, reasons in active.items()}}


def _database_voice_counts(connection, counts, since):
    """Read metadata only: model evaluation, commitment and ACK."""
    cutoff = dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat()
    queries = {
        "ack": """SELECT g.group_id,CAST(strftime('%s',g.created_at) AS INTEGER)/300,count(*)
            FROM group_messages g JOIN group_response_commitments c
              ON c.delivery_id=CASE WHEN json_valid(g.metadata_json)
                   THEN json_extract(g.metadata_json,'$.delivery_id') ELSE NULL END
             AND c.owner_kind='ambient'
            WHERE g.created_at>=? AND g.sender_id='bot' AND g.replied=1 GROUP BY 1,2""",
        "outbox_committed": """SELECT group_id,CAST(strftime('%s',updated_at) AS INTEGER)/300,count(*)
            FROM group_response_commitments WHERE updated_at>=? AND owner_kind='ambient'
            AND state='committed' GROUP BY 1,2""",
    }
    for stage, query in queries.items():
        for group, bucket, amount in connection.execute(query, (cutoff,)):
            if str(group or "").isdigit() and bucket is not None:
                counts[stage][str(group)][int(bucket)] += int(amount)


def evaluate(counts, enabled, state, last_complete):
    """Return transition notifications; quiet and model-silent groups remain quiet."""
    active = set(state.get("active") or [])
    active_reasons = dict(state.get("active_reasons") or {})
    events = []
    for group in sorted(enabled):
        upstream = counts.get("llbot", {}).get(group, {})
        bridge = counts.get("bridge", {}).get(group, {})
        overruns = counts.get("wait_overrun", {}).get(group, {})
        timeouts = counts.get("bridge_wait_timeout", {}).get(group, {})
        gap = all(
            upstream.get(bucket, 0) >= 10 and bridge.get(bucket, 0) == 0
            for bucket in (last_complete - 1, last_complete)
        )
        overrun = any(overruns.get(bucket, 0) > 0 for bucket in (last_complete, last_complete + 1))
        timeout_recent = any(timeouts.get(bucket, 0) > 0 for bucket in (last_complete, last_complete + 1))
        current_bridge = bridge.get(last_complete + 1, 0) > 0
        if group in active:
            if active_reasons.get(group) == "bridge_wait_timeout":
                clean = all(
                    timeouts.get(bucket, 0) == 0
                    for bucket in (last_complete - 1, last_complete, last_complete + 1)
                )
                resumed = any(bridge.get(bucket, 0) > 0 for bucket in (last_complete - 1, last_complete))
            else:
                clean = not timeout_recent and not overruns.get(last_complete + 1, 0)
                resumed = current_bridge
            if clean and resumed and not (timeout_recent or overrun or (gap and not current_bridge)):
                active.remove(group)
                active_reasons.pop(group, None)
                events.append(("recovery", group, "bridge_ingress_resumed"))
        elif timeout_recent or (gap and not current_bridge) or overrun:
            active.add(group)
            reason = "bridge_wait_timeout" if timeout_recent else "rate_wait_over_65s" if overrun else "upstream_bridge_gap"
            active_reasons[group] = reason
            events.append(("alert", group, reason))
    active_reasons = {group: reason for group, reason in active_reasons.items() if group in active}
    return events, {"active": sorted(active), "active_reasons": active_reasons, "updated_at": int(time.time())}


def evaluate_meme_expression(counts, state, last_complete):
    """Alert only after 20 completed expression choices in one scope with zero selections."""
    active = set(state.get("meme_expression_active") or [])
    events = []
    for scope in ("group", "private"):
        invoked = sum(int(counts.get("choice_invoked", {}).get(scope, {}).get(bucket, 0))
                      for bucket in range(last_complete - 11, last_complete + 1))
        selected = sum(int(counts.get("selected", {}).get(scope, {}).get(bucket, 0))
                       for bucket in range(last_complete - 11, last_complete + 1))
        if scope in active and selected:
            active.remove(scope)
            events.append(("recovery", scope, "selection_observed"))
        elif scope not in active and invoked >= 20 and selected == 0:
            active.add(scope)
            events.append(("alert", scope, "many_choices_zero_selection"))
    return events, sorted(active)


def _merge_meme_counts(previous, observed, last_complete):
    merged = defaultdict(lambda: defaultdict(dict))
    floor = last_complete - 11
    for source in (previous or {}, observed or {}):
        for stage, scopes in source.items():
            if stage not in {"choice_invoked", "selected"} or not isinstance(scopes, dict):
                continue
            for scope, buckets in scopes.items():
                if scope not in {"group", "private"} or not isinstance(buckets, dict):
                    continue
                for bucket, value in buckets.items():
                    try:
                        number = int(bucket)
                        count = int(value)
                    except (TypeError, ValueError):
                        continue
                    if floor <= number <= last_complete + 1 and count >= 0:
                        merged[stage][scope][number] = count
    return merged


def _run(args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f"log_command_failed:{args[0]}:{result.returncode}")
    return result.stdout + result.stderr


def _count(counts, stage, group, timestamp):
    counts[stage][group][int(timestamp // 300)] += 1


def _parse_docker_timestamp(value):
    # Docker emits nanoseconds; Python 3.10 accepts at most six fractional digits.
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", value)
    if not match:
        raise ValueError("invalid_docker_timestamp")
    fraction = ("." + match.group(2)[:6].ljust(6, "0")) if match.group(2) else ""
    normalized = match.group(1) + fraction + match.group(3).replace("Z", "+00:00")
    return dt.datetime.fromisoformat(normalized).timestamp()


def _journal_counts(counts, unit, stage, since, until, meme_counts=None):
    output = _run([
        "journalctl", "-u", unit, "--since", f"@{since}", "--until", f"@{until}",
        "--no-pager", "-o", "json",
    ])
    for line in output.splitlines():
        try:
            item = json.loads(line)
            message = item.get("MESSAGE", "")
            timestamp = int(item["__REALTIME_TIMESTAMP"]) / 1_000_000
        except (ValueError, KeyError, TypeError):
            continue
        if stage == "llbot":
            if "[收-群]" not in message:
                continue
            match = GROUP.search(message.split("[收-群]", 1)[1])
            if match:
                _count(counts, stage, match.group(1), timestamp)
        else:
            match = STAGE.search(message)
            if match and match.group(1) == "bridge":
                _count(counts, stage, match.group(2), timestamp)
            voice = VOICE_STAGE.search(message)
            if voice:
                phase, group, status = voice.groups()
                if phase == "candidate_selected" and status == "ok":
                    _count(counts, "candidate", group, timestamp)
                if phase == "model_text" and status == "ok":
                    _count(counts, "model_text", group, timestamp)
                if phase == "model_text" and status == "failed":
                    _count(counts, "model_failed", group, timestamp)
                if status == "sqlite_busy":
                    _count(counts, "sqlite_busy", group, timestamp)
                if status == "required_source_unavailable":
                    _count(counts, "required_source_unavailable", group, timestamp)
            handoff = VOICE_HANDOFF.search(message)
            if handoff:
                _count(counts, "handoff_" + handoff.group(2), handoff.group(1), timestamp)
            if meme_counts is not None and "assistant_meme_funnel " in message:
                try:
                    event = json.loads(message.split("assistant_meme_funnel ", 1)[1])
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict) and event.get("decision_source") == "expression_model" and event.get("scope") in {"group", "private"}:
                    phase = str(event.get("stage") or "")
                    if phase in {"choice_invoked", "selected"}:
                        meme_counts[phase][event["scope"]][int(timestamp // 300)] += 1


def _astrbot_counts(counts, since, until):
    start = dt.datetime.fromtimestamp(since, dt.timezone.utc).isoformat()
    end = dt.datetime.fromtimestamp(until, dt.timezone.utc).isoformat()
    output = _run(["docker", "logs", "--timestamps", "--since", start, "--until", end, "astrbot"])
    for line in output.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            timestamp = _parse_docker_timestamp(parts[0])
        except ValueError:
            continue
        match = STAGE.search(parts[1])
        if match:
            stage = "wait_overrun" if match.group(1) == "rate_wait_overrun" else match.group(1)
            _count(counts, stage, match.group(2), timestamp)
        else:
            timeout = BRIDGE_WAIT_TIMEOUT.search(parts[1])
            if timeout:
                _count(counts, "bridge_wait_timeout", timeout.group(1), timestamp)


def main():
    now = int(time.time())
    last_complete = now // 300 - 1
    since = (last_complete - 6) * 300
    counts = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    observed_meme = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    _journal_counts(counts, "llbot", "llbot", since, now)
    _journal_counts(counts, "codex-qq-bridge", "bridge", since, now, observed_meme)
    _astrbot_counts(counts, since, now)
    with sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=2) as connection:
        enabled = {str(row[0]) for row in connection.execute("SELECT group_id FROM group_policies WHERE enabled=1")}
        _database_voice_counts(connection, counts, since)
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"active": []}
    events, next_state = evaluate(counts, enabled, state, last_complete)
    counts["inbound"] = counts["bridge"]
    voice_events, voice_state = evaluate_voice_funnel(counts, enabled, state, last_complete)
    next_state["voice_active"] = voice_state.get("voice_active", {})
    meme_counts = _merge_meme_counts(state.get("meme_counts"), observed_meme, last_complete)
    meme_events, meme_active = evaluate_meme_expression(meme_counts, state, last_complete)
    next_state["meme_expression_active"] = meme_active
    next_state["meme_counts"] = {
        stage: {scope: {str(bucket): value for bucket, value in buckets.items()}
                for scope, buckets in scopes.items()}
        for stage, scopes in meme_counts.items()
    }
    next_state["counts"] = {
        stage: {group: {str(bucket): value for bucket, value in buckets.items()} for group, buckets in groups.items()}
        for stage, groups in counts.items()
    }
    for kind, group, reason in events:
        notice = json.dumps({"event": f"group_ingress_{kind}", "group_id": group, "reason": reason}, separators=(",", ":"))
        try:
            import syslog
            syslog.openlog("group-ingress-monitor")
            syslog.syslog(syslog.LOG_WARNING if kind == "alert" else syslog.LOG_NOTICE, notice)
        except (ImportError, OSError):
            print(notice, flush=True)
    for kind, scope, reason in meme_events:
        notice = json.dumps({"event": f"meme_expression_{kind}", "scope": scope, "reason": reason}, separators=(",", ":"))
        try:
            import syslog
            syslog.openlog("group-ingress-monitor")
            syslog.syslog(syslog.LOG_WARNING if kind == "alert" else syslog.LOG_NOTICE, notice)
        except (ImportError, OSError):
            print(notice, flush=True)
    for kind, group, reason in voice_events:
        notice = json.dumps({"event": f"group_voice_{kind}", "group_id": group, "reason": reason}, separators=(",", ":"))
        try:
            import syslog
            syslog.openlog("group-ingress-monitor")
            syslog.syslog(syslog.LOG_WARNING if kind == "alert" else syslog.LOG_NOTICE, notice)
        except (ImportError, OSError):
            print(notice, flush=True)
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(next_state, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, STATE)


if __name__ == "__main__":
    main()
