#!/usr/bin/env bash
# Integration proof for Phase 3 container topology (NOT part of pytest).
#
# (a) Builds agent + verifier images; asserts /tool-versions.txt shows pinned rust+clang+riscv.
# (b) Brings up devnet sidecar; asserts RPC get_tip_block_number works.
# (c) Asserts net-internal has no NAT (agent cannot curl a raw public IP directly - spike 4b).
#
# Tear-down targets ONLY ckbbench-* resources this script started.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"
PY="${CKBBENCH_PYTHON:-$ROOT/agent/.venv/bin/python}"

COMPOSE_PROJECT="ckbbench"
# The project is pinned explicitly on every Compose call, exactly as the production DevNet
# controller does. Compose gives an inherited COMPOSE_PROJECT_NAME precedence over the file's
# top-level `name:`, so without `-p` the objects this gate creates would carry a project label its
# ownership ledger does not accept -- it would then refuse and strand what it had just built.
COMPOSE="docker compose -f compose.yml -p $COMPOSE_PROJECT"

# Ownership identity for THIS invocation. Preflight absence is not ownership minutes later: the
# shared lock excludes another ckbbench entry point, not an unrelated Docker client that can create
# any fixed name while this gate spends minutes building. Every disposable target below is either
# run-scoped by name or verified to carry this run's label, and nothing else is ever removed.
#
# Always generated here, never read from the environment: a caller who can choose this value can
# choose what this gate is willing to delete. A test observes the exported value instead.
RUN_ID="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
[ -n "$RUN_ID" ] || { echo "BLOCKER: could not generate a validation run identity"; exit 1; }
export CKBBENCH_VALIDATE_RUN_ID="$RUN_ID"
# The validation gate owns the invocation-scoped networks it creates. Diagnostics deliberately leave
# this value empty because they reuse the ordinary networks and their already-running proxy while
# still stamping their disposable containers with CKBBENCH_VALIDATE_RUN_ID.
export CKBBENCH_NETWORK_VALIDATE_RUN_ID="$RUN_ID"
# Invocation-scoped storage. A Docker volume has no immutable ID, so a NAMED volume can always be
# replaced between the ownership check and the removal, and cannot be disposed safely at all. The
# DevNet data mount is therefore anonymous under validation: its lifetime is bound to the exact node
# container, and `docker rm -v <container-id>` disposes it through an immutable selector.
export CKBBENCH_DEVNET_DATA_MOUNT="/var/lib/ckb/data"
# Invocation-scoped physical networks. A fixed network name can be created by any other Docker
# client during the minutes this gate spends building, and a gate that adopts one certifies a
# topology it did not build. Ordinary runs keep the fixed defaults.
NET_INTERNAL="ckbbench-net-internal-$RUN_ID"
NET_RPC="ckbbench-net-rpc-$RUN_ID"
NET_EGRESS="ckbbench-net-egress-$RUN_ID"
export CKBBENCH_NET_INTERNAL="$NET_INTERNAL"
export CKBBENCH_NET_RPC="$NET_RPC"
export CKBBENCH_NET_EGRESS="$NET_EGRESS"
# Production agent/verifier network selection must follow the same scoped topology.
export CKBBENCH_DOCKER_NETWORK="$NET_INTERNAL"
OWNED_NETWORKS="$NET_INTERNAL $NET_RPC $NET_EGRESS"

# Run-scoped tags: a fixed `:validate` tag can be created or retargeted by another client after
# preflight, and teardown would then delete their image.
AGENT_IMAGE="ckbbench-agent:validate-$RUN_ID"
VERIFIER_IMAGE="ckbbench-verifier:validate-$RUN_ID"
PROXY_IMAGE="ckbbench-proxy:validate-$RUN_ID"
# Bind the Compose services to the tags THIS gate builds. Without this the topology checks run
# whatever ckbbench-agent:latest happens to be, which is not what validation just proved.
export CKBBENCH_AGENT_COMPOSE_IMAGE="$AGENT_IMAGE"
export CKBBENCH_PROXY_IMAGE="$PROXY_IMAGE"
# The allowlist lives in this invocation's own directory, not a shared repository path. Two
# concurrent processes previously collided on the fixed path, and an early failure could delete a
# file another client had just written there.
ALLOWLIST_ARTIFACT=""
OWNED_CONTAINERS="ckbbench-devnet-node ckbbench-devnet-miner ckbbench-proxy ckbbench-agent"
# Creation ledger. Each entry is `kind|name|id` recorded only after THIS invocation observed the
# object created, so cleanup can require that the object still present at a name is the exact one
# created here rather than a replacement that arrived later.
CREATED_LEDGER=""

# Each scoped physical network maps to exactly one logical Compose network. Any other value under
# that name is foreign, whatever else its labels say.
logical_network () {  # physical name -> logical compose network, empty if not one of ours
  case "$1" in
    "$NET_INTERNAL") printf 'net-internal' ;;
    "$NET_RPC")      printf 'net-rpc' ;;
    "$NET_EGRESS")   printf 'net-egress' ;;
  esac
}

