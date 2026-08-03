"""Structured agent trace - one event per pipeline step, for a live UI panel.

The visible "agentic" behaviour of the demo is watching the agent think: for
each step (extract -> duplicate check -> PO lookup -> GR sum -> match ->
propose/email) the pipeline emits a structured event and the frontend polls
for new ones and renders them as they arrive.

Deliberately small (the brief says don't over-build this):
* an in-memory store keyed by run_id, each run an append-only list of events;
* every event carries a monotonically increasing `seq`, so the UI polls with
  "give me everything after seq N" and never re-renders or misses one;
* thread-safe (a lock) because the FastAPI pipeline endpoint and the polling
  endpoint touch it from different request threads;
* bounded (a capped number of runs retained) so a long-lived server process
  doesn't grow without limit - it's a demo trace, not an audit log.

No persistence: a trace describes one in-flight run. The durable records are
the ledgers (posted invoices, sent emails), not this.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field

# How many recent runs to keep before evicting the oldest. A trace is only
# interesting while its run is on screen; this is generous for a demo.
_MAX_RUNS = 50


@dataclass
class TraceEvent:
    seq: int
    step: str                 # short machine name, e.g. "extract", "match"
    label: str                # human sentence for the UI
    status: str               # "running" | "ok" | "warn" | "error"
    tool: str = ""            # tool/function invoked for this step, if any
    data: dict = field(default_factory=dict)  # small, JSON-safe detail payload
    ts: float = field(default_factory=time.time)


class _Run:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.events: list[TraceEvent] = []
        self.created_at = time.time()
        self.done = False


class TraceStore:
    def __init__(self, max_runs: int = _MAX_RUNS):
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._lock = threading.Lock()
        self._max_runs = max_runs

    def start_run(self, run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        with self._lock:
            self._runs[run_id] = _Run(run_id)
            self._runs.move_to_end(run_id)
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)  # evict oldest
        return run_id

    def emit(
        self,
        run_id: str,
        step: str,
        label: str,
        status: str = "ok",
        tool: str = "",
        data: dict | None = None,
    ) -> TraceEvent:
        """Append one event to a run. Unknown run_id auto-creates the run so a
        stray emit never raises inside the pipeline."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                run = _Run(run_id)
                self._runs[run_id] = run
            event = TraceEvent(
                seq=len(run.events),
                step=step,
                label=label,
                status=status,
                tool=tool,
                data=_safe(data or {}),
            )
            run.events.append(event)
            return event

    def finish_run(self, run_id: str) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run:
                run.done = True

    def events_after(self, run_id: str, after_seq: int = -1) -> dict:
        """All events with seq > after_seq. Returns {ok, run_id, events, done,
        next_seq} - the UI passes next_seq back as after_seq to poll forward."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return {"ok": False, "error": f"Unknown run_id '{run_id}'."}
            new = [asdict(e) for e in run.events if e.seq > after_seq]
            return {
                "ok": True,
                "run_id": run_id,
                "events": new,
                "done": run.done,
                "next_seq": run.events[-1].seq if run.events else after_seq,
            }


def _safe(data: dict) -> dict:
    """Best-effort JSON-safe shrink of a detail payload: drop nothing the UI
    needs, but never let a huge blob or an unserialisable object into a trace
    event (it would break the polling endpoint's JSON response)."""
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, (list, dict)):
            out[key] = value  # assumed already small + JSON-safe by callers
        else:
            out[key] = str(value)
    return out


# One process-wide store; both the pipeline and the polling endpoint import it.
trace_store = TraceStore()
