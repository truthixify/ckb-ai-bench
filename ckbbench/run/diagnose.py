"""`./bench diagnose`: one isolated, no-grade B-arm diagnostic cell (Task 23, review revision 6).

This path never calls `run_matrix()`, `verify_suite()`, a grader, `RunResult`, result validation,
aggregation or report generation, and no report ever reads its artifact. It exists to answer one
question the accepted evidence cannot: which exception family a provider attempt ended in, whether
the pinned HTTPX handler was entered, and what shape the Responses input had.

Ownership is the load-bearing part. The worker cannot be trusted to clean up after itself, because a
supervisor that kills it at a deadline also kills its `finally` blocks — and the agent container name
is generated inside the worker today, so a killed worker leaves the parent with no selector at all.
Every identity is therefore chosen by the parent before the worker starts, and the parent is the only
process that mutates or publishes anything.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from ckbbench.run.devnet import (
    AGENT_CONTAINER_PREFIX,
    AGENT_SERVICE,
    COMPOSE_PROJECT,
    DATA_VOLUME,
    MINER_SERVICE,
    NODE_SERVICE,
    OWNER_LABELS,
    VALIDATE_RUN_LABEL,
    DevnetLifecycleError,
    DevnetVolumeRetained,
    remove_data_volume,
    _assert_owned_container,
    _container_state,
    _docker_json,
)
from ckbbench.run.diagnostic import (
    MAX_ARTIFACT_BYTES,
    InstrumentationError,
    false_envelope,
    validate_artifact_bytes,
)

DIAGNOSTIC_DEADLINE_S = 600.0
DOCKER_CALL_TIMEOUT_S = 60.0

# The reserve is DERIVED from the worst case it must cover, not chosen. Post-worker cleanup makes at
# most three inspections, three removals and three captured-id absence proofs; termination costs one
# grace window and one reap window. Cleanup calls get their own smaller timeout so the arithmetic
# closes. `test_the_cleanup_reserve_covers_its_own_worst_case` asserts this relationship.
CLEANUP_CALL_TIMEOUT_S = 10.0
MAX_CLEANUP_CALLS = 9
TERMINATE_GRACE_S = 10.0
REAP_WAIT_S = 10.0
CLEANUP_RESERVE_S = (
    MAX_CLEANUP_CALLS * CLEANUP_CALL_TIMEOUT_S + TERMINATE_GRACE_S + REAP_WAIT_S
)
AGENT_NAME_PREFIX = "minisweagent-ckbbench-diagnostic-"
DEVNET_ANONYMOUS_DATA_MOUNT = "/var/lib/ckb/data"
# Read only by the dedicated worker at startup. An ambient value must never let `run`, `smoke`,
# validation or a report command turn diagnostics on.
WORKER_MODE_ENV = "CKBBENCH_DIAGNOSTIC_WORKER"
# The parent passes its already-validated artifact directory as an inherited descriptor.
# A pathname alone would let the two processes act on different objects.
ARTIFACT_FD_ENV = "CKBBENCH_DIAGNOSTIC_ARTIFACT_FD"
RECEIPT_FD_ENV = "CKBBENCH_DIAGNOSTIC_RECEIPT_FD"

# The worker's candidate identity crosses the process boundary through a PARENT-CREATED pipe of a
# fixed size. A pathname cannot carry it: by the time the parent reads the name, the object behind
# it may be a different file. Fixed width so a hostile or broken child cannot make the parent read
# an unbounded amount, and so a short write is detectable.
RECEIPT_PREFIX = b"ckbbench-diagnostic-candidate "
RECEIPT_BYTES = len(RECEIPT_PREFIX) + 20 + 1 + 20 + 1


class DiagnosticAbort(RuntimeError):
    """The run cannot proceed safely. Sanitized: carries no provider, path or credential value."""


class CreatedButNotRolledBack(DiagnosticAbort):
    """A selector was created and could not be removed. Distinct from never having created it."""


# Selector states. "Present but unproved" is deliberately not "absent": only absence permits the
# caller to skip a removal, and only an exact identity match permits one.
ABSENT = "absent"
REGULAR = "regular"
UNPROVED = "unproved"


class Selector(NamedTuple):
    """What one name currently selects, observed once. `links` is 0 unless the state is `REGULAR`."""

    state: str
    identity: tuple[int, int] | None
    links: int


def new_execution_id() -> str:
    """Exactly 32 lower-case hexadecimal characters."""
    return secrets.token_hex(16)


def agent_container_name(execution_id: str) -> str:
    return f"{AGENT_NAME_PREFIX}{execution_id}"


@dataclass(frozen=True)
class DiagnosticIdentity:
    """Every selector the parent must own, fixed before the worker exists."""

    execution_id: str
    run_id: str
    agent_name: str
    labels: tuple[str, ...]
    run_dir: Path
    mount_dir: Path
    allowlist_dir: Path
    allowlist_path: Path
    created_dir: Path
    candidate_path: Path
    candidate_staging_path: Path
    final_path: Path
    final_staging_path: Path

    @classmethod
    def create(cls, *, run_id: str, artifact_root: Path, run_dir: Path,
               execution_id: str | None = None) -> "DiagnosticIdentity":
        # `None` means generate; an explicitly supplied value is validated, never replaced.
        execution_id = new_execution_id() if execution_id is None else execution_id
        if (not isinstance(execution_id, str) or len(execution_id) != 32
                or any(c not in "0123456789abcdef" for c in execution_id)):
            raise DiagnosticAbort("execution id must be 32 lower-case hex characters")
        diagnostic_dir = Path(artifact_root) / "diagnostic"
        run_dir = Path(run_dir)
        candidate = diagnostic_dir / f".{run_id}.diag.json.candidate"
        final = diagnostic_dir / f"{run_id}.diag.json"
        return cls(
            execution_id=execution_id,
            run_id=run_id,
            agent_name=agent_container_name(execution_id),
            labels=(f"{VALIDATE_RUN_LABEL}={execution_id}",),
            run_dir=run_dir,
            mount_dir=run_dir / "mount",
            allowlist_dir=run_dir / "allowlist",
            # Every host leaf is named here, before the first external action: a name derived later
            # is a selector nobody validated.
            allowlist_path=run_dir / "allowlist" / f"allowlist.{execution_id}.built",
            created_dir=run_dir / "created",
            candidate_path=candidate,
            candidate_staging_path=candidate.with_name(candidate.name + ".partial"),
            final_path=final,
            final_staging_path=final.with_name(final.name + ".publishing"),
        )

    def worker_env(self) -> dict[str, str]:
        """The diagnostic-only environment the worker receives.

        Provider credential and base are inherited through the established precedence; nothing new
        is invented here and no unrelated value is copied.
        """
        return {
            WORKER_MODE_ENV: "1",
            "CKBBENCH_VALIDATE_RUN_ID": self.execution_id,
            "CKBBENCH_DEVNET_DATA_MOUNT": DEVNET_ANONYMOUS_DATA_MOUNT,
            "CKBBENCH_DIAGNOSTIC_AGENT_NAME": self.agent_name,
            "CKBBENCH_DIAGNOSTIC_LABELS": ",".join(self.labels),
            "CKBBENCH_DIAGNOSTIC_CANDIDATE": str(self.candidate_path),
            "CKBBENCH_DIAGNOSTIC_RUN_ID": self.run_id,
            "CKBBENCH_DIAGNOSTIC_RUN_DIR": str(self.run_dir),
            "CKBBENCH_DIAGNOSTIC_MOUNT_DIR": str(self.mount_dir),
            "CKBBENCH_DIAGNOSTIC_ALLOWLIST_PATH": str(self.allowlist_path),
            "CKBBENCH_DIAGNOSTIC_CREATED_DIR": str(self.created_dir),
        }


@dataclass
class Deadline:
    """One monotonic budget shared by the worker and every Docker call."""

    total_s: float
    monotonic: Callable[[], float]
    started_at: float | None = None

    def start(self) -> None:
        self.started_at = self.monotonic()

    def remaining(self) -> float:
        if self.started_at is None:
            raise DiagnosticAbort("deadline was not started before the first external action")
        return max(0.0, self.total_s - (self.monotonic() - self.started_at))

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def worker_expired(self) -> bool:
        """The worker's own deadline: the total minus the reserved cleanup slice."""
        return self.remaining() <= CLEANUP_RESERVE_S