# Records an identity captured by the caller. No second name lookup.
record_identity () {  # kind name id
  if [ -z "$3" ]; then
    echo "FAIL  could not capture the identity of the $1 this run created: $2"
    fail=1
    return 0
  fi
  CREATED_LEDGER="$CREATED_LEDGER
$1|$2|$3"
}

record_created () {  # kind name
  local kind="$1" name="$2" id=""
  case "$kind" in
    volume_anon) id="$(docker volume inspect "$name" --format '{{.Mountpoint}}' 2>/dev/null || true)" ;;
    image)     id="$(docker image inspect "$name" --format '{{.Id}}' 2>/dev/null || true)" ;;
    container) id="$(docker container inspect "$name" --format '{{.Id}}' 2>/dev/null || true)" ;;
    network)   id="$(docker network inspect "$name" --format '{{.Id}}' 2>/dev/null || true)" ;;
    # Volumes expose no immutable ID, so the recorded identity is a fingerprint: the run label this
    # invocation stamped plus the mountpoint Docker assigned. Comparing name-to-name would prove
    # nothing, because the name is reusable.
    volume)    id="$(docker volume inspect "$name" \
        --format '{{index .Labels "com.ckbbench.validate-run"}}@{{.Mountpoint}}' 2>/dev/null || true)"
               case "$id" in "$RUN_ID"@?*) : ;; *) id="" ;; esac ;;
  esac
  if [ -z "$id" ]; then
    echo "FAIL  could not record the identity of the $kind this run created: $name"
    fail=1
    return 0
  fi
  CREATED_LEDGER="$CREATED_LEDGER
$kind|$name|$id"
}

# A partial startup still created objects. Record any that now exist AND carry this run's label:
# the label is stamped by Compose from the exported identity, so it is proof of creation here, and
# the id binds that proof to the exact object.
record_created_if_ours () {  # kind name
  local label
  if [ -n "$(recorded_id "$1" "$2")" ]; then
    return 0
  fi
  case "$1" in
    container)
      # Complete identity from ONE payload: exact ID AND run label AND compose project AND the
      # expected service. The ID recorded is the one from THAT payload, never re-resolved.
      REQUIRE_RECORDED_ID=0
      if container_ownership "$2"; then
        record_identity container "$2" "$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f1)"
        REQUIRE_RECORDED_ID=1
        return 0
      fi
      REQUIRE_RECORDED_ID=1
      return 0
      ;;
    volume)
      # Identity AND fingerprint from one payload.
      local vp
      vp="$(docker volume inspect "$2" --format \
        '{{index .Labels "com.ckbbench.validate-run"}}|{{index .Labels "com.ckbbench.owner"}}|{{index .Labels "com.ckbbench.role"}}|{{.Mountpoint}}' \
        2>/dev/null || true)"
      case "$vp" in
        "$RUN_ID|ckbbench|devnet-data|"?*)
          record_identity volume "$2" "$RUN_ID@$(printf '%s' "$vp" | cut -d'|' -f4)"
          ;;
      esac
      return 0
      ;;
    network)
      # The logical compose network name for each fixed name is known; any other value is foreign.
      local np expected_logical
      expected_logical="$(logical_network "$2")"
      [ -n "$expected_logical" ] || return 0
      np="$(docker network inspect "$2" --format \
        '{{.Id}}|{{index .Labels "com.ckbbench.validate-run"}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}' \
        2>/dev/null || true)"
      case "$np" in
        ?*"|$RUN_ID|$COMPOSE_PROJECT|$expected_logical")
          record_identity network "$2" "$(printf '%s' "$np" | cut -d'|' -f1)"
          ;;
      esac
      return 0
      ;;
    # An image carries no run label; the run-scoped TAG is its discriminator, and it is only
    # recorded if it exists at a tag no other client could have chosen.
    image)     docker image inspect "$2" >/dev/null 2>&1 && label="$RUN_ID" || label="" ;;
    *)         label="" ;;
  esac
  [ "$label" = "$RUN_ID" ] || return 0
  if [ -n "$(recorded_id "$1" "$2")" ]; then
    return 0
  fi
  record_created "$1" "$2"
}

# Sweep every fixed name this gate can create and record the ones carrying this run's identity.
# Called after each mutating compose command, success or failure.
record_partial_compose_objects () {
  local c n
  for c in $OWNED_CONTAINERS; do
    record_created_if_ours container "$c"
  done
  for n in $OWNED_NETWORKS; do
    record_created_if_ours network "$n"
  done
}

recorded_id () {  # kind name -> prints recorded id, empty if never recorded
  printf '%s\n' "$CREATED_LEDGER" | awk -F'|' -v k="$1" -v n="$2" '$1==k && $2==n {print $3}' | tail -1
}

