"""Cross-process run locking and fail-closed inflight journaling.

The helpers in this module are intentionally evaluator-agnostic.  Callers are
responsible for atomically persisting the state object after beginning,
reconciling, or clearing an inflight record.
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Callable, Mapping

INFLIGHT_SCHEMA_VERSION = "recuris-eval-inflight-v1"


class EvaluationRunLockError(RuntimeError):
    """Raised when an evaluation run cannot acquire its exclusive lock."""


class InflightJournalError(RuntimeError):
    """Raised when interrupted work cannot be proven safely committed."""


class RunFileLock:
    """A non-blocking, cross-process exclusive lock on one file byte."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self._handle = None

    def acquire(self) -> None:
        if self._handle is not None:
            raise EvaluationRunLockError(
                f"evaluation run lock is already held: {self.path}"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        except (OSError, IOError) as exc:
            handle.close()
            raise EvaluationRunLockError(
                f"evaluation run is already active or lock unavailable: "
                f"{self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    def __enter__(self) -> "RunFileLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


def make_inflight_record(
        *,
        run_id: str,
        protocol_sha256: str,
        shard_id: str,
        block_id: str,
        phase: str,
        task_id: str,
        arm: str,
        package_path: str,
        package_sha256: str,
        num_trials: int,
        expected_trial_seeds: list[int],
) -> dict:
    """Build the exact, deterministic identity of one downstream call."""
    return {
        "schema_version": INFLIGHT_SCHEMA_VERSION,
        "run_id": run_id,
        "protocol_sha256": protocol_sha256,
        "shard_id": shard_id,
        "block_id": block_id,
        "phase": phase,
        "task_id": task_id,
        "arm": arm,
        "package": {
            "path": str(Path(package_path).resolve()),
            "sha256": package_sha256,
        },
        "cell": {
            "num_trials": num_trials,
            "expected_trial_seeds": list(expected_trial_seeds),
        },
    }


def begin_inflight(state: dict, record: dict) -> None:
    """Install one inflight record, refusing to overwrite prior work."""
    if state.get("inflight") is not None:
        raise InflightJournalError(
            "evaluation state already contains an inflight shard"
        )
    state["inflight"] = copy.deepcopy(record)


def clear_inflight(state: dict, expected_record: dict) -> None:
    """Clear only the exact inflight record the caller believes it owns."""
    if state.get("inflight") != expected_record:
        raise InflightJournalError(
            "inflight shard changed before it could be cleared"
        )
    state.pop("inflight", None)


def reconcile_inflight(
        state: dict,
        expected_by_shard_id: Mapping[str, dict],
        verify_committed: Callable[[str, dict], bool],
) -> bool:
    """Clear interrupted work only after its committed result is verified.

    Returns ``True`` when a verified stale inflight entry was cleared and
    ``False`` when there was no inflight entry.  Any uncertainty fails closed.
    """
    inflight = state.get("inflight")
    if inflight is None:
        return False
    if not isinstance(inflight, dict):
        raise InflightJournalError(
            "evaluation state has a malformed inflight record"
        )
    shard_id = inflight.get("shard_id")
    if not isinstance(shard_id, str) or not shard_id:
        raise InflightJournalError(
            "inflight record does not identify a shard"
        )
    expected = expected_by_shard_id.get(shard_id)
    if expected is None or inflight != expected:
        raise InflightJournalError(
            f"inflight identity does not match frozen shard {shard_id}"
        )
    completed = state.get("completed_shards")
    committed_record = (
        completed.get(shard_id) if isinstance(completed, dict) else None
    )
    if not isinstance(committed_record, dict):
        raise InflightJournalError(
            f"inflight shard {shard_id} has no committed result"
        )
    try:
        verified = verify_committed(shard_id, committed_record)
    except Exception as exc:
        raise InflightJournalError(
            f"inflight shard {shard_id} committed result is not verifiable"
        ) from exc
    if verified is not True:
        raise InflightJournalError(
            f"inflight shard {shard_id} committed result is not verifiable"
        )
    state.pop("inflight", None)
    return True