def _default_run(argv: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


@dataclass
class CleanupOutcome:
    """What the parent proved about one owned resource."""

    name: str
    action: str                      # inspected | removed | absent | refused | failed
    container_id: str | None = None
    detail: str = ""


@dataclass
class Supervisor:
    """Owns the deadline, the worker process, every resource selector, and publication."""

    identity: DiagnosticIdentity
    deadline: Deadline
    run: Callable[[Sequence[str], float], Any] = _default_run
    kill: Callable[[int, int], None] = os.kill
    sleep: Callable[[float], None] = time.sleep
    outcomes: list[CleanupOutcome] = field(default_factory=list)
    acknowledged: set[str] = field(default_factory=set)
    run_dir_handle: "DirHandle | None" = None
    run_dir_scrubbed: bool = False
    # The inode the worker reported creating, received over the parent-created receipt pipe. None
    # means the parent has no proof of what the candidate selector holds, and it may neither read
    # that selector as this run's output nor remove it.
    candidate_identity: tuple[int, int] | None = None
    artifact_dir: "DirHandle | None" = None
    cleanup_ok: bool = True
    worker_ok: bool = True
    child_reaped: bool = True
    first_failure: str = ""

    # --- docker helpers ---------------------------------------------------------------------

    def _docker(self, argv: Sequence[str], *, cleanup: bool = False) -> Any:
        remaining = self.deadline.remaining()
        if remaining <= 0.0:
            raise DiagnosticAbort("deadline expired before a docker call")
        ceiling = CLEANUP_CALL_TIMEOUT_S if cleanup else DOCKER_CALL_TIMEOUT_S
        return self.run(list(argv), min(ceiling, remaining))

    def _inspect_cleanup(self, name: str) -> dict | None:
        return _container_state(lambda argv: self._docker(argv, cleanup=True), name)

    def _inspect(self, name: str) -> dict | None:
        """Proven-absent is None; anything unclear raises rather than guessing."""
        return _container_state(lambda argv: self._docker(argv), name)

    def _note(self, outcome: CleanupOutcome) -> None:
        self.outcomes.append(outcome)
        if outcome.action in ("refused", "failed"):
            self.cleanup_ok = False
            if not self.first_failure:
                self.first_failure = f"{outcome.name}: {outcome.action}"

    # --- ordinary -> diagnostic transition --------------------------------------------------

    def _running_agents(self) -> list[str]:
        proc = self._docker(["docker", "ps", "--format", "{{.Names}}"])
        if getattr(proc, "returncode", 1) != 0:
            raise DiagnosticAbort("could not list running containers")
        return [
            name for name in (getattr(proc, "stdout", "") or "").split()
            if name.startswith(AGENT_CONTAINER_PREFIX) or name == AGENT_SERVICE
        ]

    def _volume(self, name: str) -> dict | None:
        return _docker_json(
            lambda argv: self._docker(argv),
            ["docker", "volume", "inspect", name, "--format", "{{json .}}"],
            what=name, kind="volume", name=name,
        )

    def inspect_ordinary(self) -> dict[str, Any]:
        """Inventory EVERY ordinary selector before anything destructive happens.

        Under ordinary identity, not the diagnostic one: a pre-existing operator stack carries an
        empty `com.ckbbench.validate-run`, so requiring this run's id here would refuse the very
        resources the transition must remove.
        """
        running = self._running_agents()
        if running:
            # Another cell is live. Removing its DevNet underneath it would disrupt that run.
            self._note(CleanupOutcome("agents", "refused", detail="an agent is running"))
            raise DiagnosticAbort("refusing to transition while a benchmark agent is running")

        found: dict[str, Any] = {}
        for name in (self.identity.agent_name, MINER_SERVICE, NODE_SERVICE):
            found[name] = self._inspect(name)
        if found[self.identity.agent_name] is not None:
            raise DiagnosticAbort("the diagnostic agent name is already taken")

        for name in (MINER_SERVICE, NODE_SERVICE):
            payload = found[name]
            if payload is None:
                continue
            try:
                _assert_owned_container(name, payload)
            except DevnetLifecycleError as exc:
                self._note(CleanupOutcome(name, "refused", detail=str(exc)[:200]))
                raise DiagnosticAbort("an ordinary devnet resource is not benchmark-owned") from None
            if (payload.get("State") or {}).get("Running"):
                self._note(CleanupOutcome(name, "refused", detail="running"))
                raise DiagnosticAbort("an ordinary devnet service is still running")

        payload = self._volume(DATA_VOLUME)
        found["__volume__"] = payload
        if payload is not None:
            labels = payload.get("Labels") or {}
            if payload.get("Name") != DATA_VOLUME:
                self._note(CleanupOutcome(DATA_VOLUME, "refused", detail="name mismatch"))
                raise DiagnosticAbort("the ordinary devnet volume identity does not match")
            if any(labels.get(k) != v for k, v in OWNER_LABELS.items()):
                self._note(CleanupOutcome(DATA_VOLUME, "refused", detail="foreign volume"))
                raise DiagnosticAbort("the ordinary devnet volume is not benchmark-owned")
            # ALL containers, not just running ones: a stopped foreign user discovered after
            # node/miner deletion is exactly the partial transition this ordering prevents.
            #
            # The ordinary node and miner ARE legitimate users of this volume — refusing every user
            # rejected exactly the retained ordinary state this transition exists to replace. Only
            # the already-inspected, ownership-proved services are allowed.
            allowed = {
                payload.get("Id") for name, payload in found.items()
                if name in (MINER_SERVICE, NODE_SERVICE) and isinstance(payload, dict)
            } - {None}
            foreign = [uid for uid in self._volume_users(DATA_VOLUME) if uid not in allowed]
            if foreign:
                self._note(CleanupOutcome(DATA_VOLUME, "refused", detail="foreign volume user"))
                raise DiagnosticAbort("the ordinary devnet volume has an unproved user")
        return found

    def _volume_users(self, volume: str) -> list[str]:
        """Immutable container IDs, not names: a name proves nothing about which object mounts it."""
        proc = self._docker(
            # --no-trunc: docker's default formatted ID is shortened, and `inspect` returns the
            # full 64-character one. Comparing the two forms rejected our own proved containers.
            ["docker", "ps", "-a", "--no-trunc",
             "--filter", f"volume={volume}", "--format", "{{.ID}}"]
        )
        if getattr(proc, "returncode", 1) != 0:
            raise DiagnosticAbort("could not inventory the devnet volume's users")
        return [value for value in (getattr(proc, "stdout", "") or "").split() if value]

    def transition_ordinary(self, found: dict[str, Any]) -> None:
        """Remove proved ordinary containers by immutable id, then the proved ordinary volume."""
        for name in (MINER_SERVICE, NODE_SERVICE):
            payload = found.get(name)
            if payload is None:
                self._note(CleanupOutcome(name, "absent"))
                continue
            container_id = payload.get("Id")
            if not container_id:
                self._note(CleanupOutcome(name, "refused", detail="no immutable id"))
                raise DiagnosticAbort("could not read an ordinary container id")
            proc = self._docker(["docker", "rm", "-f", container_id])
            if getattr(proc, "returncode", 1) != 0:
                self._note(CleanupOutcome(name, "failed", container_id))
                raise DiagnosticAbort("an ordinary container could not be removed")
            # By captured ID: a replacement occupying the reusable name is not proof of removal.
            if self._inspect(container_id) is not None:
                self._note(CleanupOutcome(name, "failed", container_id, "still present"))
                raise DiagnosticAbort("an ordinary container removal was not proved")
            self._note(CleanupOutcome(name, "removed", container_id))

        if found.get("__volume__") is None:
            self._note(CleanupOutcome(DATA_VOLUME, "absent"))
            return
        # The tracked checked path: it revalidates name, owner labels and zero remaining users
        # immediately before the name-selected mutation, which is the only moment that check is
        # meaningful.
        try:
            remove_data_volume(run=lambda argv: self._docker(argv), volume=DATA_VOLUME)
        except DevnetVolumeRetained:
            self._note(CleanupOutcome(DATA_VOLUME, "refused", detail="retained by the lifecycle"))
            raise DiagnosticAbort("the ordinary devnet volume was retained") from None
        except DevnetLifecycleError as exc:
            self._note(CleanupOutcome(DATA_VOLUME, "failed", detail=str(exc)[:200]))
            raise DiagnosticAbort("the ordinary devnet volume could not be removed") from None
        if self._volume(DATA_VOLUME) is not None:
            self._note(CleanupOutcome(DATA_VOLUME, "failed", detail="still present"))
            raise DiagnosticAbort("the ordinary devnet volume removal was not proved")
        self._note(CleanupOutcome(DATA_VOLUME, "removed"))

    # --- post-worker cleanup ----------------------------------------------------------------

    def note_created(self, name: str) -> None:
        """A parent-observable creation acknowledgement.

        Without this, a later absence is ambiguous: never created and created-then-vanished look
        identical, and only the second is a failure.
        """
        self.acknowledged.add(name)

    def cleanup_diagnostic(self, *, expected_agent_image: str) -> None:
        """Inspect all three, validate, then mutate. Absence is proved by captured ID, not by name.

        The frozen image is REQUIRED: an optional check is one the production caller can forget.
        """
        if not expected_agent_image:
            raise DiagnosticAbort("the frozen agent image is required for cleanup")
        expect = self.identity.execution_id
        payloads: dict[str, dict | None] = {}
        for name in (self.identity.agent_name, MINER_SERVICE, NODE_SERVICE):
            try:
                payloads[name] = self._inspect_cleanup(name)
            except (DevnetLifecycleError, DiagnosticAbort) as exc:
                payloads[name] = None
                self._note(CleanupOutcome(name, "failed", detail=str(exc)[:200]))

        marker_for = {self.identity.agent_name: "agent", MINER_SERVICE: "miner",
                      NODE_SERVICE: "node"}
        self.acknowledged |= {
            key for key, marker in marker_for.items()
            if marker in read_created(self.identity.created_dir)
        }
        proved: dict[str, str] = {}
        for name, payload in payloads.items():
            if payload is None:
                if name in self.acknowledged:
                    # Created, then gone without a parent action: not the same as never created.
                    self._note(CleanupOutcome(name, "failed", detail="disappeared unexplained"))
                else:
                    self._note(CleanupOutcome(name, "absent"))
                continue
            labels = (payload.get("Config") or {}).get("Labels") or {}
            if labels.get(VALIDATE_RUN_LABEL) != expect:
                self._note(CleanupOutcome(name, "refused", detail="foreign execution label"))
                continue
            if name in (MINER_SERVICE, NODE_SERVICE):
                if (labels.get("com.docker.compose.project") != COMPOSE_PROJECT
                        or labels.get("com.docker.compose.service") != name):
                    self._note(CleanupOutcome(name, "refused", detail="foreign compose identity"))
                    continue
            else:
                image = payload.get("Image")
                if image != expected_agent_image:
                    self._note(CleanupOutcome(name, "refused", detail="foreign agent image"))
                    continue
            container_id = payload.get("Id")
            if not container_id:
                self._note(CleanupOutcome(name, "refused", detail="no immutable id"))
                continue
            proved[name] = container_id

        for name in (self.identity.agent_name, MINER_SERVICE):
            container_id = proved.get(name)
            if container_id is None:
                continue
            self._remove(name, ["docker", "rm", "-f", container_id], container_id)
        node_id = proved.get(NODE_SERVICE)
        if node_id is not None:
            # ONE operation: `-v` disposes the anonymous data volume through this same immutable
            # selector. Removing the node first and then addressing its volume by name is exactly
            # the race the anonymous volume exists to avoid.
            self._remove(NODE_SERVICE, ["docker", "rm", "-fv", node_id], node_id)

        for name, container_id in proved.items():
            # By captured ID, not by the reusable name: a replacement could occupy the name while
            # the object we removed still exists.
            try:
                if self._inspect_cleanup(container_id) is not None:
                    self._note(CleanupOutcome(name, "failed", container_id, "still present"))
            except (DevnetLifecycleError, DiagnosticAbort):
                self._note(CleanupOutcome(name, "failed", container_id, "absence unproved"))

    def _remove(self, name: str, argv: Sequence[str], container_id: str) -> None:
        try:
            proc = self._docker(argv, cleanup=True)
        except (DevnetLifecycleError, DiagnosticAbort) as exc:
            self._note(CleanupOutcome(name, "failed", container_id, str(exc)[:200]))
            return
        if getattr(proc, "returncode", 1) != 0:
            self._note(CleanupOutcome(name, "failed", container_id))
            return
        self._note(CleanupOutcome(name, "removed", container_id))

    # --- worker supervision -----------------------------------------------------------------

    def supervise(self, spawn: Callable[[], Any]) -> tuple[int | None, bool]:
        """Run the worker under its sub-budget, then guarantee it is dead before returning.

        Every exceptional path — poll, signal or reap failure — still runs the terminate/kill/reap
        sequence. Returning while a spawned child might still be making provider, Docker or
        filesystem changes is the one outcome this must never produce.
        """
        try:
            proc = spawn()
        except Exception:
            self.worker_ok = False
            self._note(CleanupOutcome("worker", "failed", detail="spawn failed"))
            return None, False
        # From here the child exists; it must be proved dead before anything else may proceed.
        self.child_reaped = False

        timed_out = False
        try:
            while True:
                try:
                    code = proc.poll()
                except Exception:
                    self.worker_ok = False
                    self._note(CleanupOutcome("worker", "failed", detail="poll failed"))
                    break
                if code is not None:
                    if code != 0:
                        self.worker_ok = False
                        self._note(CleanupOutcome("worker", "failed", detail="nonzero exit"))
                    self.child_reaped = True
                    return code, timed_out
                if self.deadline.worker_expired():
                    timed_out = True
                    self.worker_ok = False
                    self._note(CleanupOutcome("worker", "failed", detail="deadline"))
                    break
                self.sleep(0.05)
        finally:
            pass
        return self._terminate(proc), timed_out

    def _terminate(self, proc: Any) -> int | None:
        """SIGTERM, bounded grace, SIGKILL, reap. Failures are recorded, never raised."""
        self._signal(proc, signal.SIGTERM)
        grace_end = self.deadline.monotonic() + TERMINATE_GRACE_S
        while self.deadline.monotonic() < grace_end:
            try:
                code = proc.poll()
            except Exception:
                break
            if code is not None:
                self.child_reaped = True
                return code
            self.sleep(0.05)
        self._signal(proc, signal.SIGKILL)
        try:
            code = proc.wait(timeout=REAP_WAIT_S)
        except Exception:
            self.worker_ok = False
            self._note(CleanupOutcome("worker", "failed", detail="unreaped"))
            return None
        self.child_reaped = True
        return code

    def _signal(self, proc: Any, sig: int) -> bool:
        """Best effort: a failed signal must not stop the rest of the termination sequence."""
        try:
            self.kill(proc.pid, sig)
            return True
        except Exception:
            self.worker_ok = False
            self._note(CleanupOutcome("worker", "failed", detail="signal failed"))
            return False

    # --- publication ------------------------------------------------------------------------

    def scrub_run_dir_once(self) -> None:
        """Scrub the run directory exactly once, whatever happens to publication afterwards.

        Raw cleanup was reachable only through `publish()`, so a canonical-path mismatch, a planted
        staging selector or an expired deadline each ended the command with raw run data intact.
        """
        if self.run_dir_scrubbed:
            return
        self.run_dir_scrubbed = True
        self.remove_run_dir()

    def publish(self) -> bytes:
        """Publish through the retained artifact handle, into the canonical path or not at all.

        Ownership is by INODE, not by bytes: a replacement carrying identical bytes is not the file
        this transaction created.
        """
        artifact_dir = self.artifact_dir
        if artifact_dir is None:
            raise DiagnosticAbort("no retained artifact directory handle")
        canonical = self.identity.final_path.parent
        staging = self.identity.final_staging_path.name
        candidate = self.identity.candidate_path.name
        candidate_staging = self.identity.candidate_staging_path.name
        final = self.identity.final_path.name

        if not artifact_dir.occupies(canonical):
            # Nothing has been created by this invocation yet, so nothing may be withdrawn: a
            # matching leaf inside the retained directory is not evidence that it is ours.
            self._note(CleanupOutcome("artifact_dir", "failed", detail="moved or replaced"))
            raise DiagnosticAbort("the artifact directory is no longer at its canonical path")

        candidate_payload = None
        if self.cleanup_ok and self.worker_ok:
            try:
                candidate_payload = self._read_candidate(artifact_dir)
            except (InstrumentationError, DiagnosticAbort, OSError, ValueError):
                candidate_payload = None

        # A pre-existing staging name is a planted selector: refused, never deleted.
        artifact_dir.refuse_existing(staging)
        self._retire_candidate_selectors(artifact_dir, candidate, candidate_staging)

        payload = (candidate_payload if (candidate_payload is not None and self.cleanup_ok)
                   else false_envelope(self.identity.run_id))

        # The initial staging write is INSIDE the transaction: a created-but-unrollable staging file
        # must be recorded and must refuse publication, not merely raise.
        try:
            created = artifact_dir.write_exclusive_identified(staging, payload)
        except CreatedButNotRolledBack:
            self._note(CleanupOutcome("final_staging", "failed", detail="rollback"))
            raise DiagnosticAbort("the final staging selector could not be rolled back") from None

        linked = False
        try:
            artifact_dir.link(staging, final)
            linked = True
            # The linked name must select the file this transaction created, by inode.
            if not artifact_dir.selects(final, created):
                raise DiagnosticAbort("the final name does not select this transaction's file")
            if not artifact_dir.unlink(staging):
                raise DiagnosticAbort("the final staging selector could not be removed")
            installed = artifact_dir.read_verified(final, identity=created,
                                                   max_bytes=MAX_ARTIFACT_BYTES)
            if installed != payload:
                raise DiagnosticAbort("the published artifact does not match what was written")
            validate_artifact_bytes(installed, run_id=self.identity.run_id)
            # Immediately before returning healthy: the directory is still canonical AND the name
            # still selects this exact inode, re-read through its own descriptor.
            if not artifact_dir.occupies(canonical):
                raise DiagnosticAbort("the artifact directory moved during publication")
            if artifact_dir.read_verified(final, identity=created,
                                          max_bytes=MAX_ARTIFACT_BYTES) != payload:
                raise DiagnosticAbort("the canonical final changed after validation")
        except (DiagnosticAbort, InstrumentationError):
            owned = (staging, final)
            self._withdraw_created(artifact_dir, staging, created, owned_names=owned)
            if linked:
                self._withdraw_created(artifact_dir, final, created, owned_names=owned)
            raise DiagnosticAbort("diagnostic publication could not be completed") from None
        return installed

    def _withdraw_created(self, directory: "DirHandle", name: str, created: tuple[int, int],
                          *, owned_names: Sequence[str] = ()) -> None:
        """Remove `name` only while it still selects the file this transaction created.

        Absence means there is nothing to withdraw. Anything else that is not this exact file is a
        replacement or an unproved selector: it is left alone and reported as not removed, because
        the file this transaction created is then unaccounted for.

        Removing our own name does not destroy the file when something else links to the same inode.
        `owned_names` are the selectors this transaction may legitimately hold at this moment — the
        staging name and its target during the publication window — so a link beyond those is an
        outside alias that keeps the created inode alive after withdrawal.
        """
        found = directory.probe(name)
        if found.state == ABSENT:
            return
        if found.state != REGULAR or found.identity != created:
            self._note(CleanupOutcome(name, "failed", detail="not withdrawn"))
            return
        if found.links > _owned_links(directory, owned_names or (name,), created):
            self._note(CleanupOutcome(name, "failed", detail="alias survives"))
        if not directory.unlink(name):
            self._note(CleanupOutcome(name, "failed", detail="rollback"))

    def remove_run_dir(self) -> None:
        """Empty the run directory through the handle opened before the first external action.

        Absence or an identity mismatch at the canonical path is an unexplained disappearance, not a
        clean outcome: the raw contents may still exist somewhere under a moved name.
        """
        handle = self.run_dir_handle
        if handle is None:
            self._note(CleanupOutcome("run_dir", "failed", detail="no retained handle"))
            return
        run_dir = self.identity.run_dir
        try:
            current = os.stat(str(run_dir), follow_symlinks=False)
            canonical = (current.st_dev, current.st_ino)
        except OSError:
            canonical = None
        if canonical != handle.identity:
            # Moved away or replaced: whatever raw data it held is unaccounted for.
            self._note(CleanupOutcome("run_dir", "failed", detail="moved or replaced"))
        if not _scrub_at(handle.fd):
            self._note(CleanupOutcome("run_dir", "failed"))
            return
        # Entries are deliberately left in place: removing any of them would mean naming a reusable
        # pathname after the verified handle stops being authoritative. Success is that no raw bytes
        # remain, proved through the same handle.
        if _raw_bytes_remain(handle.fd):
            self._note(CleanupOutcome("run_dir", "failed", detail="raw content survived"))

    def _read_candidate(self, artifact_dir: "DirHandle") -> bytes:
        """Read the candidate only through the identity the worker reported creating.

        Without the receipt the parent knows a name, not a file. A different inode carrying valid
        bytes was accepted and published as this run's evidence, which it was not.
        """
        if self.candidate_identity is None:
            raise DiagnosticAbort("no candidate receipt was received from the worker")
        payload = artifact_dir.read_verified(self.identity.candidate_path.name,
                                             identity=self.candidate_identity,
                                             max_bytes=MAX_ARTIFACT_BYTES)
        validate_artifact_bytes(payload, run_id=self.identity.run_id)
        return payload

    def _retire_candidate_selectors(self, artifact_dir: "DirHandle", candidate: str,
                                    candidate_staging: str) -> None:
        """Remove the worker's selectors only where ownership is proved; never by name alone.

        A receipt proves the candidate inode, and a receipt also implies the worker completed its
        transaction, so a surviving staging selector at that point is not this run's.
        """
        found = artifact_dir.probe(candidate)
        proved = (self.candidate_identity is not None
                  and found.state == REGULAR and found.identity == self.candidate_identity)
        if self.candidate_identity is not None and found.state == ABSENT:
            # The receipt proves the worker completed this exact file, and the parent has not yet
            # removed it. Gone anyway is a disappearance, not "never created".
            self._note(CleanupOutcome("candidate", "failed", detail="disappeared unexplained"))
        elif found.state == ABSENT:
            pass
        elif proved:
            # `candidate` is the only name this transaction owns here; the worker already removed
            # its staging link. Anything beyond it survives our removal.
            if found.links > 1:
                self._note(CleanupOutcome("candidate", "failed", detail="alias survives"))
            if not artifact_dir.unlink(candidate):
                self._note(CleanupOutcome("candidate", "failed"))
        else:
            self._note(CleanupOutcome("candidate", "failed", detail="unproved selector"))

        if artifact_dir.probe(candidate_staging)[0] != ABSENT:
            # The worker rolls its own staging selector back; anything left here is unaccounted for
            # and is not removed by a parent that cannot prove it created it.
            self._note(CleanupOutcome("candidate_staging", "failed", detail="unproved selector"))


def _scrub_at(dir_fd: int) -> bool:
    """Destroy the CONTENT of everything under an open directory, never deleting by name.

    `unlink(name)` and `rmdir(name)` re-resolve a reusable pathname after the identity proof stops
    being authoritative, so a replacement arriving in between is what actually gets removed — and the
    original survives elsewhere with its raw bytes intact. Both were reproduced.

    Content is therefore truncated through a descriptor whose identity is compared to the entry that
    was selected, and directory entries are deliberately LEFT in place, exactly as the run-directory
    root is. What must not survive is raw data, and that is what this removes.
    """
    try:
        entries = os.listdir(dir_fd)
    except OSError:
        return False
    ok = True
    for entry in entries:
        try:
            selected = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            ok = False
            continue
        if stat.S_ISDIR(selected.st_mode):
            try:
                child = os.open(entry, os.O_RDONLY | os.O_DIRECTORY
                                | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except OSError:
                ok = False
                continue
            try:
                opened = os.fstat(child)
                if (opened.st_dev, opened.st_ino) != (selected.st_dev, selected.st_ino):
                    ok = False
                    continue
                ok = _scrub_at(child) and ok
            finally:
                os.close(child)
        elif stat.S_ISREG(selected.st_mode):
            if selected.st_nlink != 1:
                # A file outside the run directory can be hard-linked in; truncating through our
                # link destroys the outside file's data. That is a foreign mutation, not cleanup.
                ok = False
                continue
            try:
                # O_NONBLOCK: if this name is replaced by a FIFO between `stat` and `open`, a
                # blocking open would wait for a reader forever, outside the deadline machinery.
                fd = os.open(entry, os.O_WRONLY | os.O_NONBLOCK
                             | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except OSError:
                ok = False
                continue
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode):
                    ok = False          # replaced by something that is not a regular file
                    continue
                if (opened.st_dev, opened.st_ino) != (selected.st_dev, selected.st_ino):
                    ok = False
                    continue
                if opened.st_nlink != 1:
                    ok = False          # linked elsewhere between the stat and the open
                    continue
                os.ftruncate(fd, 0)
            except OSError:
                ok = False
            finally:
                os.close(fd)
        else:
            # A symlink, FIFO, socket or device is not something this run created and cannot be
            # proved clean. Unproved is not clean.
            ok = False
    return ok


def _raw_bytes_remain(dir_fd: int) -> bool:
    """True when any regular file under this open directory still holds content."""
    try:
        entries = os.listdir(dir_fd)
    except OSError:
        return True
    for entry in entries:
        try:
            info = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            return True
        if stat.S_ISREG(info.st_mode):
            if info.st_size > 0 or info.st_nlink != 1:
                return True
        elif stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(entry, os.O_RDONLY | os.O_DIRECTORY
                                | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except OSError:
                return True
            try:
                if _raw_bytes_remain(child):
                    return True
            finally:
                os.close(child)
        else:
            # An unproved or special entry is an incomplete proof, not a clean one.
            return True
    return False


class DirHandle:
    """One open directory descriptor, used for EVERY operation inside it.

    Reopening the pathname per call is check-then-use however carefully each call is written: a
    directory swapped between two of them is a different directory. Holding one descriptor makes
    every later decision refer to the object that was validated.
    """

    def __init__(self, path: Path, *, expect_identity: tuple[int, int] | None = None) -> None:
        self.path = Path(path)
        self.fd = _open_dir(self.path)
        info = os.fstat(self.fd)
        self.identity = (info.st_dev, info.st_ino)
        if expect_identity is not None and self.identity != expect_identity:
            self.close()
            raise DiagnosticAbort("the diagnostic directory is not the validated one")

    @classmethod
    def adopt(cls, fd: int) -> "DirHandle":
        """Take ownership of an INHERITED descriptor.

        The worker receives the parent's already-validated directory this way. Reopening the
        pathname instead would let the two processes act on different objects.
        """
        handle = cls.__new__(cls)
        try:
            info = os.fstat(fd)
        except OSError:
            raise DiagnosticAbort("the inherited diagnostic descriptor is unusable") from None
        if not stat.S_ISDIR(info.st_mode):
            raise DiagnosticAbort("the inherited diagnostic descriptor is not a directory")
        handle.path = Path(f"<inherited fd {fd}>")
        handle.fd = fd
        handle.identity = (info.st_dev, info.st_ino)
        return handle

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "DirHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def occupies(self, path: Path) -> bool:
        """Whether this retained object is still the directory at `path`."""
        try:
            info = os.stat(str(path), follow_symlinks=False)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) == self.identity

    def refuse_existing(self, name: str) -> None:
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise DiagnosticAbort("could not check a diagnostic destination") from None
        raise DiagnosticAbort("refusing to overwrite an existing diagnostic file")

    def write_exclusive(self, name: str, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        except FileExistsError:
            raise DiagnosticAbort("refusing to overwrite an existing diagnostic file") from None
        except OSError:
            raise DiagnosticAbort("could not create the diagnostic file safely") from None
        try:
            _write_all(fd, payload)
        except DiagnosticAbort:
            # A rollback that itself fails must be reported, not swallowed.
            if not self.unlink(name):
                raise DiagnosticAbort(
                    "a partial diagnostic file could not be rolled back"
                ) from None
            raise
        finally:
            os.close(fd)

    def link(self, source: str, target: str) -> None:
        try:
            os.link(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False)
        except FileExistsError:
            raise DiagnosticAbort("refusing to replace an existing diagnostic artifact") from None
        except OSError:
            raise DiagnosticAbort("could not publish the diagnostic artifact") from None

    def read(self, name: str, *, max_bytes: int) -> bytes:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=self.fd)
        except OSError:
            raise DiagnosticAbort("the file is missing or not a regular file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DiagnosticAbort("not a regular file")
            if info.st_nlink != 1:
                raise DiagnosticAbort("the file is hard-linked")
            if info.st_size > max_bytes:
                raise DiagnosticAbort("the file exceeds the byte ceiling")
            return os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)

    def probe(self, name: str) -> "Selector":
        """Tri-state selector state: `ABSENT`, `REGULAR` with its identity, or `UNPROVED`.

        Collapsing "not there" and "there but not a regular file" into one answer let a foreign
        symlink or FIFO be treated as absence — and absence is the one state that permits an unlink.
        `O_NONBLOCK` because an `O_RDONLY` open of a writer-less FIFO blocks indefinitely.

        The link count comes from the SAME `fstat` as the identity: reading it separately would
        reopen the name after the identity proof stopped being authoritative.
        """
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, dir_fd=self.fd)
        except FileNotFoundError:
            return Selector(ABSENT, None, 0)
        except OSError:
            # ELOOP on a symlink, ENXIO on a device, or anything else we cannot open: present as
            # far as this directory is concerned, and unproved.
            return Selector(UNPROVED, None, 0)
        try:
            info = os.fstat(fd)
        except OSError:
            return Selector(UNPROVED, None, 0)
        finally:
            os.close(fd)
        if not stat.S_ISREG(info.st_mode):
            return Selector(UNPROVED, None, 0)
        return Selector(REGULAR, (info.st_dev, info.st_ino), info.st_nlink)

    def selects(self, name: str, identity: tuple[int, int]) -> bool:
        """Whether `name` still selects exactly the regular file `identity`.

        Deliberately says nothing about the link count: the one place this is asked, the staging
        name and the target legitimately name the same inode.
        """
        found = self.probe(name)
        return found.state == REGULAR and found.identity == identity

    def read_verified(self, name: str, *, identity: tuple[int, int], max_bytes: int,
                      expect_links: int = 1) -> bytes:
        """Read `name` only if it still selects exactly `identity` with the expected link count.

        `expect_links` is 1 everywhere the staging selector has already been removed. A second link
        is a second path able to mutate the artifact after it was validated.
        """
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                         dir_fd=self.fd)
        except OSError:
            raise DiagnosticAbort("the file is missing or not a regular file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DiagnosticAbort("not a regular file")
            if (info.st_dev, info.st_ino) != identity:
                raise DiagnosticAbort("the name no longer selects this transaction's file")
            if info.st_nlink != expect_links:
                raise DiagnosticAbort("the file has an unexpected link count")
            if info.st_size > max_bytes:
                raise DiagnosticAbort("the file exceeds the byte ceiling")
            return os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)

    def write_exclusive_identified(self, name: str, payload: bytes) -> tuple[int, int]:
        """Create exclusively and return the identity of the file THIS call created.

        A rollback that would delete a replacement is not a rollback. If the name no longer selects
        the created file, the created file is unaccounted for and the caller must be told so.
        """
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=self.fd)
        except FileExistsError:
            raise DiagnosticAbort("refusing to overwrite an existing diagnostic file") from None
        except OSError:
            raise DiagnosticAbort("could not create the diagnostic file safely") from None
        try:
            try:
                info = os.fstat(fd)
            except OSError:
                created = None
            else:
                created = (info.st_dev, info.st_ino)
            if created is None:
                raise CreatedButNotRolledBack("the created file could not be identified")
            _write_all(fd, payload)
            return created
        except DiagnosticAbort:
            self.roll_back_created(name, created)
            raise
        finally:
            os.close(fd)

    def roll_back_created(self, name: str, created: tuple[int, int] | None) -> None:
        """Remove `name` only while it still selects `created`; otherwise report it stranded."""
        found = self.probe(name)
        if found.state == ABSENT:
            return
        if found.state != REGULAR or found.identity != created:
            raise CreatedButNotRolledBack(
                "the created file was replaced before it could be rolled back"
            ) from None
        if not self.unlink(name):
            raise CreatedButNotRolledBack("a partial file could not be rolled back") from None

    def unlink(self, name: str) -> bool:
        try:
            os.unlink(name, dir_fd=self.fd)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False