# True only when the object currently at this name is the exact one this invocation created.
is_recorded_object () {  # kind name
  local want current
  want="$(recorded_id "$1" "$2")"
  [ -n "$want" ] || return 1
  case "$1" in
    volume_anon) current="$(docker volume inspect "$2" --format '{{.Mountpoint}}' 2>/dev/null || true)" ;;
    image)     current="$(docker image inspect "$2" --format '{{.Id}}' 2>/dev/null || true)" ;;
    container) current="$(docker container inspect "$2" --format '{{.Id}}' 2>/dev/null || true)" ;;
    network)   current="$(docker network inspect "$2" --format '{{.Id}}' 2>/dev/null || true)" ;;
    volume)    current="$(docker volume inspect "$2" \
        --format '{{index .Labels "com.ckbbench.validate-run"}}@{{.Mountpoint}}' 2>/dev/null || true)" ;;
  esac
  [ -n "$current" ] && [ "$current" = "$want" ]
}
# .tool-versions is the single source of truth for what the images must run.
PINNED_NODE="$(awk '$1=="nodejs"{print $2}' "$ROOT/.tool-versions")"
PINNED_RUST="$(awk '$1=="rust"{print $2}' "$ROOT/.tool-versions")"
[ -n "$PINNED_NODE" ] && [ -n "$PINNED_RUST" ] || { echo "BLOCKER: cannot read pins from .tool-versions"; exit 1; }
# One owned directory instead of fixed /tmp paths: a fixed name silently overwrites whatever a
# previous run, another tool, or a concurrent process left there. Created only once the teardown
# trap is installed -- a directory made before its remover exists leaks on every blocked exit.
LOG_DIR=""

fail=0
checks=0
passed=0

# The absence decision below is only durable if no other project operation can create state after
# it. Image builds take minutes, so take the shared lock BEFORE the inventory and hold it through
# teardown. This gate always owns its own lock -- it is never handed one -- so nothing outside this
# process can shorten the window it is protected for.
# shellcheck source=../scripts/lib/lock.sh
source "$ROOT/scripts/lib/lock.sh"
with_lock "validate"
echo "lock: acquired"

# There is no named DevNet volume under validation: the data mount is anonymous and owned by the
# node container, so there is no reusable name to inventory, adopt, or fail to delete safely.

# Stopped benchmark services count too: the gate requires an absent stack, not just an idle one.
# The inventory must SUCCEED: `docker ps | grep || true` turns a daemon failure into an empty list,
# which is indistinguishable from "nothing exists" and would authorize teardown regardless.
benchmark_containers () {
  local out
  if ! out="$(docker ps -a --format '{{.Names}}')"; then
    echo "__DOCKER_PS_FAILED__"
    return 0
  fi
  printf '%s\n' "$out" | grep -E '^(ckbbench-|minisweagent-)' || true
}

existing_containers="$(benchmark_containers)"
if [ "$existing_containers" = "__DOCKER_PS_FAILED__" ]; then
  echo "BLOCKER: cannot inventory containers; refusing to run against an unproven stack."
  exit 1
fi
if [ -n "$existing_containers" ]; then
  echo "BLOCKER: benchmark containers exist (running or stopped):"
  echo "$existing_containers" | sed 's/^/  /'
  exit 1
fi

# Every fixed Docker name this gate can overwrite or remove must be absent first. Naming an object
# does not make a validation run its owner: the gate may only remove what it created.
absent_or_blocker () {
  local kind="$1" name="$2" phrase="$3" probe rc
  if probe="$(docker "$kind" inspect "$name" 2>&1 >/dev/null)"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    echo "BLOCKER: $kind $name already exists; validation would overwrite or remove it."
    echo "  Remove it deliberately, then re-run."
    exit 1
  fi
  # Fail closed: only an object-specific absence proves absence. A daemon, context or permission
  # failure must not be read as permission to create and later delete state. Docker words absence
  # differently per object kind, so match the real phrasings rather than one invented one:
  #   network -> "network NAME not found"
  #   image   -> "No such image: NAME"
  #   volume  -> "get NAME: no such volume"
  if ! printf '%s' "$probe" | grep -qiE "not found|no such (object|image|volume|container|network)" \
     || ! printf '%s' "$probe" | grep -qE "(^|[^A-Za-z0-9_.-])$name([^A-Za-z0-9_.-]|\$)"; then
    echo "BLOCKER: cannot determine whether $kind $name exists: $probe"
    exit 1
  fi
}

for _net in $OWNED_NETWORKS; do
  absent_or_blocker network "$_net" "no such network"
done
for _img in "$AGENT_IMAGE" "$VERIFIER_IMAGE" "$PROXY_IMAGE"; do
  absent_or_blocker image "$_img" "no such image"
done
check () {
  local want="$1" label="$2"
  shift 2
  checks=$((checks + 1))
  local got=0
  "$@" >/dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then
    echo "PASS  $label (exit $got)"
    passed=$((passed + 1))
  else
    echo "FAIL  $label (got exit $got, wanted $want)"
    fail=1
  fi
}

