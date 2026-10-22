"""UI-independent state and identity for one P006 conversion workflow.

Capture source identity in a worker. This module does not perform publication;
the converter remains responsible for its own no-follow and no-clobber checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


def _canonical_json(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        raise TypeError("settings and options must be mappings")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceIdentity:
    path: str
    size: int
    sha256: str

    @classmethod
    def from_bytes(cls, path: os.PathLike[str] | str, body: bytes) -> SourceIdentity:
        """Bind an already captured byte snapshot to its absolute source path."""
        if not isinstance(body, bytes):
            raise TypeError("source body must be bytes")
        return cls(os.path.normcase(os.path.abspath(os.fspath(path))), len(body), hashlib.sha256(body).hexdigest())

    @classmethod
    def from_file(cls, path: os.PathLike[str] | str, *, max_bytes: int = 256 * 1024 * 1024) -> SourceIdentity:
        """Hash a regular file in a worker; reject a changed file or symlink.

        The converter must still perform its own identity/security check at
        publication. This read is solely for workflow invalidation.
        """
        source = Path(path)
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        before = source.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source must be a regular file")
        if before.st_size > max_bytes:
            raise ValueError("source exceeds workflow byte limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("source changed while opening")
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("source exceeds workflow byte limit")
            after = os.fstat(descriptor)
            current = source.lstat()
            if (
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                or size != opened.st_size
            ):
                raise ValueError("source changed while hashing")
            return cls(os.path.normcase(os.path.abspath(os.fspath(source))), size, digest.hexdigest())
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class JobSnapshot:
    direction: str
    source: SourceIdentity
    settings_json: str
    settings_hash: str
    options_json: str
    options_hash: str
    revision: int

    @classmethod
    def create(
        cls,
        direction: str,
        source: SourceIdentity,
        settings: Mapping[str, Any],
        options: Mapping[str, Any],
        revision: int,
    ) -> JobSnapshot:
        if not direction or not isinstance(direction, str):
            raise ValueError("direction is required")
        if not isinstance(source, SourceIdentity):
            raise TypeError("source identity is required")
        if revision < 1:
            raise ValueError("revision must be positive")
        settings_json = _canonical_json(settings)
        options_json = _canonical_json(options)
        return cls(direction, source, settings_json, _digest(settings_json), options_json, _digest(options_json), revision)

    @property
    def settings(self) -> dict[str, Any]:
        return json.loads(self.settings_json)

    @property
    def options(self) -> dict[str, Any]:
        return json.loads(self.options_json)

    def same_inputs(self, other: JobSnapshot) -> bool:
        return (
            self.direction == other.direction
            and self.source == other.source
            and self.settings_json == other.settings_json
            and self.options_json == other.options_json
        )


class Stage(str, Enum):
    IDLE = "IDLE"
    ANALYZING = "ANALYZING"
    ANALYZED = "ANALYZED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    PUBLISHING = "PUBLISHING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PARTIAL_PUBLISH = "PARTIAL_PUBLISH"
    CLOSED = "CLOSED"


class Phase(str, Enum):
    ANALYZE = "ANALYZE"
    PLAN = "PLAN"
    PUBLISH = "PUBLISH"


class Outcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    PARTIAL_PUBLISH = "PARTIAL_PUBLISH"


class BusyError(RuntimeError):
    pass


class StaleSourceError(RuntimeError):
    pass


class StaleInputsError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobTicket:
    job_id: str
    phase: Phase
    snapshot: JobSnapshot


class WorkflowState:
    """Guard one active job and one current input revision.

    Controller/UI calls these methods on its event thread. Source hashing and
    conversion I/O belong in workers. A close requests cooperative cancellation;
    the owner must signal its worker and wait for safe completion separately.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.snapshot: JobSnapshot | None = None
        self.stage = Stage.IDLE
        self.active: JobTicket | None = None
        self.confirmed_revision: int | None = None
        self.plan_publishable = False
        self.product_status: str | None = None
        self.gx_status: str | None = None
        self.detail = ""
        self.partial_outputs: tuple[str, ...] = ()
        self._closed = False
        self._cancel_requested = False

    def set_inputs(
        self,
        direction: str,
        source: SourceIdentity,
        settings: Mapping[str, Any],
        options: Mapping[str, Any],
    ) -> JobSnapshot:
        """Replace changed inputs, invalidating a prior plan and confirmation."""
        with self._lock:
            self._require_open()
            revision = 1 if self.snapshot is None else self.snapshot.revision + 1
            candidate = JobSnapshot.create(direction, source, settings, options, revision)
            if self.snapshot is not None and candidate.same_inputs(self.snapshot):
                return self.snapshot
            self.snapshot = candidate
            self.stage = Stage.IDLE
            self.confirmed_revision = None
            self.plan_publishable = False
            self.product_status = None
            self.gx_status = None
            self.detail = ""
            self.partial_outputs = ()
            return candidate

    def begin_analyze(self) -> JobTicket:
        with self._lock:
            return self._begin(Phase.ANALYZE, {Stage.IDLE, Stage.ANALYZED, Stage.PLAN_READY,
                                               Stage.COMPLETED, Stage.FAILED, Stage.CANCELLED,
                                               Stage.PARTIAL_PUBLISH})

    def begin_plan(self) -> JobTicket:
        with self._lock:
            ticket = self._begin(Phase.PLAN, {Stage.ANALYZED, Stage.PLAN_READY})
            self.confirmed_revision = None
            return ticket

    def confirm_plan(
        self,
        current_source: SourceIdentity,
        current_settings: Mapping[str, Any],
        current_options: Mapping[str, Any],
    ) -> None:
        with self._lock:
            self._require_open()
            self._check_inputs(current_source, current_settings, current_options)
            if (self.stage != Stage.PLAN_READY or not self.plan_publishable
                    or self.active is not None or self.snapshot is None):
                raise ValueError("no current plan to confirm")
            self.confirmed_revision = self.snapshot.revision

    def begin_publish(
        self,
        current_source: SourceIdentity,
        current_settings: Mapping[str, Any],
        current_options: Mapping[str, Any],
    ) -> JobTicket:
        """Revalidate source bytes and settings before worker publication."""
        with self._lock:
            self._require_open()
            self._check_inputs(current_source, current_settings, current_options)
            if (self.snapshot is None or not self.plan_publishable
                    or self.confirmed_revision != self.snapshot.revision):
                raise ValueError("current plan requires confirmation")
            return self._begin(Phase.PUBLISH, {Stage.PLAN_READY})

    def request_cancel(self) -> bool:
        with self._lock:
            if self._closed or self.active is None:
                return False
            self._cancel_requested = True
            if self.active.snapshot == self.snapshot:
                self.stage = Stage.CANCEL_REQUESTED
            return True

    def should_cancel(self, ticket: JobTicket) -> bool:
        with self._lock:
            return self.active == ticket and (self._cancel_requested or self._closed)

    def accepts_event(self, ticket: JobTicket) -> bool:
        """Filter progress and data events before they touch presentation state."""
        with self._lock:
            return self._current(ticket) and not self._cancel_requested

    def finish(
        self,
        ticket: JobTicket,
        outcome: Outcome,
        *,
        product_status: str | None = None,
        gx_status: str | None = None,
        publishable: bool = False,
        detail: str = "",
        partial_outputs: tuple[str, ...] = (),
    ) -> bool:
        """Accept a terminal event once; return False for stale or closed jobs."""
        with self._lock:
            if self.active != ticket:
                return False
            current = self._current(ticket)
            if current:
                if not isinstance(outcome, Outcome):
                    raise TypeError("outcome must be Outcome")
                if type(publishable) is not bool:
                    raise TypeError("publishable must be bool")
                if outcome == Outcome.PARTIAL_PUBLISH and ticket.phase != Phase.PUBLISH:
                    raise ValueError("partial publication applies only to publish")
                if partial_outputs and outcome != Outcome.PARTIAL_PUBLISH:
                    raise ValueError("partial outputs require partial publication outcome")
                if publishable and (ticket.phase != Phase.PLAN or outcome != Outcome.SUCCEEDED):
                    raise ValueError("publishable applies only to a successful plan")
            self.active = None
            self._cancel_requested = False
            if not current:
                return False
            self.detail = detail
            self.partial_outputs = tuple(partial_outputs)
            self.plan_publishable = ticket.phase == Phase.PLAN and publishable and outcome == Outcome.SUCCEEDED
            if product_status is not None:
                self.product_status = product_status
            if gx_status is not None:
                self.gx_status = gx_status
            if outcome == Outcome.CANCELLED:
                self.stage = Stage.CANCELLED
                self.confirmed_revision = None
            elif outcome == Outcome.FAILED:
                self.stage = Stage.FAILED
                self.confirmed_revision = None
            elif outcome == Outcome.PARTIAL_PUBLISH:
                self.stage = Stage.PARTIAL_PUBLISH
                self.confirmed_revision = None
            elif ticket.phase == Phase.ANALYZE:
                self.stage = Stage.ANALYZED
                self.confirmed_revision = None
            elif ticket.phase == Phase.PLAN:
                self.stage = Stage.PLAN_READY
                self.confirmed_revision = None
            else:
                self.stage = Stage.COMPLETED
                self.confirmed_revision = None
            return True

    def close(self) -> JobTicket | None:
        """Block further events and expose an active ticket for cancellation."""
        with self._lock:
            ticket = self.active
            self._closed = True
            self._cancel_requested = ticket is not None
            self.confirmed_revision = None
            self.plan_publishable = False
            self.stage = Stage.CLOSED
            return ticket

    def _begin(self, phase: Phase, allowed: set[Stage]) -> JobTicket:
        self._require_open()
        if self.active is not None:
            raise BusyError("another job is still active")
        if self.snapshot is None or self.stage not in allowed:
            raise ValueError(f"cannot begin {phase.value} from {self.stage.value}")
        ticket = JobTicket(uuid.uuid4().hex, phase, self.snapshot)
        self.active = ticket
        self._cancel_requested = False
        if phase in {Phase.ANALYZE, Phase.PLAN}:
            self.plan_publishable = False
        self.stage = {Phase.ANALYZE: Stage.ANALYZING, Phase.PLAN: Stage.PLANNING,
                      Phase.PUBLISH: Stage.PUBLISHING}[phase]
        return ticket

    def _current(self, ticket: JobTicket) -> bool:
        return not self._closed and self.active == ticket and self.snapshot == ticket.snapshot

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("workflow is closed")

    def _check_inputs(
        self,
        current_source: SourceIdentity,
        current_settings: Mapping[str, Any],
        current_options: Mapping[str, Any],
    ) -> None:
        if self.snapshot is None:
            raise StaleInputsError("inputs must be selected before planning")
        candidate = JobSnapshot.create(self.snapshot.direction, current_source, current_settings,
                                       current_options, self.snapshot.revision + 1)
        if not candidate.same_inputs(self.snapshot):
            source_changed = current_source != self.snapshot.source
            self.set_inputs(self.snapshot.direction, current_source, current_settings, current_options)
            if source_changed:
                raise StaleSourceError("source identity changed; analyze and plan again")
            raise StaleInputsError("settings or options changed; analyze and plan again")
