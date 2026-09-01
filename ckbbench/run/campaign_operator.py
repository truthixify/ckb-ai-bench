"""Operator commands for immutable campaign execution."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence, TextIO

from ckbbench.run.attempt_store import (
    AttemptEnvelope,
    AttemptState,
    AttemptStore,
    AttemptStoreError,
)
from ckbbench.run.campaign import (
    AcceptedReportResolution,
    AttemptArtifactReference,
    CampaignError,
    CampaignManifest,
    CampaignSlot,
    ExploratoryAttemptSummary,
    ExploratoryPreview,
    ResolvedCampaignSlot,
    freeze_campaign,
    load_campaign,
    load_report_resolution,
    publish_document,
    validate_intent_for_slot,
    validate_report_resolution,
)
from ckbbench.run.campaign_report import (
    CampaignReportError,
    ReportBuilderSource,
    build_campaign_report_dataset,
    publish_campaign_report,
    resolve_report_builder_source,
)
from ckbbench.run.calibration import (
    CalibrationError,
    CalibrationRuntimeFactory,
    run_calibration,
)
from ckbbench.run.task_preflight import TaskPreflightProbe, TaskPreflightRequirements
from ckbbench.run.retry_policy import (
    RETRY_COOLDOWN_SECONDS,
    is_retryable_infrastructure_failure,
)
from ckbbench.run.single_task import (
    SingleTaskBackend,
    SingleTaskExecutionError,
    execute_single_task,
    recover_single_task,
)
from ckbbench.run.suite_release import (
    CampaignReleaseBinding,
    SuiteReleaseError,
    freeze_campaign_from_release,
    load_chain_profile,
    load_suite_release,
    load_treatment_profile,
    validate_campaign_release,
)
from ckbbench.run.task_attempt import (
    AttemptSchemaError,
    RetryReference,
    TaskAttemptIntent,
    artifact_sha256,
    canonical_json_bytes,
    validate_retry_link,
    validate_retry_resource_freshness,
)
from ckbbench.suite.registry import RegistryError, load_suite


class CampaignOperatorError(RuntimeError):
    """An operator action would violate the frozen campaign or evidence state."""


class CampaignOperatorBusy(CampaignOperatorError):
    """Another process owns accepted scheduling on this host boundary."""


DEFAULT_COORDINATION_ROOT = (
    Path(tempfile.gettempdir()) / f"ckbbench-campaign-accepted-{os.geteuid()}"
)


@dataclass(frozen=True)
class PreparedTaskAttempt:
    intent: TaskAttemptIntent
    requirements: TaskPreflightRequirements
    preflight_probe: TaskPreflightProbe
    backend: SingleTaskBackend
    max_score: int

    def __post_init__(self) -> None:
        if not isinstance(self.intent, TaskAttemptIntent):
            raise CampaignOperatorError("runtime returned an untyped attempt intent")
        if not isinstance(self.requirements, TaskPreflightRequirements):
            raise CampaignOperatorError("runtime returned untyped preflight requirements")
        if self.requirements.intent_sha256 != self.intent.sha256:
            raise CampaignOperatorError("runtime requirements do not bind its intent")
        if isinstance(self.max_score, bool) or not isinstance(self.max_score, int) or self.max_score <= 0:
            raise CampaignOperatorError("runtime returned an invalid maximum score")


class TaskRuntimeFactory(Protocol):
    """Build private adapters and attempt inputs without performing external activity."""

    def prepare(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        predecessor: AttemptEnvelope | None,
    ) -> PreparedTaskAttempt: ...

    def prepare_recovery(
        self,
        manifest: CampaignManifest,
        slot: CampaignSlot,
        state: AttemptState,
    ) -> tuple[TaskPreflightRequirements, SingleTaskBackend, int]: ...


@dataclass(frozen=True)
class SlotProgress:
    slot: CampaignSlot
    original: AttemptState | None
    retry: AttemptState | None
    status: str


@dataclass(frozen=True)
class CampaignProgress:
    slots: tuple[SlotProgress, ...]

    @property
    def current(self) -> SlotProgress | None:
        return next((slot for slot in self.slots if slot.status != "terminal"), None)

    @property
    def complete(self) -> bool:
        return self.current is None


def _slot_for_intent(manifest: CampaignManifest, intent: TaskAttemptIntent) -> CampaignSlot:
    candidates = tuple(
        slot
        for slot in manifest.slots
        if (
            slot.batch_id,
            slot.trial_id,
            slot.task_id,
            slot.arm,
            slot.model_variant_id,
        )
        == (
            intent.identity.batch_id,
            intent.identity.trial_id,
            intent.identity.task_id,
            intent.identity.arm,
            intent.identity.model_variant_id,
        )
    )
    if len(candidates) != 1:
        raise CampaignOperatorError("attempt does not identify exactly one campaign slot")
    try:
        validate_intent_for_slot(manifest, candidates[0], intent)
    except CampaignError as exc:
        raise CampaignOperatorError("attempt identity crosses its campaign boundary") from exc
    return candidates[0]


def _cleanup_complete(state: AttemptState) -> bool:
    return bool(state.receipts and state.receipts[-1].status == "complete")


def _slot_status(original: AttemptState | None, retry: AttemptState | None) -> str:
    if original is None:
        if retry is not None:
            raise CampaignOperatorError("retry evidence exists without an original attempt")
        return "pending"
    if original.result is None:
        return "active"
    if not _cleanup_complete(original):
        return "cleanup-incomplete"
    retryable = is_retryable_infrastructure_failure(original.result)
    if not retryable:
        if retry is not None:
            raise CampaignOperatorError("a terminal attempt cannot have a whole-Task retry")
        return "terminal"
    if retry is None:
        return "needs-retry"
    if retry.result is None:
        return "active"
    if not _cleanup_complete(retry):
        return "cleanup-incomplete"
    return "terminal"


def inspect_campaign(manifest: CampaignManifest, store: AttemptStore) -> CampaignProgress:
    grouped: dict[str, dict[int, AttemptState]] = {slot.slot_id: {} for slot in manifest.slots}
    for attempt_id in store.list_attempt_ids():
        state = store.load_state(attempt_id)
        slot = _slot_for_intent(manifest, state.intent)
        ordinal = state.intent.retry_ordinal
        if ordinal in grouped[slot.slot_id]:
            raise CampaignOperatorError("campaign contains duplicate attempts for one slot ordinal")
        grouped[slot.slot_id][ordinal] = state

    progress: list[SlotProgress] = []
    unresolved_seen = False
    for slot in manifest.ordered_slots:
        original = grouped[slot.slot_id].get(0)
        retry = grouped[slot.slot_id].get(1)
        if retry is not None:
            if original is None or not _cleanup_complete(original) or original.result is None:
                raise CampaignOperatorError("retry does not have a complete original predecessor")
            reference = retry.intent.retry
            if reference is None:
                raise CampaignOperatorError("retry attempt is missing its predecessor reference")
            expected = RetryReference(
                predecessor_attempt_id=original.intent.attempt_id,
                predecessor_intent_sha256=original.intent.sha256,
                predecessor_result_sha256=original.result.sha256,
                predecessor_cleanup_receipt_sha256=original.receipts[-1].sha256,
            )
            if reference != expected:
                raise CampaignOperatorError("retry does not bind the slot's original attempt")
            if retry.journal:
                try:
                    validate_retry_link(
                        retry.intent,
                        original.intent,
                        original.journal,
                        original.result,
                        original.receipts,
                    )
                    validate_retry_resource_freshness(
                        retry.intent,
                        retry.journal,
                        original.intent,
                        original.journal,
                    )
                except AttemptSchemaError as exc:
                    raise CampaignOperatorError("retry evidence violates its frozen lineage") from exc
        status = _slot_status(original, retry)
        if unresolved_seen and (original is not None or retry is not None):
            raise CampaignOperatorError("campaign evidence skips ahead of an unresolved slot")
        if status != "terminal":
            unresolved_seen = True
        progress.append(SlotProgress(slot, original, retry, status))
    return CampaignProgress(tuple(progress))


@contextmanager
def _campaign_lock(coordination_root: Path) -> Iterator[None]:
    try:
        coordination_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise CampaignOperatorError("cannot create the accepted-execution coordination root") from exc
    try:
        root_descriptor = os.open(
            coordination_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise CampaignOperatorError("accepted-execution coordination root must be a real directory") from exc
    try:
        root_status = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root_status.st_mode):
            raise CampaignOperatorError("accepted-execution coordination root must be a directory")
        if root_status.st_uid != os.geteuid() or stat.S_IMODE(root_status.st_mode) & 0o077:
            raise CampaignOperatorError("accepted-execution coordination root must be private")
        descriptor = os.open(
            ".accepted-execution.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
    except CampaignOperatorError:
        os.close(root_descriptor)
        raise
    except OSError as exc:
        os.close(root_descriptor)
        raise CampaignOperatorError("cannot open the campaign scheduler lock") from exc
    try:
        lock_status = os.fstat(descriptor)
        if not stat.S_ISREG(lock_status.st_mode):
            raise CampaignOperatorError("campaign scheduler lock must be a regular file")
        if lock_status.st_uid != os.geteuid() or stat.S_IMODE(lock_status.st_mode) & 0o077:
            raise CampaignOperatorError("campaign scheduler lock must be private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CampaignOperatorBusy("another campaign command is already executing") from None
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
            os.close(root_descriptor)


def _prepare_and_execute(
    manifest: CampaignManifest,
    slot: CampaignSlot,
    store: AttemptStore,
    runtime: TaskRuntimeFactory,
    predecessor: AttemptEnvelope | None,
    release_binding: CampaignReleaseBinding | None,
) -> AttemptEnvelope:
    prepared = runtime.prepare(manifest, slot, predecessor)
    validate_intent_for_slot(manifest, slot, prepared.intent)
    execution_contract = None
    if release_binding is not None:
        try:
            release_binding.validate_preflight(
                manifest,
                slot,
                prepared.intent,
                prepared.requirements,
            )
            execution_contract = release_binding.execution_contract_for(slot)
        except SuiteReleaseError as exc:
            raise CampaignOperatorError(
                "runtime preparation differs from the suite release"
            ) from exc
    if prepared.max_score != slot.max_score:
        raise CampaignOperatorError("runtime maximum score differs from the frozen slot")
    if predecessor is None:
        if prepared.intent.retry_ordinal != 0 or prepared.intent.retry is not None:
            raise CampaignOperatorError("original slot execution cannot carry retry provenance")
    else:
        expected = RetryReference(
            predecessor_attempt_id=predecessor.intent.attempt_id,
            predecessor_intent_sha256=predecessor.intent.sha256,
            predecessor_result_sha256=predecessor.result.sha256,
            predecessor_cleanup_receipt_sha256=predecessor.receipts[-1].sha256,
        )
        if prepared.intent.retry_ordinal != 1 or prepared.intent.retry != expected:
            raise CampaignOperatorError("runtime retry does not bind the eligible predecessor")
    return execute_single_task(
        store,
        prepared.intent,
        prepared.requirements,
        prepared.preflight_probe,
        prepared.backend,
        max_score=prepared.max_score,
        execution_contract=execution_contract,
    )


def _load_complete(store: AttemptStore, state: AttemptState) -> AttemptEnvelope:
    if not _cleanup_complete(state):
        raise CampaignOperatorError("attempt cleanup is not complete")
    return store.load_envelope(state.intent.attempt_id)


class CampaignOperator:
    def __init__(
        self,
        manifest: CampaignManifest,
        store: AttemptStore,
        runtime: TaskRuntimeFactory,
        coordination_root: Path | str,
        retry_wait: Callable[[float], None] = time.sleep,
        release_binding: CampaignReleaseBinding | None = None,
    ) -> None:
        self.manifest = manifest
        self.store = store
        self.runtime = runtime
        self.coordination_root = Path(coordination_root)
        self.retry_wait = retry_wait
        self.release_binding = release_binding
        major = int(manifest.suite_semver.split(".", 1)[0])
        if major >= 4 and release_binding is None:
            raise CampaignOperatorError(
                "this suite requires an immutable release binding before execution"
            )
        if release_binding is not None:
            try:
                release_binding.validate_manifest(manifest)
            except SuiteReleaseError as exc:
                raise CampaignOperatorError("campaign release binding is invalid") from exc

    def _execute_retry(
        self,
        slot: CampaignSlot,
        predecessor: AttemptState,
    ) -> AttemptEnvelope:
        if predecessor.result is None or not is_retryable_infrastructure_failure(
            predecessor.result
        ):
            raise CampaignOperatorError("attempt is not eligible for an infrastructure retry")
        try:
            self.retry_wait(float(RETRY_COOLDOWN_SECONDS))
        except Exception as exc:
            raise CampaignOperatorError("infrastructure retry cooldown failed") from exc
        return _prepare_and_execute(
            self.manifest,
            slot,
            self.store,
            self.runtime,
            _load_complete(self.store, predecessor),
            self.release_binding,
        )

    def run_task(self, slot_id: str) -> AttemptEnvelope:
        with _campaign_lock(self.coordination_root):
            progress = inspect_campaign(self.manifest, self.store)
            current = progress.current
            if current is None:
                raise CampaignOperatorError("campaign is already complete")
            if current.slot.slot_id != slot_id:
                raise CampaignOperatorError("only the next unresolved campaign slot may run")
            if current.status == "needs-retry":
                raise CampaignOperatorError("the current slot needs its declared retry")
            if current.status != "pending":
                raise CampaignOperatorError("the current slot has unfinished attempt evidence")
            return _prepare_and_execute(
                self.manifest,
                current.slot,
                self.store,
                self.runtime,
                None,
                self.release_binding,
            )

    def retry(self, predecessor_attempt_id: str) -> AttemptEnvelope:
        with _campaign_lock(self.coordination_root):
            progress = inspect_campaign(self.manifest, self.store)
            current = progress.current
            if (
                current is None
                or current.status != "needs-retry"
                or current.original is None
                or current.original.intent.attempt_id != predecessor_attempt_id
            ):
                raise CampaignOperatorError("attempt is not the current eligible infrastructure retry")
            return self._execute_retry(current.slot, current.original)

    def recover(self, attempt_id: str) -> AttemptEnvelope:
        with _campaign_lock(self.coordination_root):
            progress = inspect_campaign(self.manifest, self.store)
            current = progress.current
            if current is None or current.status not in {"active", "cleanup-incomplete"}:
                raise CampaignOperatorError("campaign has no interrupted attempt to recover")
            state = next(
                (
                    candidate
                    for candidate in (current.original, current.retry)
                    if candidate is not None and candidate.intent.attempt_id == attempt_id
                ),
                None,
            )
            if state is None:
                raise CampaignOperatorError("attempt is not the current interrupted campaign attempt")
            requirements, backend, max_score = self.runtime.prepare_recovery(
                self.manifest,
                current.slot,
                state,
            )
            if not isinstance(requirements, TaskPreflightRequirements):
                raise CampaignOperatorError("runtime returned untyped recovery requirements")
            if requirements.intent_sha256 != state.intent.sha256:
                raise CampaignOperatorError("recovery requirements do not bind the interrupted attempt")
            if max_score != current.slot.max_score:
                raise CampaignOperatorError("recovery maximum score differs from the frozen slot")
            if self.release_binding is not None:
                try:
                    self.release_binding.validate_preflight(
                        self.manifest,
                        current.slot,
                        state.intent,
                        requirements,
                    )
                    execution_contract = self.release_binding.execution_contract_for(current.slot)
                except SuiteReleaseError as exc:
                    raise CampaignOperatorError(
                        "recovery requirements differ from the suite release"
                    ) from exc
            else:
                execution_contract = None
            return recover_single_task(
                self.store,
                state.intent.attempt_id,
                requirements,
                backend,
                max_score=max_score,
                execution_contract=execution_contract,
            )

    def run_batch(self, batch_id: str) -> tuple[AttemptEnvelope, ...]:
        with _campaign_lock(self.coordination_root):
            batch = next(
                (batch for batch in self.manifest.batches if batch.batch_id == batch_id),
                None,
            )
            if batch is None:
                raise CampaignOperatorError("batch is not declared by the campaign")
            executed: list[AttemptEnvelope] = []
            while True:
                progress = inspect_campaign(self.manifest, self.store)
                current = progress.current
                if current is None:
                    return tuple(executed)
                if current.slot.batch_id != batch_id:
                    ordered = [item.batch_id for item in self.manifest.batches]
                    current_index = ordered.index(current.slot.batch_id)
                    requested_index = ordered.index(batch_id)
                    if current_index > requested_index:
                        return tuple(executed)
                    raise CampaignOperatorError("an earlier campaign batch is not complete")
                if current.status in {"active", "cleanup-incomplete"}:
                    raise CampaignOperatorError("current slot must be recovered before batch execution")
                if current.status == "pending":
                    envelope = _prepare_and_execute(
                        self.manifest,
                        current.slot,
                        self.store,
                        self.runtime,
                        None,
                        self.release_binding,
                    )
                elif current.status == "needs-retry":
                    if current.original is None:
                        raise CampaignOperatorError("retry state is missing its original attempt")
                    envelope = self._execute_retry(current.slot, current.original)
                else:
                    raise CampaignOperatorError("campaign progress is internally inconsistent")
                executed.append(envelope)
                if envelope.receipts[-1].status != "complete":
                    return tuple(executed)


def _attempt_reference(envelope: AttemptEnvelope) -> AttemptArtifactReference:
    return AttemptArtifactReference(
        attempt_id=envelope.intent.attempt_id,
        intent_sha256=envelope.intent.sha256,
        preflight_requirements_sha256=envelope.preflight_requirements.sha256,
        journal_entry_sha256s=tuple(entry.sha256 for entry in envelope.journal),
        preflight_evidence_sha256=envelope.preflight_evidence.sha256,
        result_sha256=envelope.result.sha256,
        cleanup_receipt_sha256s=tuple(receipt.sha256 for receipt in envelope.receipts),
        retry_ordinal=envelope.intent.retry_ordinal,
        outcome=envelope.result.outcome,
    )


def resolve_accepted_report(
    manifest: CampaignManifest,
    store: AttemptStore,
) -> AcceptedReportResolution:
    progress = inspect_campaign(manifest, store)
    if not progress.complete:
        raise CampaignOperatorError("campaign is not complete and cannot resolve an accepted report")
    resolved = []
    for slot_progress in progress.slots:
        if slot_progress.original is None:
            raise CampaignOperatorError("complete campaign slot is missing its original attempt")
        original = store.load_envelope(slot_progress.original.intent.attempt_id)
        retry = (
            None
            if slot_progress.retry is None
            else store.load_envelope(slot_progress.retry.intent.attempt_id)
        )
        resolved.append(
            ResolvedCampaignSlot(
                slot_id=slot_progress.slot.slot_id,
                original=_attempt_reference(original),
                retry=None if retry is None else _attempt_reference(retry),
                terminal_attempt_id=(
                    original.intent.attempt_id if retry is None else retry.intent.attempt_id
                ),
            )
        )
    resolution = AcceptedReportResolution(
        campaign_id=manifest.campaign_id,
        campaign_manifest_sha256=manifest.sha256,
        slots=tuple(resolved),
    )
    validate_report_resolution(manifest, resolution)
    return resolution


def validate_report_resolution_evidence(
    manifest: CampaignManifest,
    resolution: AcceptedReportResolution,
    store: AttemptStore,
) -> None:
    """Require a resolution to be the sole outcome-independent view of retained evidence."""
    validate_report_resolution(manifest, resolution)
    expected = resolve_accepted_report(manifest, store)
    if canonical_json_bytes(resolution.to_dict()) != canonical_json_bytes(expected.to_dict()):
        raise CampaignOperatorError("report resolution does not match retained campaign evidence")


def build_exploratory_preview(store: AttemptStore) -> ExploratoryPreview:
    summaries = []
    for attempt_id in store.list_attempt_ids():
        state = store.load_state(attempt_id)
        if state.result is None:
            status = "active"
        elif not state.receipts:
            status = "cleanup-pending"
        elif state.receipts[-1].status == "complete":
            status = "complete"
        else:
            status = "cleanup-incomplete"
        summaries.append(
            ExploratoryAttemptSummary(
                attempt_id=attempt_id,
                campaign_id=state.intent.identity.campaign_id,
                task_id=state.intent.identity.task_id,
                arm=state.intent.identity.arm,
                model_variant_id=state.intent.identity.model_variant_id,
                retry_ordinal=state.intent.retry_ordinal,
                state=status,
                outcome=None if state.result is None else state.result.outcome,
            )
        )
    return ExploratoryPreview(tuple(sorted(summaries, key=lambda row: row.attempt_id)))


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise CampaignOperatorError("invalid command arguments")


def _parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="ckbbench campaign")
    commands = parser.add_subparsers(dest="command", required=True)

    tasks = commands.add_parser("tasks")
    tasks.add_argument("--suite", required=True)

    freeze = commands.add_parser("freeze")
    freeze.add_argument("--draft", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--suite")
    freeze.add_argument("--chain-profile", action="append", default=[])
    freeze.add_argument("--treatment-profile", action="append", default=[])

    plan = commands.add_parser("plan")
    plan.add_argument("--manifest", required=True)

    release_commands = [plan]

    calibrate = commands.add_parser("calibrate")
    calibrate.add_argument("--manifest", required=True)
    calibrate.add_argument("--slot", required=True)
    calibrate.add_argument("--calibration-id", required=True)
    calibrate.add_argument("--attempt-root", required=True)
    calibrate.add_argument("--output", required=True)
    calibrate.add_argument("--authorized-by-user", action="store_true")
    release_commands.append(calibrate)

    for name in ("run-task", "run-batch", "retry", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", required=True)
        command.add_argument("--attempt-root")
        release_commands.append(command)
        if name == "run-task":
            command.add_argument("--slot", required=True)
        elif name == "run-batch":
            command.add_argument("--batch", required=True)
        elif name == "retry":
            command.add_argument("--attempt", required=True)
        else:
            command.add_argument("--attempt", required=True)

    report = commands.add_parser("report")
    report.add_argument("--manifest", required=True)
    report.add_argument("--attempt-root", required=True)
    report.add_argument("--output", required=True)
    release_commands.append(report)

    build_report = commands.add_parser("build-report")
    build_report.add_argument("--manifest", required=True)
    build_report.add_argument("--attempt-root", required=True)
    build_report.add_argument("--resolution", required=True)
    build_report.add_argument("--output", required=True)
    build_report.add_argument("--repository-root", default=".")
    release_commands.append(build_report)

    for command in release_commands:
        command.add_argument("--suite")
        command.add_argument("--chain-profile", action="append", default=[])
        command.add_argument("--treatment-profile", action="append", default=[])

    preview = commands.add_parser("preview")
    preview.add_argument("--attempt-root", required=True)
    preview.add_argument("--output", required=True)
    return parser


def _attempt_root(manifest: CampaignManifest, supplied: str | None) -> Path:
    if supplied is not None:
        return Path(supplied)
    return Path("benchmark-output") / "campaigns" / manifest.campaign_id / "attempts"


def _require_output_outside_store(output: Path | str, store: AttemptStore) -> None:
    try:
        destination = Path(output).resolve(strict=False)
        root = store.root.resolve(strict=False)
    except OSError as exc:
        raise CampaignOperatorError("cannot resolve the output and attempt-store paths") from exc
    if destination == root or destination.is_relative_to(root):
        raise CampaignOperatorError("output must be outside the immutable attempt store")


def _print_plan(
    manifest: CampaignManifest,
    stdout: TextIO,
    release_binding: CampaignReleaseBinding | None = None,
) -> None:
    print(f"CAMPAIGN\t{manifest.campaign_id}\t{manifest.sha256}", file=stdout)
    if release_binding is not None:
        ceilings = release_binding.campaign_ceilings(manifest)
        print(
            "CEILINGS\t"
            f"{artifact_sha256(ceilings)}\t"
            f"{json.dumps(ceilings, sort_keys=True, separators=(',', ':'))}",
            file=stdout,
        )
    print("ORDER\tBATCH\tSLOT\tTASK\tARM\tMODEL VARIANT", file=stdout)
    for index, slot in enumerate(manifest.ordered_slots, start=1):
        print(
            f"{index}\t{slot.batch_id}\t{slot.slot_id}\t{slot.task_id}\t{slot.arm}\t"
            f"{slot.model_variant_id}",
            file=stdout,
        )


def _release_binding_for_command(
    manifest: CampaignManifest,
    args: argparse.Namespace,
    supplied: CampaignReleaseBinding | None,
) -> CampaignReleaseBinding | None:
    paths_supplied = bool(args.suite or args.chain_profile or args.treatment_profile)
    if supplied is not None:
        if paths_supplied:
            raise CampaignOperatorError("release inputs cannot be supplied twice")
        try:
            supplied.validate_manifest(manifest)
        except SuiteReleaseError as exc:
            raise CampaignOperatorError("campaign release binding is invalid") from exc
        return supplied
    major = int(manifest.suite_semver.split(".", 1)[0])
    if not paths_supplied:
        if major >= 4:
            raise CampaignOperatorError("this suite requires its release inputs")
        return None
    if not args.suite or not args.chain_profile or not args.treatment_profile:
        raise CampaignOperatorError("release validation needs suite, chain and treatment profiles")
    try:
        release = load_suite_release(args.suite)
        chains = tuple(load_chain_profile(path) for path in args.chain_profile)
        treatments = tuple(load_treatment_profile(path) for path in args.treatment_profile)
        return validate_campaign_release(
            manifest,
            release,
            chain_profiles=chains,
            treatment_profiles=treatments,
        )
    except SuiteReleaseError as exc:
        raise CampaignOperatorError("campaign release inputs are invalid") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: TaskRuntimeFactory | None = None,
    calibration_runtime: CalibrationRuntimeFactory | None = None,
    retry_wait: Callable[[float], None] = time.sleep,
    release_binding: CampaignReleaseBinding | None = None,
    report_builder_source: ReportBuilderSource | None = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    coordination_root: Path | str = DEFAULT_COORDINATION_ROOT,
) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "tasks":
            try:
                suite = load_suite(args.suite)
            except (RegistryError, OSError) as exc:
                raise CampaignOperatorError("suite registry is invalid") from exc
            print("TASK\tKIND\tSCORE", file=stdout)
            for task in suite.tasks:
                print(f"{task.id}\t{task.kind}\t{task.score}", file=stdout)
            return 0
        if args.command == "freeze":
            release_inputs = bool(args.suite or args.chain_profile or args.treatment_profile)
            if release_inputs:
                if not args.suite or not args.chain_profile or not args.treatment_profile:
                    raise CampaignOperatorError(
                        "release freezing needs suite, chain and treatment profiles"
                    )
                manifest, _binding = freeze_campaign_from_release(
                    args.draft,
                    args.output,
                    suite_root=args.suite,
                    chain_profile_paths=tuple(args.chain_profile),
                    treatment_profile_paths=tuple(args.treatment_profile),
                )
            else:
                manifest = freeze_campaign(args.draft, args.output)
            print(f"frozen campaign {manifest.campaign_id} {manifest.sha256}", file=stdout)
            return 0
        if args.command == "preview":
            store = AttemptStore(args.attempt_root)
            _require_output_outside_store(args.output, store)
            preview = build_exploratory_preview(store)
            publish_document(args.output, preview.to_dict(), "exploratory preview")
            print(f"wrote exploratory preview {preview.sha256}", file=stdout)
            return 0

        manifest = load_campaign(args.manifest)
        command_release_binding = _release_binding_for_command(
            manifest,
            args,
            release_binding,
        )
        if args.command == "plan":
            _print_plan(manifest, stdout, command_release_binding)
            return 0
        if args.command == "calibrate":
            if not args.authorized_by_user:
                raise CampaignOperatorError(
                    "calibration needs explicit live authorization for this invocation"
                )
            if command_release_binding is None:
                raise CampaignOperatorError("calibration needs an immutable release binding")
            if calibration_runtime is None:
                raise CampaignOperatorError("live calibration adapters are not configured")
            store = AttemptStore(args.attempt_root)
            _require_output_outside_store(args.output, store)
            evidence, envelope = run_calibration(
                manifest,
                args.slot,
                args.calibration_id,
                store,
                args.output,
                command_release_binding,
                calibration_runtime,
            )
            print(
                f"{evidence.calibration_id}\t{envelope.intent.attempt_id}\t"
                f"{envelope.result.outcome}\tcleanup={envelope.receipts[-1].status}",
                file=stdout,
            )
            if not evidence.cleanup_complete:
                print("FAIL: calibration cleanup is incomplete", file=stderr)
                return 1
            return 0
        store = AttemptStore(_attempt_root(manifest, getattr(args, "attempt_root", None)))
        if args.command == "report":
            _require_output_outside_store(args.output, store)
            with _campaign_lock(Path(coordination_root)):
                resolution = resolve_accepted_report(manifest, store)
            publish_document(args.output, resolution.to_dict(), "accepted report resolution")
            print(f"wrote accepted report resolution {resolution.sha256}", file=stdout)
            return 0
        if args.command == "build-report":
            _require_output_outside_store(args.output, store)
            resolution = load_report_resolution(args.resolution)
            builder_source = report_builder_source or resolve_report_builder_source(
                args.repository_root
            )
            with _campaign_lock(Path(coordination_root)):
                dataset = build_campaign_report_dataset(
                    manifest,
                    resolution,
                    store,
                    builder_source,
                    command_release_binding,
                )
                dataset_sha256, site_sha256 = publish_campaign_report(args.output, dataset)
            print(
                f"wrote accepted campaign report dataset={dataset_sha256} site={site_sha256}",
                file=stdout,
            )
            return 0
        if runtime is None:
            raise CampaignOperatorError("live campaign adapters are not configured")
        operator = CampaignOperator(
            manifest,
            store,
            runtime,
            coordination_root,
            retry_wait=retry_wait,
            release_binding=command_release_binding,
        )
        if args.command == "run-task":
            envelopes = (operator.run_task(args.slot),)
        elif args.command == "run-batch":
            envelopes = operator.run_batch(args.batch)
        elif args.command == "retry":
            envelopes = (operator.retry(args.attempt),)
        elif args.command == "recover":
            envelopes = (operator.recover(args.attempt),)
        else:
            raise CampaignOperatorError("unsupported campaign command")
        for envelope in envelopes:
            print(
                f"{envelope.intent.attempt_id}\t{envelope.result.outcome}\t"
                f"cleanup={envelope.receipts[-1].status}",
                file=stdout,
            )
        if any(envelope.receipts[-1].status != "complete" for envelope in envelopes):
            print("FAIL: execution paused because cleanup is incomplete", file=stderr)
            return 1
        return 0
    except (
        AttemptSchemaError,
        AttemptStoreError,
        CalibrationError,
        CampaignError,
        CampaignReportError,
        CampaignOperatorError,
        RegistryError,
        SingleTaskExecutionError,
        SuiteReleaseError,
    ) as exc:
        print(f"FAIL: {exc}", file=stderr)
        return 1
    except Exception:
        print("FAIL: campaign command failed safely", file=stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