# Object-specific absence, reused from preflight. A daemon, context or permission error is NOT
# proof that something disappeared, so cleanup verification must not equate a nonzero exit with
# absence the way a bare `inspect >/dev/null` does.
object_absent () {
  local kind="$1" name="$2" probe rc
  if probe="$(docker "$kind" inspect "$name" 2>&1 >/dev/null)"; then
    return 1                                  # exists
  fi
  rc=$?
  if printf '%s' "$probe" | grep -qiE "not found|no such (object|image|volume|container|network)" \
     && printf '%s' "$probe" | grep -qE "(^|[^A-Za-z0-9_.-])$name([^A-Za-z0-9_.-]|\$)"; then
    return 0                                  # provably absent
  fi
  echo "FAIL  cannot determine whether $kind $name still exists: $probe"
  fail=1
  return 1
}

# Ownership by construction, not by timestamp: these are the volumes mounted into the exact
# containers this gate created, read before Compose removes them. A global before/after difference
# would also select an unrelated anonymous volume that any other Docker client happened to create
# during this minutes-long run.
OWNED_ANON=""

# Ownership decision for one fixed name. Three outcomes, and only one of them permits cleanup:
#   0 = this invocation's container (label matches RUN_ID)
#   1 = provably absent, nothing to do
#   2 = present but NOT ours, or undeterminable -> gate failure, object left untouched
# One inspect, one payload. Reading the label and the mounts from two separate name-based inspects
# would let a replacement between them change which object is read.
container_payload () {
  docker container inspect "$1" --format \
    '{{.Id}}|{{index .Config.Labels "com.ckbbench.validate-run"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}},{{end}}{{end}}' \
    2>&1
}

# Builds and records the exact image the build produced. The ID comes from --iidfile, written by
# the build operation itself: resolving the tag afterwards would record whatever a replacement had
# retargeted that tag to. The run label makes the image content unique to this invocation, so a
# cached content-addressed layer set cannot be mistaken for something this run uniquely owns.
build_image () {  # dockerfile tag context logname
  local iid_file="$LOG_DIR/iid-$4" iid
  docker build -f "$1" -t "$2" --iidfile "$iid_file" \
    --label "com.ckbbench.validate-run=$RUN_ID" "$3" >"$LOG_DIR/$4-build.log" 2>&1
  iid="$(tr -d '\r\n' < "$iid_file" 2>/dev/null || true)"
  record_identity image "$2" "$iid"
  [ -n "$iid" ] || { echo "BLOCKER: build of $2 produced no image id"; exit 1; }
  # The captured ID is the EXECUTION selector from here on, not just the deletion selector. A tag
  # is a mutable pointer: everything this gate then runs, and every image Compose is configured
  # with, must be the exact image this build produced.
  BUILT_IMAGE_ID="$iid"
}

# Fails closed if the run tag no longer resolves to the image the build produced. The exact ID is
# still what gets used afterwards, so a retarget after this proof cannot redirect execution.
assert_tag_still_points_at_build () {  # tag id
  local current
  current="$(docker image inspect "$1" --format '{{.Id}}' 2>/dev/null || true)"
  if [ "$current" != "$2" ]; then
    echo "BLOCKER: $1 no longer points at the image this run built; refusing to run it"
    fail=1
    exit 1
  fi
}

# Every expected object must be the exact one this run created BEFORE anything depends on it.
# Recording a foreign object as "not ours" only protects cleanup; continuing to the RPC probe,
# agent startup, DNS use or an exec means the gate certifies a topology it did not build.
assert_topology_owned () {  # kind:name ...
  local spec kind name blocked=0
  for spec in "$@"; do
    kind="${spec%%:*}"; name="${spec#*:}"
    if [ -z "$(recorded_id "$kind" "$name")" ]; then
      echo "BLOCKER: $kind $name was not created by this run; refusing to use it"
      blocked=1
    elif ! recorded_object_still_ours "$kind" "$name"; then
      echo "BLOCKER: $kind $name is no longer the object this run created; refusing to use it"
      blocked=1
    fi
  done
  if [ "$blocked" -ne 0 ]; then
    fail=1
    exit 1
  fi
}

# Default: the full delete-time check. Set to 0 for the record-time question.
REQUIRE_RECORDED_ID=1

# Proves the complete identity of an exact ID from ONE payload, without ever resolving a name.
# This is the deletion-time question: the object we are about to mutate must still be the object
# recorded, with every identity field intact.
recorded_object_still_ours () {  # kind name
  local kind="$1" name="$2" want payload expected_logical
  want="$(recorded_id "$kind" "$name")"
  [ -n "$want" ] || return 1
  case "$kind" in
    container)
      payload="$(docker container inspect "$want" --format \
        '{{.Id}}|{{index .Config.Labels "com.ckbbench.validate-run"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' \
        2>/dev/null || true)"
      [ "$payload" = "$want|$RUN_ID|$COMPOSE_PROJECT|$name" ]
      ;;
    network)
      expected_logical="$(logical_network "$name")"
      [ -n "$expected_logical" ] || return 1
      payload="$(docker network inspect "$want" --format \
        '{{.Id}}|{{index .Labels "com.ckbbench.validate-run"}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.network"}}' \
        2>/dev/null || true)"
      [ "$payload" = "$want|$RUN_ID|$COMPOSE_PROJECT|$expected_logical" ]
      ;;
    image)
      payload="$(docker image inspect "$want" --format '{{.Id}}' 2>/dev/null || true)"
      [ "$payload" = "$want" ]
      ;;
    *) return 1 ;;
  esac
}

