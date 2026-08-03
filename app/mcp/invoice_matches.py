"""Completed-match store - the seam between the two steps of the invoice flow.

Matching an invoice and posting it are two separate user decisions, so they are
two separate server calls. Step 1 (`run_invoice_pipeline`) ends at a finished
3-way match and deposits it here; step 2 (`propose_posting_for_match`) picks it
up by id and only THEN runs the posting pre-flight. A user who only wanted to
know "does this invoice match?" never sees a posting blocker at all.

WHY A STORE, AND NOT JUST HANDING THE MATCH BACK TO THE CLIENT
The match record is what the posting payload is built from - quantity, unit
price, PO item, supplier, the pre-flight prerequisites. If step 2 accepted that
record as a request body, anyone could post any figures they liked without a
match ever having run, and the server's "every figure is server-computed"
promise would be worth nothing. So the client gets an opaque `match_id` and the
record stays here, exactly the way `proposal_id` keeps an invented payload out
of the commit step (invoice_proposals.py).

The id is minted here rather than reusing the client-supplied `run_id` for the
same reason: a run_id arrives from the browser and is therefore guessable by
whoever sends one.

Entries are in-memory and expire on the same TTL as a proposal; a restart
invalidates them, which is safe - the worst case is re-running a read-only
match. `proposal_id` is written back onto the entry once step 2 has proposed,
so a second click cannot mint a second live proposal for the same match (two
proposals means two commit ids, and the commit ledger's idempotency key is
(payload_hash, proposal_id) - it would not dedup them).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class MatchEntry:
    match_id: str
    run_id: str
    record: dict                       # the matcher's record, verbatim
    values: dict                       # extracted field values (for the review queue)
    extracted: dict                    # extracted fields + confidences (for the panel)
    created_at: float = field(default_factory=time.monotonic)
    # Set once step 2 has built a proposal from this match. Not a second store:
    # it is the link that keeps one match to at most one live proposal.
    proposal_id: str = ""


class MatchStore:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._entries: dict[str, MatchEntry] = {}

    def put(self, *, run_id: str, record: dict, values: dict, extracted: dict) -> MatchEntry:
        self._prune()
        entry = MatchEntry(
            match_id=uuid.uuid4().hex,
            run_id=run_id,
            record=record,
            values=values,
            extracted=extracted,
        )
        self._entries[entry.match_id] = entry
        return entry

    def get(self, match_id: str) -> tuple[MatchEntry | None, str]:
        """Fetch without consuming - matching is read-only, so re-reading a
        match is harmless. Returns (entry, "") or (None, reason)."""
        entry = self._entries.get(match_id)
        if entry is None:
            return None, (
                "That match is no longer available (unknown id, expired, or the "
                "server restarted since it ran). Re-run the match on the invoice "
                "to post it."
            )
        if time.monotonic() - entry.created_at > self._ttl:
            del self._entries[match_id]
            return None, (
                "This match has expired. Re-run it on the invoice before posting, "
                "so the figures are checked against the PO as it stands now."
            )
        return entry, ""

    def attach_proposal(self, match_id: str, proposal_id: str) -> None:
        entry = self._entries.get(match_id)
        if entry:
            entry.proposal_id = proposal_id

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, e in self._entries.items() if now - e.created_at > self._ttl]:
            del self._entries[key]
