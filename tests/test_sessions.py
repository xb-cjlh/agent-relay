import json
from datetime import datetime, timezone
from pathlib import Path



def codex_records(cwd, sid="native-codex-1", busy=False):
    now = datetime.now(timezone.utc).isoformat()
    events = [
        {"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd)}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "response_item", "timestamp": now, "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "terminal question"}]}},
        {"type": "response_item", "timestamp": now, "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "terminal answer"}]}},
    ]
    if not busy:
        events.append({"type": "event_msg", "payload": {"type": "task_complete"}})
    return events


def write_records(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