container_ownership () {
  local name="$1" probe label project identity_ok
  if ! probe="$(container_payload "$name")"; then
    if printf '%s' "$probe" | grep -qiE "not found|no such (object|container)" \
       && printf '%s' "$probe" | grep -qE "(^|[^A-Za-z0-9_.-])$name([^A-Za-z0-9_.-]|\$)"; then
      return 1
    fi
    echo "FAIL  cannot determine ownership of container $name: $probe"
    fail=1
    return 2
  fi
  OWNED_PAYLOAD="$(printf '%s' "$probe" | tr -d '\r\n')"
  local id service want
  id="$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f1)"
  label="$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f2)"
  project="$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f3)"
  service="$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f4)"
  want="$(recorded_id container "$name")"
  identity_ok=0
  [ "$label" = "$RUN_ID" ] && [ "$project" = "$COMPOSE_PROJECT" ] && [ "$service" = "$name" ] \
    && identity_ok=1
  if [ "$REQUIRE_RECORDED_ID" = "0" ]; then
    # Record-time question: is this what this run created? The ledger entry does not exist yet.
    [ "$identity_ok" = "1" ] && return 0
  elif [ "$identity_ok" = "1" ] && [ -n "$want" ] && [ "$id" = "$want" ]; then
    # Delete-time question: identity AND the exact recorded id. A replacement adopting the name,
    # label and project is still a different object.
    return 0
  fi
  if [ -n "$want" ] && [ "$id" != "$want" ]; then
    echo "FAIL  container $name is not the object this run created (identity changed);"
    echo "      refusing to inspect or remove anything belonging to it"
    fail=1
    return 2
  fi
  echo "FAIL  container $name exists but is not this validation run's (foreign or unlabelled);"
  echo "      refusing to inspect or remove anything belonging to it"
  fail=1
  return 2
}

capture_owned_anonymous_volumes () {
  local c mounts m rc
  for c in $OWNED_CONTAINERS; do
    if container_ownership "$c"; then rc=0; else rc=$?; fi
    [ "$rc" -eq 1 ] && continue            # absent
    [ "$rc" -eq 2 ] && continue            # foreign/undeterminable: already failed, never touched
    # Same payload the ownership decision was made from.
    mounts="$(printf '%s' "$OWNED_PAYLOAD" | cut -d'|' -f5 | tr ',' '\n')"
    while IFS= read -r m; do
      [ -z "$m" ] && continue
      # Anonymous volumes are 64-hex names. The labelled ckbbench-devnet-data has its own lifecycle
      # path and must never be swept here.
      if printf '%s' "$m" | grep -qE '^[0-9a-f]{64}$'; then
        # Mounted into a container this run proved it owns; recorded so cleanup operates on a
        # ledger entry rather than a bare name read at teardown time.
        record_created volume_anon "$m"
        OWNED_ANON="$OWNED_ANON $m"
      fi
    done <<< "$mounts"
  done
}

