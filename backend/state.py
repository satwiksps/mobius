import asyncio
import time
import sys
import json

pause_event = asyncio.Event()
pause_event.set()  # starts unpaused

_node_statuses: dict[str, str] = {}
_node_outputs: dict[str, object] = {}
_node_logs: dict[str, list] = {}
_sse_queues: list[asyncio.Queue] = []


def _send_event(event_type: str, data: dict):
    try:
        msg = json.dumps({"event": event_type, **data})
        sys.__stdout__.write(msg + "\n")
        sys.__stdout__.flush()
    except Exception:
        pass


def report_node(node_id: str, status: str) -> None:
    _node_statuses[node_id] = status
    _send_event("node_status", {"node_id": node_id, "status": status})
    for q in _sse_queues:
        q.put_nowait({"node_id": node_id, "status": status})


def report_node_output(node_id: str, output: object) -> None:
    _node_outputs[node_id] = output
    _send_event("node_output", {"node_id": node_id, "output": output})
    for q in _sse_queues:
        q.put_nowait({"type": "node_output", "node_id": node_id, "output": output})


def report_node_log(node_id: str, line: str) -> None:
    entry = {"t": round(time.time() * 1000), "msg": str(line)[:600]}
    if node_id not in _node_logs:
        _node_logs[node_id] = []
    _node_logs[node_id].append(entry)
    if len(_node_logs[node_id]) > 100:
        _node_logs[node_id] = _node_logs[node_id][-100:]
    _send_event("node_log", {"node_id": node_id, "entry": entry})
    for q in _sse_queues:
        q.put_nowait({"type": "node_log", "node_id": node_id, "entry": entry})


def reset_execution() -> None:
    _node_statuses.clear()
    _node_outputs.clear()
    _node_logs.clear()
    _send_event("reset", {})
    for q in _sse_queues:
        q.put_nowait({"type": "reset"})


def get_node_statuses() -> dict:
    return dict(_node_statuses)


def get_node_outputs() -> dict:
    return dict(_node_outputs)


def get_node_logs() -> dict:
    return {k: list(v) for k, v in _node_logs.items()}

