"""Immutable filesystem store for Task-attempt evidence."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ckbbench.run.task_preflight import (
    TaskPreflightError,
    TaskPreflightEvidence,
    TaskPreflightRequirements,
    validate_task_preflight_evidence,
    validate_preflight_result_binding,
)
from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    CleanupReceipt,
    OwnershipJournalEntry,
    TaskAttemptIntent,
    TaskAttemptResult,
    canonical_json_bytes,
    validate_attempt_envelope,
    validate_journal,
    validate_receipt_chain,
    validate_result_binding,
    validate_retry_link,
    validate_retry_resource_freshness,
)

_ATTEMPT_ID = re.compile(r"^attempt-[0-9a-f]{32}$")
_CHAIN_FILE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<digest>[0-9a-f]{64})\.json$")
_MAX_ARTIFACT_BYTES = 1 << 20


class AttemptStoreError(RuntimeError):
    """Attempt evidence cannot be read or appended without weakening immutability."""


@dataclass(frozen=True)
class AttemptEnvelope:
    intent: TaskAttemptIntent
    preflight_requirements: TaskPreflightRequirements
    journal: tuple[OwnershipJournalEntry, ...]
    preflight_evidence: TaskPreflightEvidence
    result: TaskAttemptResult
    receipts: tuple[CleanupReceipt, ...]


@dataclass(frozen=True)
class AttemptState:
    intent: TaskAttemptIntent
    preflight_requirements: TaskPreflightRequirements | None
    journal: tuple[OwnershipJournalEntry, ...]
    preflight_evidence: TaskPreflightEvidence | None
    result: TaskAttemptResult | None
    receipts: tuple[CleanupReceipt, ...]


def _safe_attempt_id(value: str) -> str:
    if not isinstance(value, str) or not _ATTEMPT_ID.fullmatch(value):
        raise AttemptStoreError("attempt_id is not a valid opaque directory name")
    return value


def _lstat_regular(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise AttemptStoreError(f"{label} is missing") from None
    if not stat.S_ISREG(mode):
        raise AttemptStoreError(f"{label} must be a regular non-symlink file")


def _lstat_directory(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise AttemptStoreError(f"{label} is missing") from None
    if not stat.S_ISDIR(mode):
        raise AttemptStoreError(f"{label} must be a real directory")


def _read_document(path: Path, label: str) -> dict[str, Any]:
    _lstat_regular(path, label)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise AttemptStoreError(f"{label} must be a regular file")
            payload = handle.read(_MAX_ARTIFACT_BYTES + 1)
    except OSError as exc:
        raise AttemptStoreError(f"cannot read {label}") from exc
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise AttemptStoreError(f"{label} exceeds the artifact size limit")
    try:
        document = json.loads(payload.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise AttemptStoreError(f"{label} is not canonical JSON") from None
    if not isinstance(document, dict):
        raise AttemptStoreError(f"{label} must contain a JSON object")
    try:
        canonical = canonical_json_bytes(document)
    except AttemptSchemaError as exc:
        raise AttemptStoreError(f"{label} is not canonical JSON") from exc
    if payload != canonical:
        raise AttemptStoreError(f"{label} bytes are not canonical")
    return document


def _read_typed_document(path: Path, label: str, parser: Any) -> Any:
    document = _read_document(path, label)
    try:
        row = parser(document)
        normalized = canonical_json_bytes(row.to_dict())
    except (AttemptSchemaError, TaskPreflightError) as exc:
        raise AttemptStoreError(f"stored {label} is invalid") from exc
    if normalized != canonical_json_bytes(document):
        raise AttemptStoreError(f"{label} does not use its canonical schema representation")
    return row


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise AttemptStoreError("artifact contains a duplicate JSON key")
        document[key] = value
    return document


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_once(path: Path, document: dict[str, Any], label: str) -> None:
    payload = canonical_json_bytes(document)
    if len(payload) > _MAX_ARTIFACT_BYTES:
        raise AttemptStoreError(f"{label} exceeds the artifact size limit")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise AttemptStoreError(f"{label} already exists and cannot be replaced") from None
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class AttemptStore:
    """Append-only Task-attempt artifacts rooted in one operator-selected directory."""

    def __init__(self, root: Path | str):
        self.root = Path(root)

    def initialize(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AttemptStoreError("cannot create attempt store") from exc
        _lstat_directory(self.root, "attempt store")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.initialize()
        descriptor = os.open(
            self.root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _attempt_dir(self, attempt_id: str) -> Path:
        return self.root / _safe_attempt_id(attempt_id)

    def create_intent(self, intent: TaskAttemptIntent) -> Path:
        """Reserve a fresh attempt directory and publish its intent exactly once."""
        with self._locked():
            attempt_dir = self._attempt_dir(intent.attempt_id)
            try:
                intent_document = intent.to_dict()
            except AttemptSchemaError as exc:
                raise AttemptStoreError("attempt intent is not publishable") from exc
            if intent.retry is not None:
                predecessor = self._load_envelope_unlocked(
                    intent.retry.predecessor_attempt_id,
                    require_complete=True,
                )
                try:
                    validate_retry_link(
                        intent,
                        predecessor.intent,
                        predecessor.journal,
                        predecessor.result,
                        predecessor.receipts,
                    )
                except AttemptSchemaError as exc:
                    raise AttemptStoreError(
                        "retry intent does not bind an eligible predecessor"
                    ) from exc
                for existing_path in self.root.iterdir():
                    _lstat_directory(existing_path, "attempt store entry")
                    existing = self._read_intent(existing_path)
                    if (
                        existing.retry is not None
                        and existing.retry.predecessor_attempt_id
                        == intent.retry.predecessor_attempt_id
                    ):
                        raise AttemptStoreError(
                            "predecessor already has a reserved whole-Task retry"
                        )
            try:
                attempt_dir.mkdir(mode=0o700)
            except FileExistsError:
                raise AttemptStoreError("attempt directory is already reserved") from None
            except OSError as exc:
                raise AttemptStoreError("cannot reserve attempt directory") from exc
            _fsync_directory(self.root)
            try:
                _write_once(attempt_dir / "intent.json", intent_document, "attempt intent")
                (attempt_dir / "journal").mkdir(mode=0o700)
                (attempt_dir / "receipts").mkdir(mode=0o700)
                _fsync_directory(attempt_dir)
            except (AttemptSchemaError, AttemptStoreError, OSError) as exc:
                raise AttemptStoreError("cannot publish a valid attempt intent") from exc
            return attempt_dir / "intent.json"

    def write_preflight_requirements(
        self,
        attempt_id: str,
        requirements: TaskPreflightRequirements,
    ) -> Path:
        """Publish the immutable preflight plan before reservations or external activity."""
        with self._locked():
            attempt_dir = self._validated_attempt_dir(attempt_id, result_required=False)
            intent = self._read_intent(attempt_dir)
            if requirements.intent_sha256 != intent.sha256:
                raise AttemptStoreError("preflight requirements do not bind the stored intent")
            journal = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            if journal:
                raise AttemptStoreError("preflight requirements must precede resource claims")
            if (attempt_dir / "preflight-evidence.json").exists():
                raise AttemptStoreError("preflight evidence already seals its requirements")
            path = attempt_dir / "preflight-requirements.json"
            _write_once(path, requirements.to_dict(), "preflight requirements")
            return path

    def write_preflight_evidence(
        self,
        attempt_id: str,
        evidence: TaskPreflightEvidence,
    ) -> Path:
        """Publish one terminal preflight observation before setup or result creation."""
        with self._locked():
            attempt_dir = self._validated_attempt_dir(attempt_id, result_required=False)
            intent = self._read_intent(attempt_dir)
            requirements = self._read_preflight_requirements(attempt_dir)
            entries = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            try:
                if not entries:
                    raise AttemptSchemaError("preflight evidence needs reserved resources")
                state = validate_journal(intent, entries)
                if any(
                    entry.phase != "reserve" or entry.action != "claim" for entry in entries
                ):
                    raise AttemptSchemaError("preflight evidence must precede setup activity")
                if set(state.resources) != set(requirements.required_resource_claims):
                    raise AttemptSchemaError("preflight reservations do not match requirements")
                if evidence.created_utc < entries[-1].created_utc:
                    raise AttemptSchemaError("preflight evidence predates its reservations")
                validate_task_preflight_evidence(intent, requirements, evidence)
            except (AttemptSchemaError, TaskPreflightError) as exc:
                raise AttemptStoreError("preflight evidence does not bind the stored attempt") from exc
            path = attempt_dir / "preflight-evidence.json"
            _write_once(path, evidence.to_dict(), "preflight evidence")
            return path

    def append_journal(self, entry: OwnershipJournalEntry) -> Path:
        with self._locked():
            attempt_dir = self._validated_attempt_dir(entry.attempt_id, result_required=False)
            intent = self._read_intent(attempt_dir)
            requirements_path = attempt_dir / "preflight-requirements.json"
            if not requirements_path.exists():
                raise AttemptStoreError("preflight requirements must precede resource claims")
            evidence_path = attempt_dir / "preflight-evidence.json"
            evidence = (
                self._read_preflight_evidence(attempt_dir) if evidence_path.exists() else None
            )
            entries = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            receipts = self._read_chain(
                attempt_dir / "receipts", CleanupReceipt.from_dict, "receipt"
            )
            result_path = attempt_dir / "result.json"
            if evidence is None and (entry.phase != "reserve" or entry.action != "claim"):
                raise AttemptStoreError("setup activity requires terminal preflight evidence")
            if evidence is not None and entry.action == "claim":
                raise AttemptStoreError("preflight evidence seals the resource claim set")
            if evidence is not None and evidence.status == "failed" and not result_path.exists():
                raise AttemptStoreError("failed preflight must be sealed before teardown")
            if intent.retry is not None and entry.action == "claim":
                predecessor = self._load_envelope_unlocked(
                    intent.retry.predecessor_attempt_id,
                    require_complete=True,
                )
                predecessor_resources = set(
                    validate_journal(
                        predecessor.intent,
                        predecessor.journal,
                    ).resources
                )
                if (entry.resource_kind, entry.resource_id) in predecessor_resources:
                    raise AttemptStoreError("retry must claim a fresh resource identity")
            if result_path.exists():
                result = self._read_result(attempt_dir)
                try:
                    if receipts:
                        if receipts[-1].status == "complete":
                            raise AttemptSchemaError("completed cleanup seals the journal")
                        if entry.phase != "reconcile" or entry.action == "claim":
                            raise AttemptSchemaError(
                                "reconciliation may only update an already claimed resource"
                            )
                    elif entry.phase != "teardown":
                        raise AttemptSchemaError(
                            "post-result journal entries must begin in teardown"
                        )
                    validate_receipt_chain(
                        intent,
                        (*entries, entry),
                        result,
                        receipts,
                        require_complete=False,
                        allow_pending_reconciliation=True,
                    )
                except AttemptSchemaError as exc:
                    raise AttemptStoreError("journal append violates the sealed result") from exc
            elif receipts:
                raise AttemptStoreError("cleanup receipt exists before the attempt result")
            elif entry.phase in {"teardown", "reconcile"}:
                raise AttemptStoreError("attempt result must be published before teardown")
            try:
                validate_journal(intent, (*entries, entry))
            except AttemptSchemaError as exc:
                raise AttemptStoreError("journal append violates the ownership chain") from exc
            path = attempt_dir / "journal" / f"{entry.sequence:06d}-{entry.sha256}.json"
            _write_once(path, entry.to_dict(), "journal entry")
            return path

    def write_result(self, result: TaskAttemptResult) -> Path:
        with self._locked():
            attempt_dir = self._validated_attempt_dir(result.attempt_id, result_required=False)
            intent = self._read_intent(attempt_dir)
            requirements = self._read_preflight_requirements(attempt_dir)
            preflight = self._read_preflight_evidence(attempt_dir)
            entries = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            receipts = self._read_chain(
                attempt_dir / "receipts", CleanupReceipt.from_dict, "receipt"
            )
            if receipts:
                raise AttemptStoreError("cleanup receipt exists before the attempt result")
            try:
                validate_journal(intent, entries)
                validate_result_binding(intent, entries, result)
                validate_preflight_result_binding(intent, requirements, preflight, result)
                if result.pre_teardown_journal_sha256 != entries[-1].sha256:
                    raise AttemptSchemaError("result must be published before teardown begins")
            except (AttemptSchemaError, TaskPreflightError) as exc:
                raise AttemptStoreError("result does not bind the stored attempt") from exc
            path = attempt_dir / "result.json"
            _write_once(path, result.to_dict(), "attempt result")
            return path

    def append_receipt(self, receipt: CleanupReceipt) -> Path:
        with self._locked():
            attempt_dir = self._validated_attempt_dir(receipt.attempt_id, result_required=True)
            intent = self._read_intent(attempt_dir)
            entries = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            result = self._read_result(attempt_dir)
            receipts = self._read_chain(
                attempt_dir / "receipts", CleanupReceipt.from_dict, "receipt"
            )
            try:
                validate_journal(intent, entries)
                validate_result_binding(intent, entries, result)
                validate_receipt_chain(
                    intent,
                    entries,
                    result,
                    (*receipts, receipt),
                    require_complete=False,
                )
            except AttemptSchemaError as exc:
                raise AttemptStoreError("receipt append violates the cleanup chain") from exc
            path = attempt_dir / "receipts" / f"{receipt.sequence:06d}-{receipt.sha256}.json"
            _write_once(path, receipt.to_dict(), "cleanup receipt")
            return path

    def load_envelope(self, attempt_id: str, *, require_complete: bool = True) -> AttemptEnvelope:
        with self._locked():
            return self._load_envelope_unlocked(attempt_id, require_complete=require_complete)

    def load_state(self, attempt_id: str) -> AttemptState:
        """Load and validate the currently published prefix of an interrupted attempt."""
        with self._locked():
            attempt_dir = self._validated_attempt_dir(attempt_id, result_required=False)
            intent = self._read_intent(attempt_dir)
            requirements = (
                self._read_preflight_requirements(attempt_dir)
                if (attempt_dir / "preflight-requirements.json").exists()
                else None
            )
            entries = self._read_chain(
                attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
            )
            evidence = (
                self._read_preflight_evidence(attempt_dir)
                if (attempt_dir / "preflight-evidence.json").exists()
                else None
            )
            result = (
                self._read_result(attempt_dir) if (attempt_dir / "result.json").exists() else None
            )
            receipts = self._read_chain(
                attempt_dir / "receipts", CleanupReceipt.from_dict, "receipt"
            )
            try:
                if entries:
                    validate_journal(intent, entries)
                if requirements is not None and requirements.intent_sha256 != intent.sha256:
                    raise AttemptSchemaError("requirements do not bind the stored intent")
                if evidence is not None:
                    if requirements is None or not entries:
                        raise AttemptSchemaError("preflight evidence lacks its required prefix")
                    validate_task_preflight_evidence(intent, requirements, evidence)
                if result is not None:
                    if requirements is None or evidence is None:
                        raise AttemptSchemaError("result lacks its preflight evidence")
                    validate_result_binding(intent, entries, result)
                    validate_preflight_result_binding(intent, requirements, evidence, result)
                    validate_receipt_chain(
                        intent,
                        entries,
                        result,
                        receipts,
                        require_complete=False,
                        allow_pending_reconciliation=True,
                    )
                elif receipts:
                    raise AttemptSchemaError("cleanup receipts require a sealed result")
            except (AttemptSchemaError, TaskPreflightError) as exc:
                raise AttemptStoreError("stored attempt prefix is invalid") from exc
            return AttemptState(intent, requirements, entries, evidence, result, receipts)

    def _load_envelope_unlocked(
        self,
        attempt_id: str,
        *,
        require_complete: bool,
        ancestors: frozenset[str] = frozenset(),
    ) -> AttemptEnvelope:
        if attempt_id in ancestors:
            raise AttemptStoreError("attempt retry lineage contains a cycle")
        attempt_dir = self._validated_attempt_dir(attempt_id, result_required=True)
        intent = self._read_intent(attempt_dir)
        requirements = self._read_preflight_requirements(attempt_dir)
        entries = self._read_chain(
            attempt_dir / "journal", OwnershipJournalEntry.from_dict, "journal"
        )
        result = self._read_result(attempt_dir)
        preflight = self._read_preflight_evidence(attempt_dir)
        receipts = self._read_chain(
            attempt_dir / "receipts", CleanupReceipt.from_dict, "receipt"
        )
        try:
            validate_attempt_envelope(
                intent,
                entries,
                result,
                receipts,
                require_complete=require_complete,
            )
            validate_preflight_result_binding(intent, requirements, preflight, result)
        except (AttemptSchemaError, TaskPreflightError) as exc:
            raise AttemptStoreError("stored attempt envelope is invalid") from exc
        if intent.retry is not None:
            predecessor = self._load_envelope_unlocked(
                intent.retry.predecessor_attempt_id,
                require_complete=True,
                ancestors=ancestors | {attempt_id},
            )
            try:
                validate_retry_link(
                    intent,
                    predecessor.intent,
                    predecessor.journal,
                    predecessor.result,
                    predecessor.receipts,
                )
                validate_retry_resource_freshness(
                    intent,
                    entries,
                    predecessor.intent,
                    predecessor.journal,
                )
            except AttemptSchemaError as exc:
                raise AttemptStoreError("stored retry lineage is invalid") from exc
        return AttemptEnvelope(intent, requirements, entries, preflight, result, receipts)

    def _validated_attempt_dir(self, attempt_id: str, *, result_required: bool) -> Path:
        attempt_dir = self._attempt_dir(attempt_id)
        _lstat_directory(attempt_dir, "attempt directory")
        base = {"intent.json", "journal", "receipts"}
        optional = {
            "preflight-requirements.json", "preflight-evidence.json", "result.json",
        }
        actual = {item.name for item in attempt_dir.iterdir()}
        if not base <= actual or not actual <= base | optional:
            raise AttemptStoreError("attempt directory contains missing or unexpected artifacts")
        has_requirements = "preflight-requirements.json" in actual
        has_evidence = "preflight-evidence.json" in actual
        has_result = "result.json" in actual
        if has_evidence and not has_requirements:
            raise AttemptStoreError("preflight evidence is missing its requirements")
        if has_result and not has_evidence:
            raise AttemptStoreError("attempt result is missing its preflight evidence")
        if result_required and not has_result:
            raise AttemptStoreError("attempt result is missing")
        _lstat_directory(attempt_dir / "journal", "journal directory")
        _lstat_directory(attempt_dir / "receipts", "receipt directory")
        return attempt_dir

    def _read_intent(self, attempt_dir: Path) -> TaskAttemptIntent:
        intent = _read_typed_document(
            attempt_dir / "intent.json", "attempt intent", TaskAttemptIntent.from_dict
        )
        if intent.attempt_id != attempt_dir.name:
            raise AttemptStoreError("attempt directory name does not match its intent")
        return intent

    def _read_result(self, attempt_dir: Path) -> TaskAttemptResult:
        return _read_typed_document(
            attempt_dir / "result.json", "attempt result", TaskAttemptResult.from_dict
        )

    def _read_preflight_requirements(
        self,
        attempt_dir: Path,
    ) -> TaskPreflightRequirements:
        return _read_typed_document(
            attempt_dir / "preflight-requirements.json",
            "preflight requirements",
            TaskPreflightRequirements.from_dict,
        )

    def _read_preflight_evidence(self, attempt_dir: Path) -> TaskPreflightEvidence:
        return _read_typed_document(
            attempt_dir / "preflight-evidence.json",
            "preflight evidence",
            TaskPreflightEvidence.from_dict,
        )

    def _read_chain(self, directory: Path, parser: Any, label: str) -> tuple[Any, ...]:
        _lstat_directory(directory, f"{label} directory")
        rows: list[Any] = []
        for expected_sequence, path in enumerate(sorted(directory.iterdir())):
            match = _CHAIN_FILE.fullmatch(path.name)
            if match is None or int(match.group("sequence")) != expected_sequence:
                raise AttemptStoreError(f"{label} filenames are missing, extra or reordered")
            row = _read_typed_document(path, label, parser)
            if row.sequence != expected_sequence or row.sha256 != match.group("digest"):
                raise AttemptStoreError(f"{label} filename does not bind its artifact")
            rows.append(row)
        return tuple(rows)