teardown () {
  # Preserve the incoming status, then attempt EVERY owned cleanup step regardless of an earlier
  # check failure. Exiting early here is why a failed run used to leave its images, networks,
  # allowlist, volumes and log directory behind.
  local incoming=$fail

  capture_owned_anonymous_volumes

  # `down` without -v: named volume removal goes through the labelled, inspected lifecycle path
  # below, and only for a volume this gate created. Skipped entirely if any fixed name is held by
  # something this invocation did not create.
  # No project-wide `compose down`: that selector can remove any object carrying the project
  # label, including one this invocation never created. Recorded container IDs are removed
  # individually after re-proving each one.
  for entry in $(printf '%s\n' "$CREATED_LEDGER" | awk -F'|' '$1=="container"{print $2"="$3}'); do
    cname="${entry%%=*}"; cid="${entry#*=}"
    # Inspected BY THE RECORDED ID: resolving the name again here would reintroduce exactly the
    # check/use split this replaces.
    if ! recorded_object_still_ours container "$cname"; then
      echo "FAIL  container $cname does not present this run's complete identity; left untouched"
      fail=1
      continue
    fi
    # `-v` disposes the anonymous volumes bound to THIS exact container. That is the only
    # ownership-safe volume deletion Docker offers: the selector is the immutable container ID,
    # not a reusable volume name.
    docker rm -f -v "$cid" >/dev/null 2>&1 || true
    if docker container inspect "$cid" >/dev/null 2>&1; then
      echo "FAIL  container $cname (recorded id) remains after teardown"
      fail=1
    fi
  done
  for entry in $(printf '%s\n' "$CREATED_LEDGER" | awk -F'|' '$1=="network"{print $2"="$3}'); do
    nname="${entry%%=*}"; nid="${entry#*=}"
    if ! recorded_object_still_ours network "$nname"; then
      echo "FAIL  network $nname is no longer the object this run created; left untouched"
      fail=1
      continue
    fi
    # By recorded ID: a replacement arriving after the check would otherwise be what the name
    # resolves to when the removal runs.
    docker network rm "$nid" >/dev/null 2>&1 || true
    if docker network inspect "$nid" >/dev/null 2>&1; then
      echo "FAIL  network $nname (recorded id) remains after teardown"
      fail=1
    fi
  done

  leftovers="$(benchmark_containers)"
  if [ "$leftovers" = "__DOCKER_PS_FAILED__" ]; then
    echo "FAIL  could not inventory containers during teardown"
    fail=1
  elif [ -n "$leftovers" ]; then
    echo "FAIL  benchmark containers remain after teardown:"
    echo "$leftovers" | sed 's/^/  /'
    fail=1
  fi

  for image in $(printf '%s\n' "$CREATED_LEDGER" | awk -F'|' '$1=="image"{print $2}'); do
    if ! recorded_object_still_ours image "$image"; then
      echo "FAIL  $image is no longer the image this run built; leaving it untouched"
      fail=1
      continue
    fi
    docker rmi "$(recorded_id image "$image")" >/dev/null 2>&1 || true
    object_absent image "$image" || {
      # Unconditional: suppressing this when an earlier check already failed is precisely how a
      # surviving image would go unreported in the runs that need the warning most.
      echo "FAIL  validation image $image remains after teardown"
      fail=1
    }
  done

  # Only networks this run created are its business. One that survives teardown is reported; one
  # that a foreign client created after preflight is left alone and fails the gate instead.
  for net in $OWNED_NETWORKS; do
    if [ -n "$(recorded_id network "$net")" ]; then
      if object_absent network "$net"; then
        continue
      fi
      net_run_label="$(docker network inspect "$net" \
        --format '{{index .Labels "com.ckbbench.validate-run"}}' 2>/dev/null || true)"
      if [ "$net_run_label" = "$RUN_ID" ] && is_recorded_object network "$net"; then
        echo "FAIL  network $net created by this run remains after teardown"
      else
        echo "FAIL  network $net exists and is not this run's; left untouched"
      fi
      fail=1
    fi
  done



  # Disposed with their owning containers above. Anything still present either was not bound to a
  # container this run removed, or is a replacement that arrived at that name; either way it is
  # reported and the gate fails rather than issuing a name-selected deletion.
  for vol in $OWNED_ANON; do
    if object_absent volume "$vol"; then
      continue
    fi
    if is_recorded_object volume_anon "$vol"; then
      echo "FAIL  anonymous volume $vol from this run remains after teardown"
    else
      echo "FAIL  anonymous volume $vol is no longer the object this run recorded; left untouched"
    fi
    fail=1
  done

  if [ -n "$ALLOWLIST_ARTIFACT" ] && [ -e "$ALLOWLIST_ARTIFACT" ]; then
    rm -f "$ALLOWLIST_ARTIFACT" 2>/dev/null || true
    if [ -e "$ALLOWLIST_ARTIFACT" ]; then
      echo "FAIL  validation allowlist $ALLOWLIST_ARTIFACT remains after teardown"
      fail=1
    fi
  fi

  if [ -n "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR" 2>/dev/null || true
    if [ -e "$LOG_DIR" ]; then
      echo "FAIL  validation log directory $LOG_DIR remains after teardown"
      fail=1
    fi
  fi

  # The success invariant, checked against the ledger rather than against the steps above: this run
  # may report success only if every object it recorded creating is provably gone. A cleanup step
  # that silently did nothing cannot pass here.
  for entry in $(printf '%s\n' "$CREATED_LEDGER" | awk -F'|' 'NF==3{print $1"="$3}'); do
    lkind="${entry%%=*}"; lid="${entry#*=}"
    case "$lkind" in
      container|network|image) : ;;
      *) continue ;;
    esac
    if ! object_absent "$lkind" "$lid"; then
      echo "FAIL  $lkind $lid recorded by this run is still present after teardown"
      fail=1
    fi
  done

  if [ "$incoming" -ne 0 ] || [ "$fail" -ne 0 ]; then
    echo "RESULT: CONTAINER CHECK FAILURES PRESENT (teardown complete)"
    release_lock
    exit 1
  fi
  release_lock
}

trap teardown EXIT
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ckbbench-validate-XXXXXX")"

