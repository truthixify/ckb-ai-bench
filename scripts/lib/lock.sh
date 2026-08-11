#!/usr/bin/env bash
# Shared advisory project lock for CKB AI Bench shell entrypoints.
#
# Sourced by scripts/ckbbench and containers/validate.sh so every destructive operation excludes
# every other one through the same flock(2) on the same file. A second, private mechanism would not
# exclude the first, so new callers must source this rather than roll their own.
#
# Callers must define PY (or accept the python3/python fallback) before sourcing.
#
# There is deliberately no "inherited lock" mode. The lock is a file description, and nothing in the
# environment can prove possession of one: a PID copied out of owner.meta would pass any check based
# on it. Each entrypoint that touches Docker state takes the lock itself, for as long as it needs it.
#
# Public: with_lock <label>, release_lock, LOCK_DIR, LOCK_FILE, META_FILE.

LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/ckbbench-${UID:-$(id -u)}"
LOCK_FILE="$LOCK_DIR/project.lock"
META_FILE="$LOCK_DIR/owner.meta"

# FD used for the advisory lock (must stay open while holding the lock)
LOCK_FD=9

if ! declare -F die >/dev/null 2>&1; then
  die() { echo "FAIL: $*" >&2; exit 1; }
fi
if ! declare -F info >/dev/null 2>&1; then
  info() { echo "$*"; }
fi

ensure_lock_dir() {
  # Refuse a symlinked lock path before creating anything, so a redirected path is never followed.
  if [[ -L "$LOCK_DIR" ]]; then
    die "lock dir is a symlink: $LOCK_DIR"
  fi
  mkdir -p -m 700 "$LOCK_DIR"
  local owner
  owner="$(stat -c '%u' "$LOCK_DIR" 2>/dev/null || stat -f '%u' "$LOCK_DIR")"
  if [[ "$owner" != "${UID:-$(id -u)}" ]]; then
    die "lock dir not owned by current user: $LOCK_DIR"
  fi
  if [[ -L "$LOCK_FILE" ]]; then
    die "lock file is a symlink: $LOCK_FILE"
  fi
  # Create lock file without truncating if present
  if [[ ! -e "$LOCK_FILE" ]]; then
    : >"$LOCK_FILE"
    chmod 600 "$LOCK_FILE"
  fi
}

write_owner_meta() {
  printf 'pid=%s\ncmd=%s\nstarted=%s\n' "$$" "${1:-ckbbench}" "$(date -Iseconds 2>/dev/null || date)" >"$META_FILE"
  chmod 600 "$META_FILE" 2>/dev/null || true
}

clear_owner_meta() {
  rm -f "$META_FILE"
}

meta_pid() {
  [[ -f "$META_FILE" ]] || return 1
  sed -n 's/^pid=//p' "$META_FILE" | head -1
}

pid_alive() {
  local p="$1"
  [[ -n "$p" ]] && kill -0 "$p" 2>/dev/null
}

# Lock backends. flock(1) is util-linux and absent on a stock macOS host; python's fcntl.flock
# issues the same flock(2) call on the same file, so the two backends still exclude each other.
# Either way the lock belongs to the descriptor this shell keeps open, so it outlives the helper
# process and the kernel drops it when the operation's shell exits.
LOCK_BACKEND=""
LOCK_PY=""
LOCK_PY_CODE='import fcntl, sys
fcntl.flock(int(sys.argv[2]), fcntl.LOCK_UN if sys.argv[1] == "unlock" else fcntl.LOCK_EX | fcntl.LOCK_NB)'

lock_python() {
  local cand
  for cand in "${PY:-}" python3 python; do
    [[ -n "$cand" ]] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import fcntl' >/dev/null 2>&1; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

resolve_lock_backend() {
  if [[ -n "$LOCK_BACKEND" ]]; then
    return 0
  fi
  local want="${CKBBENCH_LOCK_BACKEND:-auto}"
  case "$want" in
    auto)
      if command -v flock >/dev/null 2>&1; then
        LOCK_BACKEND=flock
      else
        LOCK_PY="$(lock_python)" ||
          die "no lock backend: flock(1) missing and no python with fcntl (run: ./bench setup)"
        LOCK_BACKEND=python
      fi
      ;;
    flock)
      command -v flock >/dev/null 2>&1 || die "CKBBENCH_LOCK_BACKEND=flock but flock(1) is not installed"
      LOCK_BACKEND=flock
      ;;
    python)
      LOCK_PY="$(lock_python)" || die "CKBBENCH_LOCK_BACKEND=python but no python with fcntl was found"
      LOCK_BACKEND=python
      ;;
    *) die "unknown CKBBENCH_LOCK_BACKEND: $want (auto|flock|python)" ;;
  esac
}

lock_take() {
  case "$LOCK_BACKEND" in
    flock) flock -n "$LOCK_FD" 2>/dev/null ;;
    python) "$LOCK_PY" -c "$LOCK_PY_CODE" lock "$LOCK_FD" 2>/dev/null ;;
    *) return 1 ;;
  esac
}

lock_drop() {
  case "$LOCK_BACKEND" in
    flock) flock -u "$LOCK_FD" 2>/dev/null || true ;;
    python) "$LOCK_PY" -c "$LOCK_PY_CODE" unlock "$LOCK_FD" 2>/dev/null || true ;;
  esac
}

# Acquire the exclusive lock; auto-reclaim dead owner metadata only.
with_lock() {
  ensure_lock_dir
  resolve_lock_backend
  # Open append so we never truncate; keep FD open for the lock's duration.
  eval "exec ${LOCK_FD}>>\"\$LOCK_FILE\""
  if ! lock_take; then
    local opid
    opid="$(meta_pid || true)"
    if [[ -n "${opid:-}" ]] && ! pid_alive "$opid"; then
      info "WARN: reclaiming stale lock metadata (dead pid $opid)"
      clear_owner_meta
    fi
    if ! lock_take; then
      opid="$(meta_pid || echo unknown)"
      die "another ckbbench operation holds the lock (owner pid=${opid}). Try: ./bench unlock"
    fi
  fi
  write_owner_meta "${1:-ckbbench}"
  trap 'clear_owner_meta; lock_drop' EXIT
}

release_lock() {
  clear_owner_meta
  lock_drop
  trap - EXIT
}