def _owned_links(directory: "DirHandle", names: Sequence[str],
                 created: tuple[int, int]) -> int:
    """How many of this transaction's own names currently select `created`."""
    return sum(1 for name in names if directory.probe(name).identity == created)


def _write_all(fd: int, payload: bytes) -> None:
    """`os.write` may accept fewer bytes than offered; a single call can publish a truncated file."""
    written = 0
    while written < len(payload):
        try:
            count = os.write(fd, payload[written:])
        except OSError:
            raise DiagnosticAbort("the diagnostic file could not be written") from None
        if count <= 0:
            raise DiagnosticAbort("the diagnostic file write made no progress")
        written += count


def _open_dir(path: Path) -> int:
    """A no-follow directory handle. Every later decision is made against this fd, not a pathname."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise DiagnosticAbort("the diagnostic directory is unusable or not a real directory") from None
    return fd


def prepare_directory(path: Path, *, exclusive: bool = False) -> None:
    """Validate every component and create the directory.

    `exclusive=True` means this run must be the one that creates it: an existing directory is
    somebody else's and is refused, so a later `rmtree` cannot delete a stranger's files.
    """
    path = Path(path)
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise DiagnosticAbort("refusing a symlinked diagnostic path component")
        if parent == parent.parent:
            break
    try:
        if exclusive:
            path.mkdir(parents=True, exist_ok=False)
        else:
            path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        raise DiagnosticAbort("refusing a pre-existing diagnostic directory") from None
    except OSError:
        raise DiagnosticAbort("could not create the diagnostic directory") from None
    fd = _open_dir(path)
    os.close(fd)


def write_exclusive(directory: Path, name: str, payload: bytes) -> None:
    """Create `name` inside `directory` exclusively, anchored to a directory fd.

    `O_EXCL | O_NOFOLLOW` against a `dir_fd` means a pre-existing file, a symlink planted at the
    name, or a directory swapped in after the check cannot be followed or clobbered: a
    check-then-use pathname walk cannot make that guarantee.
    """
    dir_fd = _open_dir(Path(directory))
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        except FileExistsError:
            raise DiagnosticAbort("refusing to overwrite an existing diagnostic file") from None
        except OSError:
            raise DiagnosticAbort("could not create the diagnostic file safely") from None
        try:
            _write_all(fd, payload)
        except DiagnosticAbort:
            # A partial file this call created must not survive its own failure, and a rollback that
            # itself fails must be reported rather than swallowed.
            try:
                os.unlink(name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            except OSError:
                raise DiagnosticAbort(
                    "a partial diagnostic file could not be rolled back"
                ) from None
            raise
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def link_within(directory: Path, source: str, target: str) -> None:
    """Atomic NO-CLOBBER publication: `link()` fails if the target already exists.

    `replace()` would silently overwrite a file that appeared after any earlier existence check.
    """
    dir_fd = _open_dir(Path(directory))
    try:
        try:
            os.link(source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
        except FileExistsError:
            raise DiagnosticAbort("refusing to replace an existing diagnostic artifact") from None
        except OSError:
            raise DiagnosticAbort("could not publish the diagnostic artifact") from None
    finally:
        os.close(dir_fd)


def read_exclusive(directory: Path, name: str, *, max_bytes: int) -> bytes:
    """Read a regular, non-symlinked file anchored to the directory fd, bounded by `max_bytes`."""
    dir_fd = _open_dir(Path(directory))
    try:
        try:
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
        except OSError:
            raise DiagnosticAbort("the candidate is missing or not a regular file") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise DiagnosticAbort("the candidate is not a regular file")
            if info.st_nlink != 1:
                raise DiagnosticAbort("the candidate is hard-linked")
            if info.st_size > max_bytes:
                raise DiagnosticAbort("the candidate exceeds the byte ceiling")
            return os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def refuse_existing(directory: Path, name: str) -> None:
    """A pre-existing final is not ours to replace, symlink or not."""
    dir_fd = _open_dir(Path(directory))
    try:
        try:
            os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError:
            raise DiagnosticAbort("could not check the diagnostic destination") from None
        raise DiagnosticAbort("refusing to replace an existing diagnostic artifact")
    finally:
        os.close(dir_fd)


def unlink_within(directory: Path, name: str) -> bool:
    """True when the name is gone afterwards. A silently swallowed failure would let a run publish
    successfully while retaining its staging or candidate leaf."""
    dir_fd = _open_dir(Path(directory))
    try:
        try:
            os.unlink(name, dir_fd=dir_fd)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
    finally:
        os.close(dir_fd)


def write_candidate(path: Path, payload: bytes,
                    *, directory: "DirHandle | None" = None) -> tuple[int, int]:
    """Atomic install of the complete bounded bytes into an ALREADY-VALIDATED directory.

    Both selectors are parent-chosen and must be FREE: deleting a pre-existing staging file or
    replacing a pre-existing candidate would clobber something this run does not own.

    `directory` is the shared handle. The worker passes the descriptor it inherited from the parent,
    so both processes write into the same object rather than into whatever occupies the pathname.
    """
    path = Path(path)
    staging = path.name + ".partial"
    owned = directory is None
    directory = directory if directory is not None else DirHandle(path.parent)
    try:
        directory.refuse_existing(staging)
        directory.refuse_existing(path.name)
        created = directory.write_exclusive_identified(staging, payload)
        # Every failure after the staging file exists rolls it back through the same handle, and the
        # candidate only while that name still selects the file THIS call created. Matching bytes
        # are not ownership: a replacement carrying identical bytes is a different file.
        linked = False
        try:
            directory.link(staging, path.name)
            linked = True
            if not directory.selects(path.name, created):
                raise DiagnosticAbort("the candidate name does not select this call's file")
            if not directory.unlink(staging):
                raise DiagnosticAbort("the candidate staging selector could not be removed")
            if directory.read_verified(path.name, identity=created,
                                       max_bytes=MAX_ARTIFACT_BYTES) != payload:
                raise DiagnosticAbort("the candidate does not match what was written")
            return created
        except DiagnosticAbort:
            # Each selector is withdrawn only while it still selects the file this call created.
            # A replacement or an unproved entry is left alone and reported stranded instead. Both
            # are attempted even when the first is stranded, so one failure cannot hide the other.
            stranded = False
            for selector in (staging,) + ((path.name,) if linked else ()):
                try:
                    directory.roll_back_created(selector, created)
                except CreatedButNotRolledBack:
                    stranded = True
            if stranded:
                raise CreatedButNotRolledBack("the candidate could not be rolled back") from None
            raise
    finally:
        if owned:
            directory.close()


CREATION_MARKERS = ("agent", "miner", "node")


# Multi-byte on purpose: a one-byte marker cannot be partially written, so a short-write
# failure could never be distinguished from a complete one.
MARKER_BYTES = b"created\n"


def mark_created(created_dir: Path, name: str) -> None:
    """Worker-side: record that a real resource was created, as a durable parent-readable fact.

    A file, not an in-memory flag: the parent must be able to tell "never created" from "created and
    then vanished" even when the worker was killed before it could report anything.

    Fails CLOSED. Swallowing every error as "already recorded" would lose creation state on an I/O,
    permission or directory-identity failure; only an existing regular marker with this run's exact
    contents is accepted as already written.
    """
    created_dir = Path(created_dir)
    if name not in CREATION_MARKERS:
        raise DiagnosticAbort("unknown creation marker")
    try:
        write_exclusive(created_dir, name, MARKER_BYTES)
        return
    except DiagnosticAbort:
        pass
    try:
        existing = read_exclusive(created_dir, name, max_bytes=len(MARKER_BYTES) + 1)
    except DiagnosticAbort:
        raise DiagnosticAbort("a creation marker could not be written or verified") from None
    if existing != MARKER_BYTES:
        raise DiagnosticAbort("a creation marker is not this run's marker")


def write_allowlist(arm: str, chain: str, mcp_url: str, path: Path) -> None:
    """Render the cell allowlist straight into the parent-selected leaf, exclusively."""
    from ckbbench.config import ARM_MATRIX
    from containers.build_allowlist import build_allowlist

    from ckbbench.run.defaults import internal_rpc_for

    mcp_enabled, _ = ARM_MATRIX[arm]
    content = build_allowlist(
        chain_rpc=internal_rpc_for(chain),
        mcp_url=mcp_url if mcp_enabled else None,
        arm=arm,
    )
    path = Path(path)
    write_exclusive(path.parent, path.name, content.encode("utf-8"))


def encode_receipt(identity: tuple[int, int]) -> bytes:
    """The fixed-width candidate receipt. Rejected at decode if it is not exactly this shape."""
    device, inode = identity
    if not (0 <= device < 10 ** 20 and 0 <= inode < 10 ** 20):
        raise DiagnosticAbort("the candidate identity does not fit the receipt")
    payload = b"%s%020d %020d\n" % (RECEIPT_PREFIX, device, inode)
    if len(payload) != RECEIPT_BYTES:  # pragma: no cover - width is fixed by construction
        raise DiagnosticAbort("the candidate receipt is the wrong width")
    return payload


def decode_receipt(payload: bytes) -> tuple[int, int] | None:
    """The identity carried by an exact receipt, or None for anything else."""
    if len(payload) != RECEIPT_BYTES or not payload.startswith(RECEIPT_PREFIX):
        return None
    body = payload[len(RECEIPT_PREFIX):-1]
    device, _, inode = body.partition(b" ")
    if len(device) != 20 or len(inode) != 20 or not device.isdigit() or not inode.isdigit():
        return None
    return (int(device), int(inode))


def write_receipt(fd: int, identity: tuple[int, int]) -> None:
    """Worker side: report the inode it created, through the descriptor the parent opened."""
    _write_all(fd, encode_receipt(identity))


def read_receipt(fd: int) -> tuple[int, int] | None:
    """Parent side: the identity the worker reported, or None.

    Read only after the child is reaped, and never blocking: a spawn that failed leaves the parent
    holding the write end, and a blocking read would then wait for a writer that will never come.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= RECEIPT_BYTES:
        try:
            chunk = os.read(fd, RECEIPT_BYTES + 1 - total)
        except (BlockingIOError, InterruptedError):
            break
        except OSError:
            return None
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return decode_receipt(b"".join(chunks))