assert_tool_versions () {
  local image="$1" label="$2"
  local txt node_v rustc_v
  txt="$(docker run --rm "$image" cat /tool-versions.txt)"
  checks=$((checks + 1))
  # Compare the EXACT committed pins, not a prefix: a `rustc 1.95` prefix would accept 1.95.9, and
  # a mutable Node stream would satisfy any major-only check while contradicting the frozen suite.
  node_v="$(docker run --rm "$image" node --version 2>/dev/null || echo MISSING)"
  if [ "$node_v" != "v${PINNED_NODE}" ]; then
    echo "FAIL  $label node is $node_v, not the pinned v${PINNED_NODE}"; fail=1; return
  fi
  rustc_v="$(docker run --rm "$image" rustc --version 2>/dev/null | awk '{print $2}')"
  if [ "$rustc_v" != "$PINNED_RUST" ]; then
    echo "FAIL  $label rustc is ${rustc_v:-MISSING}, not the pinned $PINNED_RUST"; fail=1; return
  fi
  echo "$txt" | grep -qi "clang" || { echo "FAIL  $label missing clang"; fail=1; return; }
  echo "$txt" | grep -q "riscv64imac-unknown-none-elf" || { echo "FAIL  $label missing riscv target"; fail=1; return; }
  # python is a harness-side pin, not an image runtime (see .tool-versions): neither role image
  # ships a pinned interpreter, so asserting one here would test a claim the images never make.
  echo "$txt" | grep -qF "v${PINNED_NODE}" || { echo "FAIL  $label tool-versions.txt does not record the pinned node"; fail=1; return; }
  echo "PASS  $label runs exactly node v${PINNED_NODE} and rustc $PINNED_RUST (+clang, riscv)"
  passed=$((passed + 1))
}

echo "== (a) build agent + verifier images (repo-root context for cargo bake) =="
build_image agent.Dockerfile "$AGENT_IMAGE" "$ROOT" agent
AGENT_ID="$BUILT_IMAGE_ID"
# Verifier bake needs suites/; context must be repo root (not containers/ only).
build_image verifier.Dockerfile "$VERIFIER_IMAGE" "$ROOT" verifier
VERIFIER_ID="$BUILT_IMAGE_ID"
# Built here rather than implicitly by `compose up`, so the proxy image ID also comes from a build
# operation this gate owns instead of a tag lookup afterwards.
build_image proxy/proxy.Dockerfile "$PROXY_IMAGE" ./proxy proxy
PROXY_ID="$BUILT_IMAGE_ID"
assert_tag_still_points_at_build "$AGENT_IMAGE" "$AGENT_ID"
assert_tag_still_points_at_build "$VERIFIER_IMAGE" "$VERIFIER_ID"
assert_tag_still_points_at_build "$PROXY_IMAGE" "$PROXY_ID"
# Compose must launch the exact built images too; the tags remain diagnostics only.
export CKBBENCH_AGENT_COMPOSE_IMAGE="$AGENT_ID"
export CKBBENCH_PROXY_IMAGE="$PROXY_ID"
assert_tool_versions "$AGENT_ID" "agent image"
assert_tool_versions "$VERIFIER_ID" "verifier image"
# Structural bake gates (image-local cargo + /work seed); full offline smoke is bake-time.
check 0 "agent image has /work sticky seed" \
  docker run --rm --user 1000:1000 "$AGENT_ID" sh -c 'test -d /work && test -w /work'
check 0 "verifier image has image-local CARGO_HOME" \
  docker run --rm "$VERIFIER_ID" sh -c 'test -d /opt/ckbbench-cargo && grep -q CARGO_HOME= /tool-versions.txt'
# Agent image must never contain hidden suite sources.
check 0 "agent image has no hidden suite tree" \
  sh -c 'docker run --rm "$0" sh -c "test ! -e /tmp/verifier-bake && test ! -d /suite/src"' "$AGENT_ID"
# The pinned transaction SDK must import from an arbitrary fresh workspace with NO network: a
# graded run cannot download packages, and Node's ESM resolver only walks parent directories.
check 0 "agent image imports pinned CKB SDK offline from a fresh workspace" \
  sh -c 'docker run --rm --network none --user 1000:1000 -w /work "$0" \
    sh -c "mkdir -p /work/fresh-\$\$ && cd /work/fresh-\$\$ \
      && node --input-type=module -e \"import { SignerCkbPrivateKey } from \\\"@ckb-ccc/core\\\"; if (typeof SignerCkbPrivateKey !== \\\"function\\\") process.exit(1)\""' \
  "$AGENT_ID"
check 0 "agent image records the pinned CKB SDK version" \
  sh -c 'docker run --rm "$0" grep -q "@ckb-ccc/core: 1.12.5" /tool-versions.txt' "$AGENT_ID"
# The host harness owns the agent fork and its MCP client. If the execution image carried them, a
# no-MCP arm could reach the product under test from an ordinary shell.
check 0 "agent image has no host-side agent fork" \
  sh -c 'docker run --rm "$0" sh -c "test ! -e /agent"' "$AGENT_ID"
check 0 "agent image cannot import the MCP client from the former carrier path" \
  sh -c 'docker run --rm "$0" sh -c "test ! -e /agent/ckb_mcp.py && test ! -e /agent/spike_mcp.py \
    && ! PYTHONPATH=/agent python3 -c \"import ckb_mcp\" 2>/dev/null"' "$AGENT_ID"
