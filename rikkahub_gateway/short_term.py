"""Bounded, volatile handovers between reliably identified chat sources.

The caller, not this module, authenticates ``Scope.owner`` and establishes a
stable ``Source``. Unknown source must be passed as None. Only the final body of
a successfully completed assistant response may be passed to ``complete``.
There is deliberately no persistence, logging, text summarization, source
inference, or automatic promotion into long-term memory here.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable


def _identifier(value: object, name: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{name} must be a nonempty bounded string")
    if value != value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name} must not contain surrounding whitespace or controls")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must be valid Unicode") from error
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _bounded_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class Scope:
    """An authenticated owner identifier, never a bearer token, and model."""

    owner: str
    model: str

    def __post_init__(self) -> None:
        _identifier(self.owner, "owner")
        _identifier(self.model, "model")


@dataclass(frozen=True)
class Source:
    """Caller-verified stable frontend and conversation identifiers."""

    frontend: str
    conversation: str

    def __post_init__(self) -> None:
        _identifier(self.frontend, "frontend")
        _identifier(self.conversation, "conversation")


@dataclass(frozen=True)
class RequestTicket:
    scope: Scope
    source: Source
    generation: int
    request_seq: int
    issued_at: float
    _cache_id: str = field(repr=False)
    _nonce: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scope, Scope) or not isinstance(self.source, Source):
            raise ValueError("ticket requires validated Scope and Source")
        for name in ("generation", "request_seq"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("ticket counters must be positive integers")
        if (isinstance(self.issued_at, bool) or not isinstance(self.issued_at, (int, float))
                or not math.isfinite(self.issued_at)):
            raise ValueError("ticket time must be finite")
        for value in (self._cache_id, self._nonce):
            if (not isinstance(value, str) or len(value) != 32
                    or any(char not in "0123456789abcdef" for char in value)):
                raise ValueError("ticket identity must be a valid opaque identifier")


@dataclass(frozen=True)
class HandoverMessage:
    message_id: str
    text: str = field(repr=False)
    age_seconds: float

    def __post_init__(self) -> None:
        _identifier(self.message_id, "message_id")
        if not isinstance(self.text, str):
            raise ValueError("handover body must be a string")
        if (isinstance(self.age_seconds, bool) or not isinstance(self.age_seconds, (int, float))
                or not math.isfinite(self.age_seconds) or self.age_seconds < 0):
            raise ValueError("handover age must be finite and nonnegative")


@dataclass(frozen=True)
class Handover:
    from_source: Source
    messages: tuple[HandoverMessage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.from_source, Source):
            raise ValueError("handover requires a validated Source")
        if (not isinstance(self.messages, tuple) or not 1 <= len(self.messages) <= 3
                or any(not isinstance(item, HandoverMessage) for item in self.messages)):
            raise ValueError("handover requires one to three immutable message snapshots")


@dataclass(frozen=True)
class SourceTransition:
    """Request-source observation, independent of whether a reply is available.

    Unknown never means unchanged. This describes client-declared routing, not
    UI focus, and lasts only as long as the existing bounded volatile lineage.
    """

    state: str = "unknown"
    reason: str = "baseline_unavailable"
    frontend_changed: bool | None = None
    conversation_changed: bool | None = None


@dataclass(frozen=True)
class BeginResult:
    ticket: RequestTicket | None
    handover: Handover | None
    transition: SourceTransition = field(default_factory=SourceTransition)


@dataclass(frozen=True)
class CompletionResult:
    stored: bool
    reason: str


@dataclass(frozen=True)
class CacheStats:
    scope_count: int
    message_count: int
    byte_count: int
    closed: bool


@dataclass(frozen=True)
class _StoredMessage:
    message_id: str
    text: str = field(repr=False)
    completed_at: float
    expires_at: float
    byte_count: int


@dataclass
class _ScopeState:
    source: Source
    generation: int
    expires_at: float
    messages: deque[_StoredMessage] = field(default_factory=deque, repr=False)
    seen_message_ids: dict[str, float] = field(default_factory=dict, repr=False)
    latest_ticket: RequestTicket | None = field(default=None, repr=False)
    latest_completed: bool = False


class ShortTermMemory:
    """Thread-safe last-three-replies cache with one-shot source handovers.

    Reads and handovers never refresh a message's expiry. A switch clears the
    previous source's reply buffer after returning its immutable snapshot. A
    completion from an older source generation, or any request superseded by a
    newer begin in the same scope, is rejected. This intentionally prefers a
    missed reply over allowing concurrent stale output to enter a new source.

    The default has no thread. Call ``prune`` from gateway maintenance, or opt
    into a stoppable daemon with ``cleanup_interval_seconds``. ``close`` stops
    the daemon, clears all data, and permanently disables this instance.
    """

    _MAX_SEEN_MESSAGE_IDS = 1024

    def __init__(
        self,
        *,
        ttl_seconds: float = 1800,
        max_messages: int = 3,
        max_message_bytes: int = 128 * 1024,
        max_scopes: int = 128,
        clock: Callable[[], float] = time.monotonic,
        cleanup_interval_seconds: float | None = None,
    ) -> None:
        self.ttl_seconds = _positive_number(ttl_seconds, "ttl_seconds")
        if self.ttl_seconds > 1800:
            raise ValueError("ttl_seconds must not exceed 1800")
        self.max_messages = _bounded_int(max_messages, "max_messages", 3)
        self.max_message_bytes = _bounded_int(max_message_bytes, "max_message_bytes", 128 * 1024)
        self.max_scopes = _bounded_int(max_scopes, "max_scopes", 128)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock
        self._last_clock_value: float | None = None
        self._lock = threading.RLock()
        self._states: dict[Scope, _ScopeState] = {}
        self._closed = False
        self._sequence = 0
        self._generation = 0
        self._cache_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        if cleanup_interval_seconds is not None:
            interval = _positive_number(cleanup_interval_seconds, "cleanup_interval_seconds")
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                args=(interval,),
                name="st-short-term-pruner",
                daemon=True,
            )
            self._cleanup_thread.start()

    def _now(self) -> float:
        raw = self._clock()
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("clock must return a finite monotonic number")
        now = float(raw)
        if not math.isfinite(now):
            raise ValueError("clock must return a finite monotonic number")
        if self._last_clock_value is not None and now < self._last_clock_value:
            raise ValueError("clock moved backwards")
        self._last_clock_value = now
        return now

    def _prune_locked(self, now: float) -> int:
        removed = 0
        for scope, state in tuple(self._states.items()):
            # Body expiry is independent from source/ticket metadata. A fresh
            # same-source request may still be running after the preceding
            # body's TTL, but must never keep that preceding body alive.
            retained = deque(message for message in state.messages if message.expires_at > now)
            removed += len(state.messages) - len(retained)
            state.messages = retained
            state.seen_message_ids = {
                message_id: expiry for message_id, expiry in state.seen_message_ids.items()
                if expiry > now
            }
            if self._state_deadline(state) <= now:
                del self._states[scope]
        return removed

    def _state_deadline(self, state: _ScopeState) -> float:
        """Metadata may outlive bodies only for the latest live request ticket."""
        deadline = state.expires_at
        if state.latest_ticket is not None and not state.latest_completed:
            deadline = max(deadline, state.latest_ticket.issued_at + self.ttl_seconds)
        return deadline

    def prune(self) -> int:
        """Remove expired data and return the number of expired reply bodies."""
        with self._lock:
            if self._closed:
                return 0
            return self._prune_locked(self._now())

    def clear(self) -> None:
        """Break all lineage when the host cannot establish a current identity."""
        with self._lock:
            self._states.clear()

    def discard(self, scope: Scope) -> None:
        """Forget one authenticated scope on disabled policy/unknown lineage."""
        if not isinstance(scope, Scope):
            raise TypeError("scope must be Scope")
        with self._lock:
            self._states.pop(scope, None)

    def begin(self, scope: Scope, source: Source | None) -> BeginResult:
        """Start a request; None source is a no-op, never inferred or cached."""
        if not isinstance(scope, Scope):
            raise TypeError("scope must be Scope")
        if source is not None and not isinstance(source, Source):
            raise TypeError("source must be Source or None")
        with self._lock:
            if self._closed:
                return BeginResult(None, None, SourceTransition(reason="cache_closed"))
            now = self._now()
            self._prune_locked(now)
            if source is None:
                return BeginResult(None, None, SourceTransition(reason="missing_reliable_source"))
            state = self._states.get(scope)
            handover = None
            transition = SourceTransition()
            if state is None:
                if len(self._states) >= self.max_scopes:
                    # Reads do not change priority. Lose the earliest-expiring
                    # scope whole rather than exceeding the memory bound.
                    victim = min(self._states, key=lambda item: self._state_deadline(self._states[item]))
                    del self._states[victim]
                self._generation += 1
                state = _ScopeState(source, self._generation, now + self.ttl_seconds)
                self._states[scope] = state
            elif source != state.source:
                transition = SourceTransition(
                    "changed", "reliable_source_changed",
                    (source.frontend != state.source.frontend
                     if "unlabelled-client" not in {source.frontend, state.source.frontend} else None),
                    source.conversation != state.source.conversation,
                )
                if state.messages:
                    handover = Handover(
                        from_source=state.source,
                        messages=tuple(
                            HandoverMessage(message.message_id, message.text,
                                            max(0.0, now - message.completed_at))
                            for message in state.messages
                        ),
                    )
                self._generation += 1
                state.source = source
                state.generation = self._generation
                state.messages.clear()
                # No old reply body survives the switch. Fresh source metadata
                # has its own deadline, without extending any reply or ID TTL.
                state.expires_at = now + self.ttl_seconds
            else:
                transition = SourceTransition("same", "same_reliable_source", False
                    if source.frontend != "unlabelled-client" else None, False)
            self._sequence += 1
            ticket = RequestTicket(scope, source, state.generation, self._sequence,
                                   now, self._cache_id, uuid.uuid4().hex)
            state.latest_ticket = ticket
            state.latest_completed = False
            return BeginResult(ticket, handover, transition)

    def complete(self, ticket: RequestTicket | None, message_id: str, text: str) -> CompletionResult:
        """Commit one successful final assistant body, unchanged, or skip it.

        Failure paths must not call this method with partial/model-error text.
        A valid ticket can complete at most once. Content validation failures
        consume that completion attempt, without creating a body or ID record.
        """
        if ticket is None:
            return CompletionResult(False, "disabled")
        if not isinstance(ticket, RequestTicket) or ticket._cache_id != self._cache_id:
            return CompletionResult(False, "invalid_ticket")
        try:
            _identifier(message_id, "message_id")
        except ValueError:
            return CompletionResult(False, "invalid_message_id")
        with self._lock:
            if self._closed:
                return CompletionResult(False, "closed")
            now = self._now()
            self._prune_locked(now)
            state = self._states.get(ticket.scope)
            if state is None or now - ticket.issued_at >= self.ttl_seconds:
                return CompletionResult(False, "expired")
            if ticket.generation != state.generation or ticket.source != state.source:
                return CompletionResult(False, "stale_source")
            if state.latest_ticket != ticket:
                return CompletionResult(False, "stale_request")
            if message_id in state.seen_message_ids:
                state.latest_completed = True
                return CompletionResult(False, "duplicate")
            if state.latest_completed:
                return CompletionResult(False, "already_completed")
            state.latest_completed = True
            if not isinstance(text, str):
                return CompletionResult(False, "invalid_text")
            # Every UTF-8 character occupies at least one byte; this rejects an
            # oversized input before allocating its encoded representation.
            if len(text) > self.max_message_bytes:
                return CompletionResult(False, "too_large")
            if not text.strip():
                return CompletionResult(False, "empty")
            try:
                size = len(text.encode("utf-8"))
            except UnicodeEncodeError:
                return CompletionResult(False, "invalid_text")
            if size > self.max_message_bytes:
                return CompletionResult(False, "too_large")
            if len(state.seen_message_ids) >= self._MAX_SEEN_MESSAGE_IDS:
                # Never evict live deduplication records just to accept another
                # body: reject new writes until their original TTLs expire.
                return CompletionResult(False, "dedupe_capacity")
            expiry = now + self.ttl_seconds
            state.messages.append(_StoredMessage(message_id, text, now, expiry, size))
            while len(state.messages) > self.max_messages:
                state.messages.popleft()
            state.seen_message_ids[message_id] = expiry
            state.expires_at = expiry
            return CompletionResult(True, "stored")

    def stats(self) -> CacheStats:
        """Body-free diagnostic counts; reading them does not renew retention."""
        with self._lock:
            if not self._closed:
                self._prune_locked(self._now())
            messages = tuple(message for state in self._states.values() for message in state.messages)
            return CacheStats(len(self._states), len(messages),
                              sum(message.byte_count for message in messages), self._closed)

    def _cleanup_loop(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                self.prune()
            except Exception:
                # A broken clock/maintenance hook must fail closed, without
                # printing potentially identifying object data or retaining it.
                self.close()
                return

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._closed = True
            self._states.clear()
        worker = self._cleanup_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    def __enter__(self) -> ShortTermMemory:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