def read_created(created_dir: Path) -> set[str]:
    """Parent-side: which resources the worker reported creating.

    Proved by exact content through a no-follow read, not by `is_file()`: a zero-byte or truncated
    marker is not an acknowledgement, and treating one as such is a false creation fact.
    """
    found: set[str] = set()
    try:
        directory = DirHandle(Path(created_dir))
    except DiagnosticAbort:
        return found
    try:
        for name in CREATION_MARKERS:
            try:
                if directory.read(name, max_bytes=len(MARKER_BYTES) + 1) == MARKER_BYTES:
                    found.add(name)
            except DiagnosticAbort:
                continue
    finally:
        directory.close()
    return found


def inherited_artifact_dir() -> "DirHandle":
    """Adopt the artifact directory descriptor the parent passed to this process."""
    raw = os.environ.get(ARTIFACT_FD_ENV, "")
    if not raw.isdigit():
        raise DiagnosticAbort("the parent did not pass an artifact directory descriptor")
    return DirHandle.adopt(int(raw))


def inherited_receipt_fd() -> int:
    """The write end of the parent's receipt pipe, passed to this process by descriptor."""
    raw = os.environ.get(RECEIPT_FD_ENV, "")
    if not raw.isdigit():
        raise DiagnosticAbort("the parent did not pass a candidate receipt descriptor")
    fd = int(raw)
    try:
        os.fstat(fd)
    except OSError:
        raise DiagnosticAbort("the inherited receipt descriptor is unusable") from None
    return fd


def worker_requested() -> bool:
    """True only inside the dedicated worker process."""
    return os.environ.get(WORKER_MODE_ENV) == "1"


def load_worker_identity() -> dict[str, str]:
    """The diagnostic configuration passed explicitly by the parent after its own validation."""
    required = (
        "CKBBENCH_DIAGNOSTIC_RUN_ID",
        "CKBBENCH_DIAGNOSTIC_AGENT_NAME",
        "CKBBENCH_DIAGNOSTIC_CANDIDATE",
        "CKBBENCH_DIAGNOSTIC_RUN_DIR",
        "CKBBENCH_DIAGNOSTIC_MOUNT_DIR",
        "CKBBENCH_DIAGNOSTIC_ALLOWLIST_PATH",
        "CKBBENCH_DIAGNOSTIC_CREATED_DIR",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise DiagnosticAbort("the diagnostic worker was not configured by its parent")
    return {name: os.environ[name] for name in required}


def summarize(outcomes: Sequence[CleanupOutcome]) -> str:
    """One sanitized line per resource, for the operator. No ids, no provider values."""
    return json.dumps(
        [{"resource": o.name, "action": o.action} for o in outcomes],
        sort_keys=True, separators=(",", ":"),
    )