check 0 "agent image injects no MCP endpoint into its environment" \
  sh -c 'docker image inspect "$0" --format "{{json .Config.Env}}" \
    | grep -qiE "MCP_URL|mcp\\.ckbdev" && exit 1 || exit 0' "$AGENT_ID"
# General HTTP tooling stays: the boundary is product access, not ordinary web research.
check 0 "agent image keeps general-purpose HTTP libraries for B" \
  sh -c 'docker run --rm "$0" python3 -c "import requests"' "$AGENT_ID"

echo "== (b) devnet sidecar RPC =="
# Block-mode allowlist for validate (devnet node + proxy only), written into THIS invocation's
# own directory. A shared repository path collides between concurrent processes and lets an early
# failure delete a file another client wrote.
ALLOWLIST_ARTIFACT="$LOG_DIR/allowlist.validate.built"
"$PY" "$ROOT/containers/build_allowlist.py" \
  --arm A --chain-rpc http://ckbbench-devnet-node:8114 \
  -o "$ALLOWLIST_ARTIFACT"
export CKBBENCH_ALLOWLIST_FILE="$ALLOWLIST_ARTIFACT"

# No unguarded `compose down` here: preflight already proved every fixed name absent, and acting on
# whatever holds those names now would destroy an object that appeared during the build window.
# The failure branch must record before `set -e` reaches the trap: a partially created proxy or
# network otherwise carries this run's label with no ledger entry, is classified foreign, and is
# left behind.
if ! $COMPOSE up -d ckbbench-proxy >/dev/null; then
  record_partial_compose_objects
  echo "FAIL  proxy startup failed"
  fail=1
  exit 1
fi
record_created_if_ours container ckbbench-proxy
record_partial_compose_objects
# Hard stop before anything depends on this topology.
assert_topology_owned "network:$NET_INTERNAL" "network:$NET_EGRESS" "container:ckbbench-proxy"

# Bring DevNet up through the production lifecycle controller, not a bare `compose up`: it creates
# the labelled state volume, hands it to the node user, and proves chain identity, miner progress
# and indexer readiness. Validating the real path is the point of this gate.
checks=$((checks + 1))
if "$PY" -c 'from ckbbench.run.devnet import prepare_devnet; s = prepare_devnet(); \
print(f"prepared {s.chain} tip={s.prepared_tip_number} genesis={s.genesis_hash[:18]}...")'; then
  devnet_ok=1
else
  devnet_ok=0
fi
# Swept whether preparation succeeded or failed partway, and BEFORE either branch decides what to
# do: anything carrying this run's label was created here and must remain cleanable. The RPC
# network is introduced by this lifecycle's selected-service create, not by proxy startup, so the
# sweep is the only place it can enter the ledger.
record_partial_compose_objects
if [ "$devnet_ok" -eq 1 ]; then
  echo "PASS  devnet prepared through the production lifecycle controller"
  passed=$((passed + 1))
else
  # Ownership proves Docker created these objects, not that the chain is ready. Continuing would
  # run the RPC probe, the agent and an exec against a chain whose preparation just failed.
  echo "FAIL  devnet lifecycle preparation"
  fail=1
  exit 1
fi
assert_topology_owned "network:$NET_INTERNAL" "network:$NET_RPC" \
  "container:ckbbench-devnet-node" "container:ckbbench-devnet-miner"

# The network is selected by its recorded ID for the same reason.
check 0 "devnet get_tip_block_number via RPC" \
  sh -c 'docker run --rm --network "$0" curlimages/curl:8.12.1 \
    -fsS -m 10 -X POST http://ckbbench-devnet-node:8114 \
    -H "Content-Type: application/json" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"get_tip_block_number\",\"params\":[]}" \
    | grep -q result' \
  "$(recorded_id network "$NET_INTERNAL")"

echo "== (c) internal network has no NAT (spike 4b) =="
if ! $COMPOSE --profile agent up -d ckbbench-agent >/dev/null; then
  record_partial_compose_objects
  echo "FAIL  agent startup failed"
  fail=1
  exit 1
fi
record_partial_compose_objects
assert_topology_owned "network:$NET_INTERNAL" "container:ckbbench-agent"

# Addressed by the recorded container ID, never by the reusable service name: an exec selected by
# name runs against whatever holds that name at exec time.
check 0 "agent direct curl to raw public IP fails at L3 (6/7/28)" \
  sh -c 'docker exec "$0" curl -fsS -m 8 http://1.1.1.1/ >/dev/null 2>&1; ec=$?; case "$ec" in 6|7|28) exit 0;; *) echo "got curl exit $ec, wanted 6/7/28"; exit 1;; esac' \
  "$(recorded_id container ckbbench-agent)"

echo
echo "SUMMARY: $passed/$checks checks passed"
if [ "$fail" -eq 0 ]; then
  echo "RESULT: ALL CONTAINER CHECKS PASSED"
  exit 0
fi
echo "RESULT: CONTAINER CHECK FAILURES PRESENT"
exit 1
