"""Persistent anti-duplicate and dispatch-limit guard for Tourvisor searches.

The guard deliberately separates a local claim from an actual upstream dispatch:

1. ``claim`` reserves a normalized search while local/preflight checks run.
2. ``mark_dispatched`` atomically consumes one of the two allowed dispatches and
   must be called immediately before ``/tours/search`` is sent to Tourvisor.
3. ``mark_succeeded`` or ``mark_failed`` closes the attempt. A claim that never
   reached Tourvisor must instead be released with ``abandon_claim``.

Only HMAC digests and operational state are persisted. Raw chat identifiers,
request data, prices, and result payloads are never written to SQLite. A
successful response may be retained in process memory for at most 60 seconds
solely to absorb delivery retries.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


WINDOW_TTL_SECONDS = 72 * 60 * 60
TECHNICAL_REPLAY_TTL_SECONDS = 60
MAX_DISPATCHES_PER_WINDOW = 2
STALE_ATTEMPT_SECONDS = 5 * 60
SCHEMA_VERSION = 1


class SearchGuardError(RuntimeError):
    """Base class for errors that must fail a search closed."""


class SearchGuardConfigurationError(SearchGuardError, ValueError):
    """The guard cannot safely start with its current configuration."""


class SearchGuardUnavailable(SearchGuardError):
    """SQLite is unavailable or its schema is not usable."""


class SearchGuardStateError(SearchGuardError):
    """An invalid or unsafe attempt state transition was requested."""


class SearchDispatchLimitReached(SearchGuardError):
    """The two-dispatch window was exhausted between claim and dispatch."""

    def __init__(self, *, window_expires_at: float | None) -> None:
        super().__init__("search dispatch limit reached")
        self.window_expires_at = window_expires_at


class ClaimAction(str, Enum):
    """Decision returned by :meth:`SearchGuard.claim`."""

    CLAIMED = "claimed"
    IN_FLIGHT = "in_flight"
    REPLAY = "replay"
    DUPLICATE = "duplicate"
    LIMIT_REACHED = "limit_reached"


class AttemptState(str, Enum):
    CLAIMED = "claimed"
    DISPATCHED = "dispatched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class SearchClaim:
    """Safe decision object; every identifier in it is opaque or HMAC-derived."""

    action: ClaimAction
    chat_key: str
    search_fingerprint: str
    delivery_fingerprint: str
    attempt_id: str | None
    dispatch_count: int
    remaining_dispatches: int
    window_started_at: float | None
    window_expires_at: float | None
    prior_state: AttemptState | None = None
    replay_payload: Any | None = None
    delivery_matches: bool = True

    @property
    def should_run_preflight(self) -> bool:
        """Whether the caller owns a claim and may continue toward Tourvisor."""
        return self.action is ClaimAction.CLAIMED

    @property
    def has_replay(self) -> bool:
        return self.action is ClaimAction.REPLAY and self.replay_payload is not None


@dataclasses.dataclass(frozen=True, slots=True)
class DispatchPermit:
    """Proof that an upstream dispatch atomically consumed a quota slot."""

    attempt_id: str
    dispatch_number: int
    remaining_dispatches: int
    window_started_at: float
    window_expires_at: float


# Only fields that can change the Tourvisor query or server-side ranking belong
# here. Free-text wishes and response formatting intentionally do not: changing
# either must not silently authorize another paid/fresh Tourvisor search.
_SEARCH_FIELDS = (
    "departure_city",
    "country",
    "resort",
    "date_from",
    "date_to",
    "nights_from",
    "nights_to",
    "adults",
    "children",
    "children_ages",
    "meal",
    "hotel_stars",
    "currency",
)

_CASE_INSENSITIVE_SEARCH_FIELDS = {
    "departure_city",
    "country",
    "resort",
    "meal",
    "currency",
}

_DELIVERY_FIELDS = (
    "image_mode",
    "hotel_preferences",
    "beach_preferences",
    "locale",
)

# Known secret/PII-bearing fields are removed recursively before a short-lived
# replay is persisted. This is defense in depth; replay responses should already
# be constructed from the public BotResponse contract.
_SENSITIVE_REPLAY_KEYS = {
    "auth",
    "authorization",
    "auth_token",
    "token",
    "jwt",
    "password",
    "secret",
    "chat_id",
    "client_name",
    "client_phone",
    "phone",
    "email",
    "hotel_preferences",
    "beach_preferences",
    "unverified_preferences",
}

_SAFE_FAILURE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class SearchGuard:
    """SQLite-backed, process/thread-safe search dispatch guard.

    A separate SQLite connection is used for every operation and quota changes
    use ``BEGIN IMMEDIATE`` transactions. The asynchronous methods delegate the
    short blocking operation to a worker thread, making them suitable for async
    FastAPI endpoints.

    ``namespace`` is trusted server configuration used to isolate tenants. It
    must never be taken from the LLM-controlled request ``source`` field.
    """

    def __init__(
        self,
        db_path: str | Path,
        hmac_secret: str | bytes,
        *,
        namespace: str = "suvvy-tourvisor",
        clock: Callable[[], float] | None = None,
        sqlite_timeout_seconds: float = 5.0,
        window_ttl_seconds: float = WINDOW_TTL_SECONDS,
        replay_ttl_seconds: float = TECHNICAL_REPLAY_TTL_SECONDS,
        max_dispatches: int = MAX_DISPATCHES_PER_WINDOW,
        stale_attempt_seconds: float = STALE_ATTEMPT_SECONDS,
    ) -> None:
        path_text = str(db_path).strip()
        if not path_text or path_text == ":memory:":
            raise SearchGuardConfigurationError(
                "SEARCH_GUARD_DB_PATH must point to a persistent SQLite file"
            )
        secret = hmac_secret.encode("utf-8") if isinstance(hmac_secret, str) else bytes(hmac_secret)
        if len(secret) < 16:
            raise SearchGuardConfigurationError(
                "SEARCH_GUARD_HMAC_SECRET must contain at least 16 bytes"
            )
        namespace = namespace.strip()
        if not namespace:
            raise SearchGuardConfigurationError("SEARCH_GUARD_NAMESPACE must not be empty")
        if sqlite_timeout_seconds <= 0:
            raise SearchGuardConfigurationError("sqlite timeout must be positive")
        if window_ttl_seconds != WINDOW_TTL_SECONDS:
            raise SearchGuardConfigurationError(
                "search guard window TTL must be exactly 72 hours"
            )
        if not 0 < replay_ttl_seconds <= TECHNICAL_REPLAY_TTL_SECONDS:
            raise SearchGuardConfigurationError(
                "technical replay TTL must be between 1 and 60 seconds"
            )
        if max_dispatches != MAX_DISPATCHES_PER_WINDOW:
            raise SearchGuardConfigurationError("search guard currently requires max_dispatches=2")
        if stale_attempt_seconds <= 0:
            raise SearchGuardConfigurationError("stale attempt timeout must be positive")

        self._db_path = Path(path_text)
        self._secret = secret
        self._namespace = namespace
        self._clock = clock or time.time
        self._sqlite_timeout_seconds = float(sqlite_timeout_seconds)
        self._window_ttl_seconds = float(window_ttl_seconds)
        self._replay_ttl_seconds = float(replay_ttl_seconds)
        self._max_dispatches = int(max_dispatches)
        self._stale_attempt_seconds = float(stale_attempt_seconds)
        self._initialization_lock = threading.Lock()
        self._replay_lock = threading.Lock()
        self._replay_cache: dict[str, tuple[float, str]] = {}
        self._health_lock = threading.Lock()
        self._prune_healthy = True
        self._initialize()

    # ---- Public fingerprint helpers -------------------------------------------------

    def chat_key(self, chat_id: str) -> str:
        """Return a non-reversible, namespace-bound key for a raw chat ID."""
        raw = str(chat_id).strip()
        if not raw:
            raise SearchGuardConfigurationError("chat_id is required for a real search")
        return self._digest("chat", raw.encode("utf-8"))

    def search_fingerprint(self, search: Any) -> str:
        """Fingerprint canonical query/ranking fields without storing their values."""
        canonical = self._canonical_search(search)
        return self._digest("search", self._canonical_json(canonical))

    def delivery_fingerprint(self, search: Any, delivery_key: Any | None = None) -> str:
        """Fingerprint response-shaping fields separately from the actual search."""
        search_fingerprint = self.search_fingerprint(search)
        if delivery_key is None:
            request_data = self._as_mapping(search)
            delivery: Any = {
                field: self._normalize_value(request_data.get(field), casefold=True)
                for field in _DELIVERY_FIELDS
            }
        else:
            delivery = self._normalize_value(delivery_key, casefold=False)
        canonical = {
            "search_fingerprint": search_fingerprint,
            "delivery": delivery,
        }
        return self._digest("delivery", self._canonical_json(canonical))

    # ---- Atomic state machine -------------------------------------------------------

    def claim(
        self,
        chat_id: str,
        search: Any,
        *,
        refresh_requested: bool = False,
        delivery_key: Any | None = None,
    ) -> SearchClaim:
        """Atomically classify and, when allowed, reserve a search.

        A claim does *not* consume quota. Call ``mark_dispatched`` immediately
        before the actual Tourvisor search call. ``refresh_requested`` is only
        authoritative when the bot sets it after an explicit user request.
        """
        now = float(self._clock())
        chat_key = self.chat_key(chat_id)
        search_fingerprint = self.search_fingerprint(search)
        delivery_fingerprint = self.delivery_fingerprint(search, delivery_key)

        try:
            with self._transaction() as connection:
                window = self._load_or_create_window(connection, chat_key, now)
                self._recover_stale_attempts(connection, chat_key, now)

                # Serialize unfinished work per chat. This prevents two different
                # concurrent requests from both reaching Tourvisor unexpectedly.
                unfinished = connection.execute(
                    """
                    SELECT attempt_id, search_fingerprint, delivery_fingerprint, state
                    FROM search_attempts
                    WHERE chat_key = ? AND state IN (?, ?)
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (chat_key, AttemptState.CLAIMED.value, AttemptState.DISPATCHED.value),
                ).fetchone()
                if unfinished is not None:
                    state = AttemptState(unfinished["state"])
                    return self._decision(
                        action=ClaimAction.IN_FLIGHT,
                        chat_key=chat_key,
                        search_fingerprint=search_fingerprint,
                        delivery_fingerprint=delivery_fingerprint,
                        attempt_id=unfinished["attempt_id"],
                        window=window,
                        prior_state=state,
                        delivery_matches=(
                            unfinished["search_fingerprint"] == search_fingerprint
                            and unfinished["delivery_fingerprint"] == delivery_fingerprint
                        ),
                    )

                latest = connection.execute(
                    """
                    SELECT attempt_id, delivery_fingerprint, state
                    FROM search_attempts
                    WHERE chat_key = ? AND search_fingerprint = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT 1
                    """,
                    (chat_key, search_fingerprint),
                ).fetchone()

                if latest is not None and not refresh_requested:
                    prior_state = AttemptState(latest["state"])
                    delivery_matches = latest["delivery_fingerprint"] == delivery_fingerprint
                    replay_payload = None
                    if prior_state is AttemptState.SUCCEEDED and delivery_matches:
                        replay_payload = self._get_replay_payload(
                            latest["attempt_id"],
                            now,
                        )
                    if replay_payload is not None:
                        return self._decision(
                            action=ClaimAction.REPLAY,
                            chat_key=chat_key,
                            search_fingerprint=search_fingerprint,
                            delivery_fingerprint=delivery_fingerprint,
                            attempt_id=latest["attempt_id"],
                            window=window,
                            prior_state=prior_state,
                            replay_payload=replay_payload,
                            delivery_matches=True,
                        )
                    return self._decision(
                        action=ClaimAction.DUPLICATE,
                        chat_key=chat_key,
                        search_fingerprint=search_fingerprint,
                        delivery_fingerprint=delivery_fingerprint,
                        attempt_id=latest["attempt_id"],
                        window=window,
                        prior_state=prior_state,
                        delivery_matches=delivery_matches,
                    )

                if int(window["dispatch_count"]) >= self._max_dispatches:
                    return self._decision(
                        action=ClaimAction.LIMIT_REACHED,
                        chat_key=chat_key,
                        search_fingerprint=search_fingerprint,
                        delivery_fingerprint=delivery_fingerprint,
                        attempt_id=None,
                        window=window,
                    )

                attempt_id = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO search_attempts (
                        attempt_id, chat_key, search_fingerprint,
                        delivery_fingerprint, state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        chat_key,
                        search_fingerprint,
                        delivery_fingerprint,
                        AttemptState.CLAIMED.value,
                        now,
                    ),
                )
                return self._decision(
                    action=ClaimAction.CLAIMED,
                    chat_key=chat_key,
                    search_fingerprint=search_fingerprint,
                    delivery_fingerprint=delivery_fingerprint,
                    attempt_id=attempt_id,
                    window=window,
                )
        except sqlite3.Error as exc:
            raise SearchGuardUnavailable("search guard claim failed") from exc

    def mark_dispatched(self, attempt_id: str) -> DispatchPermit:
        """Consume one slot atomically immediately before Tourvisor is called."""
        now = float(self._clock())
        attempt_id = self._validate_attempt_id(attempt_id)
        try:
            with self._transaction() as connection:
                attempt = connection.execute(
                    """
                    SELECT chat_key, state, created_at
                    FROM search_attempts
                    WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                ).fetchone()
                if attempt is None or attempt["state"] != AttemptState.CLAIMED.value:
                    raise SearchGuardStateError("only a claimed attempt may be dispatched")
                if float(attempt["created_at"]) <= now - self._stale_attempt_seconds:
                    connection.execute(
                        "DELETE FROM search_attempts WHERE attempt_id = ?",
                        (attempt_id,),
                    )
                    raise SearchGuardStateError("search claim lease expired before dispatch")

                window = self._load_or_create_window(
                    connection,
                    attempt["chat_key"],
                    now,
                    preserve_attempt_id=attempt_id,
                )
                dispatch_count = int(window["dispatch_count"])
                if dispatch_count >= self._max_dispatches:
                    expires_at = self._window_expires_at(window)
                    raise SearchDispatchLimitReached(window_expires_at=expires_at)

                window_started_at = window["window_started_at"]
                if window_started_at is None:
                    window_started_at = now
                next_count = dispatch_count + 1
                connection.execute(
                    """
                    UPDATE chat_windows
                    SET window_started_at = ?, dispatch_count = ?
                    WHERE chat_key = ?
                    """,
                    (window_started_at, next_count, attempt["chat_key"]),
                )
                updated = connection.execute(
                    """
                    UPDATE search_attempts
                    SET state = ?, dispatched_at = ?
                    WHERE attempt_id = ? AND state = ?
                    """,
                    (
                        AttemptState.DISPATCHED.value,
                        now,
                        attempt_id,
                        AttemptState.CLAIMED.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise SearchGuardStateError("dispatch claim was concurrently modified")

                return DispatchPermit(
                    attempt_id=attempt_id,
                    dispatch_number=next_count,
                    remaining_dispatches=self._max_dispatches - next_count,
                    window_started_at=float(window_started_at),
                    window_expires_at=float(window_started_at) + self._window_ttl_seconds,
                )
        except sqlite3.Error as exc:
            raise SearchGuardUnavailable("could not consume search dispatch quota") from exc

    def mark_succeeded(self, attempt_id: str, replay_payload: Any) -> None:
        """Close a dispatched attempt and retain a replay in memory for <=60 seconds."""
        now = float(self._clock())
        attempt_id = self._validate_attempt_id(attempt_id)
        sanitized = self._sanitize_replay_payload(replay_payload)
        try:
            encoded = json.dumps(
                sanitized,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise SearchGuardStateError("replay payload must be JSON serializable") from exc

        self._finish_attempt(
            attempt_id,
            state=AttemptState.SUCCEEDED,
            now=now,
            replay_until=None,
            replay_payload=None,
            failure_code=None,
        )
        with self._replay_lock:
            self._replay_cache[attempt_id] = (
                now + self._replay_ttl_seconds,
                encoded,
            )

    def mark_failed(self, attempt_id: str, failure_code: str) -> None:
        """Close a dispatched attempt without retaining raw exception details."""
        attempt_id = self._validate_attempt_id(attempt_id)
        failure_code = self._validate_failure_code(failure_code)
        self._finish_attempt(
            attempt_id,
            state=AttemptState.FAILED,
            now=float(self._clock()),
            replay_until=None,
            replay_payload=None,
            failure_code=failure_code,
            expected_state=AttemptState.DISPATCHED,
        )

    def mark_preflight_failed(self, attempt_id: str, failure_code: str) -> None:
        """Close a preflight failure without consuming an upstream dispatch slot.

        Unlike ``abandon_claim`` (used for a correctable clarification), this
        terminal state prevents an automatic retry of the same normalized query.
        """
        attempt_id = self._validate_attempt_id(attempt_id)
        failure_code = self._validate_failure_code(failure_code)
        self._finish_attempt(
            attempt_id,
            state=AttemptState.FAILED,
            now=float(self._clock()),
            replay_until=None,
            replay_payload=None,
            failure_code=failure_code,
            expected_state=AttemptState.CLAIMED,
        )

    def abandon_claim(self, attempt_id: str) -> None:
        """Release preflight/clarification work that never reached Tourvisor."""
        attempt_id = self._validate_attempt_id(attempt_id)
        try:
            with self._transaction() as connection:
                deleted = connection.execute(
                    """
                    DELETE FROM search_attempts
                    WHERE attempt_id = ? AND state = ?
                    """,
                    (attempt_id, AttemptState.CLAIMED.value),
                )
                if deleted.rowcount != 1:
                    raise SearchGuardStateError("only a claimed attempt may be abandoned")
        except sqlite3.Error as exc:
            raise SearchGuardUnavailable("could not abandon search claim") from exc

    # ---- Readiness, cleanup and async wrappers -------------------------------------

    def check_ready(self) -> None:
        """Raise fail-closed if the database/schema cannot be locked and read."""
        try:
            with self._transaction() as connection:
                version = connection.execute(
                    "SELECT value FROM search_guard_meta WHERE key = 'schema_version'"
                ).fetchone()
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if version is None or int(version["value"]) != SCHEMA_VERSION:
                    raise SearchGuardUnavailable("search guard schema version mismatch")
                if integrity is None or integrity[0] != "ok":
                    raise SearchGuardUnavailable("search guard integrity check failed")
        except sqlite3.Error as exc:
            raise SearchGuardUnavailable("search guard readiness check failed") from exc
        with self._health_lock:
            if not self._prune_healthy:
                raise SearchGuardUnavailable("search guard retention cleanup is unhealthy")

    def prune_expired(self) -> int:
        """Delete windows older than 72 hours and expired technical payloads."""
        now = float(self._clock())
        self._prune_replay_cache(now)
        cutoff = now - self._window_ttl_seconds
        try:
            with self._transaction() as connection:
                stale_cutoff = now - self._stale_attempt_seconds
                connection.execute(
                    """
                    DELETE FROM search_attempts
                    WHERE state = ? AND created_at <= ?
                    """,
                    (AttemptState.CLAIMED.value, stale_cutoff),
                )
                connection.execute(
                    """
                    UPDATE search_attempts
                    SET state = ?, completed_at = ?, failure_code = ?
                    WHERE state = ? AND dispatched_at IS NOT NULL
                      AND dispatched_at <= ?
                    """,
                    (
                        AttemptState.FAILED.value,
                        now,
                        "STALE_DISPATCH",
                        AttemptState.DISPATCHED.value,
                        stale_cutoff,
                    ),
                )
                connection.execute(
                    """
                    UPDATE search_attempts
                    SET replay_payload = NULL
                    WHERE replay_until IS NOT NULL AND replay_until <= ?
                    """,
                    (now,),
                )
                deleted = connection.execute(
                    """
                    DELETE FROM chat_windows
                    WHERE window_started_at IS NOT NULL AND window_started_at <= ?
                    """,
                    (cutoff,),
                )
                deleted_windows = max(0, deleted.rowcount)
                connection.execute(
                    """
                    DELETE FROM search_attempts
                    WHERE state = ? AND dispatched_at IS NULL
                      AND completed_at IS NOT NULL AND completed_at <= ?
                    """,
                    (AttemptState.FAILED.value, cutoff),
                )
                empty = connection.execute(
                    """
                    DELETE FROM chat_windows
                    WHERE window_started_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM search_attempts
                          WHERE search_attempts.chat_key = chat_windows.chat_key
                      )
                    """
                )
                deleted_count = deleted_windows + max(0, empty.rowcount)
        except sqlite3.Error as exc:
            with self._health_lock:
                self._prune_healthy = False
            raise SearchGuardUnavailable("search guard cleanup failed") from exc
        with self._health_lock:
            self._prune_healthy = True
        return deleted_count

    async def aclaim(self, *args: Any, **kwargs: Any) -> SearchClaim:
        return await asyncio.to_thread(self.claim, *args, **kwargs)

    async def amark_dispatched(self, attempt_id: str) -> DispatchPermit:
        return await asyncio.to_thread(self.mark_dispatched, attempt_id)

    async def amark_succeeded(self, attempt_id: str, replay_payload: Any) -> None:
        await asyncio.to_thread(self.mark_succeeded, attempt_id, replay_payload)

    async def amark_failed(self, attempt_id: str, failure_code: str) -> None:
        await asyncio.to_thread(self.mark_failed, attempt_id, failure_code)

    async def amark_preflight_failed(self, attempt_id: str, failure_code: str) -> None:
        await asyncio.to_thread(self.mark_preflight_failed, attempt_id, failure_code)

    async def aabandon_claim(self, attempt_id: str) -> None:
        await asyncio.to_thread(self.abandon_claim, attempt_id)

    async def acheck_ready(self) -> None:
        await asyncio.to_thread(self.check_ready)

    async def aprune_expired(self) -> int:
        return await asyncio.to_thread(self.prune_expired)

    # ---- Internals -----------------------------------------------------------------

    def _initialize(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SearchGuardUnavailable("could not create search guard directory") from exc

        with self._initialization_lock:
            try:
                with self._connection() as connection:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = FULL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS search_guard_meta (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS chat_windows (
                            chat_key TEXT PRIMARY KEY,
                            window_started_at REAL,
                            dispatch_count INTEGER NOT NULL DEFAULT 0
                                CHECK (dispatch_count >= 0 AND dispatch_count <= 2)
                        );

                        CREATE TABLE IF NOT EXISTS search_attempts (
                            attempt_id TEXT PRIMARY KEY,
                            chat_key TEXT NOT NULL,
                            search_fingerprint TEXT NOT NULL,
                            delivery_fingerprint TEXT NOT NULL,
                            state TEXT NOT NULL
                                CHECK (state IN ('claimed', 'dispatched', 'succeeded', 'failed')),
                            created_at REAL NOT NULL,
                            dispatched_at REAL,
                            completed_at REAL,
                            replay_until REAL,
                            replay_payload TEXT,
                            failure_code TEXT,
                            FOREIGN KEY (chat_key) REFERENCES chat_windows(chat_key)
                                ON DELETE CASCADE
                        );

                        CREATE INDEX IF NOT EXISTS idx_search_attempts_chat_search
                            ON search_attempts(chat_key, search_fingerprint, created_at DESC);

                        CREATE INDEX IF NOT EXISTS idx_search_attempts_chat_state
                            ON search_attempts(chat_key, state, created_at DESC);
                        """
                    )
                    row = connection.execute(
                        "SELECT value FROM search_guard_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    if row is None:
                        connection.execute(
                            "INSERT INTO search_guard_meta(key, value) VALUES ('schema_version', ?)",
                            (str(SCHEMA_VERSION),),
                        )
                    elif int(row["value"]) != SCHEMA_VERSION:
                        raise SearchGuardConfigurationError(
                            "unsupported search guard schema version"
                        )
            except sqlite3.Error as exc:
                raise SearchGuardUnavailable("could not initialize search guard database") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self._db_path),
            timeout=self._sqlite_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self._sqlite_timeout_seconds * 1000)}")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _load_or_create_window(
        self,
        connection: sqlite3.Connection,
        chat_key: str,
        now: float,
        *,
        preserve_attempt_id: str | None = None,
    ) -> sqlite3.Row:
        window = connection.execute(
            "SELECT chat_key, window_started_at, dispatch_count FROM chat_windows WHERE chat_key = ?",
            (chat_key,),
        ).fetchone()
        if window is not None and self._window_expired(window, now):
            if preserve_attempt_id is None:
                connection.execute(
                    "DELETE FROM search_attempts WHERE chat_key = ?",
                    (chat_key,),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM search_attempts
                    WHERE chat_key = ? AND attempt_id <> ?
                    """,
                    (chat_key, preserve_attempt_id),
                )
            connection.execute(
                """
                UPDATE chat_windows
                SET window_started_at = NULL, dispatch_count = 0
                WHERE chat_key = ?
                """,
                (chat_key,),
            )
            window = None
        if window is None:
            connection.execute(
                """
                INSERT INTO chat_windows(chat_key, window_started_at, dispatch_count)
                VALUES (?, NULL, 0)
                ON CONFLICT(chat_key) DO NOTHING
                """,
                (chat_key,),
            )
            window = connection.execute(
                "SELECT chat_key, window_started_at, dispatch_count FROM chat_windows WHERE chat_key = ?",
                (chat_key,),
            ).fetchone()
        if window is None:  # pragma: no cover - protects against storage corruption
            raise SearchGuardUnavailable("could not create search guard window")
        return window

    def _window_expired(self, window: sqlite3.Row, now: float) -> bool:
        started_at = window["window_started_at"]
        return started_at is not None and now >= float(started_at) + self._window_ttl_seconds

    def _recover_stale_attempts(
        self,
        connection: sqlite3.Connection,
        chat_key: str,
        now: float,
    ) -> None:
        """Release crashed preflight leases and terminalize uncertain dispatches."""
        stale_cutoff = now - self._stale_attempt_seconds
        connection.execute(
            """
            DELETE FROM search_attempts
            WHERE chat_key = ? AND state = ? AND created_at <= ?
            """,
            (chat_key, AttemptState.CLAIMED.value, stale_cutoff),
        )
        connection.execute(
            """
            UPDATE search_attempts
            SET state = ?, completed_at = ?, failure_code = ?
            WHERE chat_key = ? AND state = ?
              AND dispatched_at IS NOT NULL AND dispatched_at <= ?
            """,
            (
                AttemptState.FAILED.value,
                now,
                "STALE_DISPATCH",
                chat_key,
                AttemptState.DISPATCHED.value,
                stale_cutoff,
            ),
        )
        # A failed dictionary lookup before the first real search has no window
        # start. Retain its no-auto-retry record for the same 72-hour horizon,
        # then remove it so an empty chat window cannot live forever.
        connection.execute(
            """
            DELETE FROM search_attempts
            WHERE chat_key = ? AND state = ? AND dispatched_at IS NULL
              AND completed_at IS NOT NULL AND completed_at <= ?
            """,
            (chat_key, AttemptState.FAILED.value, now - self._window_ttl_seconds),
        )

    def _window_expires_at(self, window: sqlite3.Row) -> float | None:
        started_at = window["window_started_at"]
        if started_at is None:
            return None
        return float(started_at) + self._window_ttl_seconds

    def _decision(
        self,
        *,
        action: ClaimAction,
        chat_key: str,
        search_fingerprint: str,
        delivery_fingerprint: str,
        attempt_id: str | None,
        window: sqlite3.Row,
        prior_state: AttemptState | None = None,
        replay_payload: Any | None = None,
        delivery_matches: bool = True,
    ) -> SearchClaim:
        dispatch_count = int(window["dispatch_count"])
        return SearchClaim(
            action=action,
            chat_key=chat_key,
            search_fingerprint=search_fingerprint,
            delivery_fingerprint=delivery_fingerprint,
            attempt_id=attempt_id,
            dispatch_count=dispatch_count,
            remaining_dispatches=max(0, self._max_dispatches - dispatch_count),
            window_started_at=(
                float(window["window_started_at"])
                if window["window_started_at"] is not None
                else None
            ),
            window_expires_at=self._window_expires_at(window),
            prior_state=prior_state,
            replay_payload=replay_payload,
            delivery_matches=delivery_matches,
        )

    def _finish_attempt(
        self,
        attempt_id: str,
        *,
        state: AttemptState,
        now: float,
        replay_until: float | None,
        replay_payload: str | None,
        failure_code: str | None,
        expected_state: AttemptState = AttemptState.DISPATCHED,
    ) -> None:
        try:
            with self._transaction() as connection:
                updated = connection.execute(
                    """
                    UPDATE search_attempts
                    SET state = ?, completed_at = ?, replay_until = ?,
                        replay_payload = ?, failure_code = ?
                    WHERE attempt_id = ? AND state = ?
                    """,
                    (
                        state.value,
                        now,
                        replay_until,
                        replay_payload,
                        failure_code,
                        attempt_id,
                        expected_state.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise SearchGuardStateError(
                        f"only a {expected_state.value} attempt may be completed"
                    )
        except sqlite3.Error as exc:
            raise SearchGuardUnavailable("could not complete search attempt") from exc

    def _get_replay_payload(self, attempt_id: str, now: float) -> Any | None:
        with self._replay_lock:
            replay = self._replay_cache.get(attempt_id)
            if replay is None:
                return None
            expires_at, encoded = replay
            if expires_at <= now:
                self._replay_cache.pop(attempt_id, None)
                return None
        return json.loads(encoded)

    def _prune_replay_cache(self, now: float) -> None:
        with self._replay_lock:
            expired = [
                attempt_id
                for attempt_id, (expires_at, _) in self._replay_cache.items()
                if expires_at <= now
            ]
            for attempt_id in expired:
                self._replay_cache.pop(attempt_id, None)

    def _canonical_search(self, search: Any) -> dict[str, Any]:
        data = self._as_mapping(search)
        canonical: dict[str, Any] = {}
        for field in _SEARCH_FIELDS:
            value = data.get(field)
            if field == "children_ages":
                normalized = self._normalize_value(value or [], casefold=False)
                if isinstance(normalized, list):
                    normalized = sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
                value = normalized
            else:
                value = self._normalize_value(
                    value,
                    casefold=field in _CASE_INSENSITIVE_SEARCH_FIELDS,
                )
            canonical[field] = value

        # Treat legacy budget=500000 exactly like budget_type=max/budget_to=500000.
        raw_policy = data.get("budget_policy")
        if raw_policy is None:
            raw_policy = getattr(search, "budget_policy", None)
        policy = self._as_mapping(raw_policy, allow_none=True)
        budget_type = self._first_not_none(
            data.get("budget_type"),
            policy.get("budget_type"),
            policy.get("type"),
            policy.get("mode"),
        )
        legacy_budget = data.get("budget")
        if budget_type is None:
            budget_type = "max" if legacy_budget is not None else "unknown"
        budget_type = self._normalize_value(budget_type, casefold=True)
        budget_from = self._first_not_none(
            data.get("budget_from"),
            policy.get("budget_from"),
            data.get("normalized_budget_from"),
            policy.get("normalized_budget_from"),
            policy.get("from"),
            policy.get("minimum"),
        )
        if budget_from is None and budget_type in {"min", "range"}:
            budget_from = self._first_not_none(
                data.get("price_from"),
                policy.get("price_from"),
            )
        budget_to = self._first_not_none(
            data.get("budget_to"),
            policy.get("budget_to"),
            data.get("normalized_budget_to"),
            policy.get("normalized_budget_to"),
            policy.get("to"),
            policy.get("maximum"),
        )
        if budget_to is None and budget_type == "approx":
            budget_to = self._first_not_none(
                data.get("anchor"),
                policy.get("anchor"),
            )
        if budget_to is None and budget_type in {"max", "range"}:
            budget_to = self._first_not_none(
                data.get("price_to"),
                policy.get("price_to"),
            )
        if budget_type == "max" and budget_to is None:
            budget_to = legacy_budget
        canonical["budget_type"] = budget_type
        canonical["budget_from"] = self._normalize_value(budget_from, casefold=False)
        canonical["budget_to"] = self._normalize_value(budget_to, casefold=False)
        return canonical

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        return next((value for value in values if value is not None), None)

    @classmethod
    def _as_mapping(cls, value: Any, *, allow_none: bool = False) -> dict[str, Any]:
        if value is None and allow_none:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.asdict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
        try:
            return {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        except (TypeError, AttributeError) as exc:
            if allow_none:
                return {}
            raise SearchGuardConfigurationError(
                "search must be a mapping, dataclass, or model-like object"
            ) from exc

    @classmethod
    def _normalize_value(cls, value: Any, *, casefold: bool) -> Any:
        if isinstance(value, Enum):
            value = value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, str):
            normalized = " ".join(value.strip().split())
            return normalized.casefold() if casefold else normalized
        if isinstance(value, Mapping):
            return {
                str(key): cls._normalize_value(item, casefold=casefold)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._normalize_value(item, casefold=casefold) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)

    @classmethod
    def _sanitize_replay_payload(cls, value: Any) -> Any:
        if hasattr(value, "model_dump") and callable(value.model_dump):
            value = value.model_dump(mode="json")
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            value = dataclasses.asdict(value)
        if isinstance(value, Mapping):
            return {
                str(key): cls._sanitize_replay_payload(item)
                for key, item in value.items()
                if str(key).strip().casefold() not in _SENSITIVE_REPLAY_KEYS
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_replay_payload(item) for item in value]
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    @staticmethod
    def _canonical_json(value: Any) -> bytes:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SearchGuardConfigurationError(
                "search fingerprint contains a non-JSON value"
            ) from exc

    def _digest(self, purpose: str, payload: bytes) -> str:
        message = (
            purpose.encode("ascii")
            + b"\x00"
            + self._namespace.encode("utf-8")
            + b"\x00"
            + payload
        )
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_attempt_id(attempt_id: str) -> str:
        value = str(attempt_id).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise SearchGuardStateError("invalid search attempt ID")
        return value

    @staticmethod
    def _validate_failure_code(failure_code: str) -> str:
        value = str(failure_code).strip().upper()
        if not _SAFE_FAILURE_CODE.fullmatch(value):
            raise SearchGuardStateError("failure_code must be a stable uppercase reason")
        return value


__all__ = [
    "AttemptState",
    "ClaimAction",
    "DispatchPermit",
    "MAX_DISPATCHES_PER_WINDOW",
    "SearchClaim",
    "SearchDispatchLimitReached",
    "SearchGuard",
    "SearchGuardConfigurationError",
    "SearchGuardError",
    "SearchGuardStateError",
    "SearchGuardUnavailable",
    "STALE_ATTEMPT_SECONDS",
    "TECHNICAL_REPLAY_TTL_SECONDS",
    "WINDOW_TTL_SECONDS",
]
