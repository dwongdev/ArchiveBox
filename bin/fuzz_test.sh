#!/usr/bin/env bash

# Chaos-drive ArchiveBox CLI commands and print what happens.
# This is intentionally not a test harness: it does not assert success/failure.

set -u

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT_DIR/data}"
LOG_DIR="${FUZZ_LOG_DIR:-$ROOT_DIR/tmp/fuzz-$(date +%Y%m%d-%H%M%S)}"
FUZZ_ROUNDS="${FUZZ_ROUNDS:-8}"
FUZZ_PARALLEL="${FUZZ_PARALLEL:-5}"
FUZZ_KILL_MIN_SECONDS="${FUZZ_KILL_MIN_SECONDS:-60}"
FUZZ_KILL_MAX_SECONDS="${FUZZ_KILL_MAX_SECONDS:-120}"
SERVER_BASE_PORT="${FUZZ_SERVER_BASE_PORT:-8700}"
SLEEP_BETWEEN_JOBS_MAX="${FUZZ_SLEEP_BETWEEN_JOBS_MAX:-5}"

if [[ "$FUZZ_PARALLEL" -gt 5 ]]; then
    FUZZ_PARALLEL=5
fi
if [[ "$FUZZ_KILL_MAX_SECONDS" -lt "$FUZZ_KILL_MIN_SECONDS" ]]; then
    FUZZ_KILL_MAX_SECONDS="$FUZZ_KILL_MIN_SECONDS"
fi

if [[ -n "${ARCHIVEBOX_CMD:-}" ]]; then
    read -r -a ABX <<< "$ARCHIVEBOX_CMD"
elif command -v archivebox >/dev/null 2>&1; then
    ABX=(archivebox)
else
    ABX=(uv run archivebox)
fi

DEFAULT_URLS=(
    "https://example.com/"
    "https://blog.sweeting.me/"
    "https://www.iana.org/domains/reserved"
    "https://httpbin.org/html"
    "https://www.recurse.com/"
)

URLS=()
if [[ -n "${FUZZ_URLS:-}" ]]; then
    while IFS= read -r url; do
        [[ -n "$url" ]] && URLS+=("$url")
    done <<< "$FUZZ_URLS"
else
    URLS=("${DEFAULT_URLS[@]}")
fi

ts() {
    date "+%Y-%m-%d %H:%M:%S"
}

pick() {
    local arr_name="$1"
    local len idx
    eval "len=\${#${arr_name}[@]}"
    idx=$((RANDOM % len))
    eval "printf '%s\n' \"\${${arr_name}[$idx]}\""
}

random_between() {
    local min="$1"
    local max="$2"
    echo $((min + RANDOM % (max - min + 1)))
}

random_kill_after() {
    random_between "$FUZZ_KILL_MIN_SECONDS" "$FUZZ_KILL_MAX_SECONDS"
}

random_start_delay() {
    random_between 0 "$SLEEP_BETWEEN_JOBS_MAX"
}

tree_pids() {
    local pid="$1"
    local child
    if command -v pgrep >/dev/null 2>&1; then
        for child in $(pgrep -P "$pid" 2>/dev/null || true); do
            tree_pids "$child"
        done
    fi
    echo "$pid"
}

kill_tree() {
    local pid="$1"
    local signal="${2:-TERM}"
    local tree_file="${3:-}"
    if [[ -n "$tree_file" ]]; then
        tree_pids "$pid" >> "$tree_file"
        sort -u "$tree_file" | while IFS= read -r tree_pid; do
            kill "-$signal" "$tree_pid" >/dev/null 2>&1 || true
        done
    else
        tree_pids "$pid" | while IFS= read -r tree_pid; do
            kill "-$signal" "$tree_pid" >/dev/null 2>&1 || true
        done
    fi
}

cleanup() {
    local pid
    echo "[$(ts)] cleanup: stopping active background jobs"
    for pid in $(jobs -pr); do
        kill_tree "$pid"
    done
    sleep 2
    for pid in $(jobs -pr); do
        kill -9 "$pid" >/dev/null 2>&1 || true
    done
}

trap cleanup INT TERM EXIT

run_with_timeout() {
    local label="$1"
    shift 1

    local slug token timeout timeout_marker tree_file
    timeout="$(random_kill_after)"
    slug="$(echo "$label" | tr ' /:' '____' | tr -cd '[:alnum:]_.-')"
    token="$(date +%s).$RANDOM.$RANDOM"
    local logfile="$LOG_DIR/${slug}.${token}.log"
    timeout_marker="$LOG_DIR/.timeout.${token}"
    tree_file="$LOG_DIR/.tree.${token}"

    {
        echo "[$(ts)] START label=$label shell=$$ data=$DATA_DIR"
        echo "[$(ts)] CMD DATA_DIR=$DATA_DIR $*"
        echo "[$(ts)] CHAOS kill_after=${timeout}s"
    } | tee -a "$logfile"

    (
        DATA_DIR="$DATA_DIR" "$@"
    ) >> "$logfile" 2>&1 &
    local child=$!

    (
        sleep "$timeout"
        if kill -0 "$child" >/dev/null 2>&1; then
            echo "[$(ts)] TIMEOUT label=$label pid=$child after=${timeout}s" >> "$logfile"
            : > "$timeout_marker"
            : > "$tree_file"
            kill_tree "$child" TERM "$tree_file"
            sleep 5
            kill_tree "$child" KILL "$tree_file"
        fi
    ) &
    local watchdog=$!

    trap 'kill_tree "$child"; kill "$watchdog" >/dev/null 2>&1 || true' INT TERM

    wait "$child"
    local code=$?
    if [[ -e "$timeout_marker" ]]; then
        wait "$watchdog" >/dev/null 2>&1 || true
    else
        kill "$watchdog" >/dev/null 2>&1 || true
    fi
    wait "$watchdog" >/dev/null 2>&1 || true
    kill_tree "$child"
    rm -f "$timeout_marker" "$tree_file"
    trap - INT TERM

    echo "[$(ts)] END label=$label pid=$child exit=$code log=$logfile" | tee -a "$logfile"
    return 0
}

run_server_for_a_bit() {
    local label="$1"
    local port="$2"
    local debug_flag="$3"
    local token logfile hold
    local server_extra=()
    [[ -n "$debug_flag" ]] && read -r -a server_extra <<< "$debug_flag"
    hold="$(random_kill_after)"
    token="$(date +%s).$RANDOM.$RANDOM"
    logfile="$LOG_DIR/server-${port}.${token}.log"

    {
        echo "[$(ts)] START label=$label shell=$$ data=$DATA_DIR"
        echo "[$(ts)] CMD DATA_DIR=$DATA_DIR ${ABX[*]} server $debug_flag 127.0.0.1:$port"
        echo "[$(ts)] CHAOS kill_after=${hold}s"
    } | tee -a "$logfile"

    (
        DATA_DIR="$DATA_DIR" "${ABX[@]}" server "${server_extra[@]}" "127.0.0.1:$port"
    ) >> "$logfile" 2>&1 &
    local child=$!

    trap 'kill_tree "$child"' INT TERM

    sleep "$hold"
    echo "[$(ts)] STOP label=$label pid=$child after=${hold}s" | tee -a "$logfile"
    kill_tree "$child"
    sleep 5
    kill_tree "$child" KILL
    wait "$child" >/dev/null 2>&1
    local code=$?
    trap - INT TERM

    echo "[$(ts)] END label=$label pid=$child exit=$code log=$logfile" | tee -a "$logfile"
    return 0
}

job_init() {
    run_with_timeout "init" "${ABX[@]}" init
}

job_init_install() {
    run_with_timeout "init-install" "${ABX[@]}" init --install
}

job_update_all() {
    run_with_timeout "update" "${ABX[@]}" update
}

job_update_index() {
    run_with_timeout "update-index-only" "${ABX[@]}" update --index-only
}

job_update_migrate() {
    run_with_timeout "update-migrate-only" "${ABX[@]}" update --migrate-only
}

job_update_index_migrate() {
    run_with_timeout "update-index-migrate-only" "${ABX[@]}" update --index-only --migrate-only
}

job_add_depth0() {
    local url
    url="$(pick URLS)"
    run_with_timeout "add-depth0-$url" "${ABX[@]}" add --depth=0 "$url"
}

job_add_depth1() {
    local url max_urls
    url="$(pick URLS)"
    max_urls=$((5 + RANDOM % 25))
    run_with_timeout "add-depth1-max${max_urls}-$url" "${ABX[@]}" add --depth=1 --max-urls="$max_urls" "$url"
}

job_server() {
    local port debug_flag
    port=$((SERVER_BASE_PORT + RANDOM % 50))
    debug_flag=""
    [[ $((RANDOM % 4)) -eq 0 ]] && debug_flag="--debug"
    run_server_for_a_bit "server-$port" "$port" "$debug_flag"
}

job_server_reload_debug() {
    local port
    port=$((SERVER_BASE_PORT + 50 + RANDOM % 25))
    run_server_for_a_bit "server-reload-debug-$port" "$port" "--reload --debug"
}

job_version() {
    run_with_timeout "version" "${ABX[@]}" version
}

job_list() {
    run_with_timeout "list-search" "${ABX[@]}" list --search content example
}

# Used through pick JOBS.
# shellcheck disable=SC2034
JOBS=(
    job_init
    job_init_install
    job_update_all
    job_update_index
    job_update_migrate
    job_update_index_migrate
    job_add_depth0
    job_add_depth1
    job_server
    job_server_reload_debug
    job_version
    job_list
)

run_random_job() {
    local job
    job="$(pick JOBS)"
    "$job"
}

run_difficult_sequence() {
    local port_a port_b
    port_a=$((SERVER_BASE_PORT + 100 + RANDOM % 50))
    port_b=$((SERVER_BASE_PORT + 150 + RANDOM % 50))

    echo "[$(ts)] SEQUENCE overlap-init-server-update-add ports=$port_a,$port_b"
    run_server_for_a_bit "sequence-server-a-$port_a" "$port_a" "" &
    sleep "$(random_start_delay)"
    job_init &
    sleep "$(random_start_delay)"
    job_update_index &
    sleep "$(random_start_delay)"
    job_add_depth0 &
    sleep "$(random_start_delay)"
    run_server_for_a_bit "sequence-server-b-$port_b" "$port_b" "--reload --debug" &
    wait

    echo "[$(ts)] SEQUENCE update-mode-collision"
    job_update_all &
    job_update_index &
    job_update_migrate &
    job_update_index_migrate &
    wait
}

main() {
    mkdir -p "$LOG_DIR"
    cd "$ROOT_DIR" || exit 1

    echo "[$(ts)] ArchiveBox fuzz run"
    echo "  root:       $ROOT_DIR"
    echo "  data:       $DATA_DIR"
    echo "  logs:       $LOG_DIR"
    echo "  command:    ${ABX[*]}"
    echo "  rounds:     $FUZZ_ROUNDS"
    echo "  parallel:   $FUZZ_PARALLEL"
    echo "  kill after: ${FUZZ_KILL_MIN_SECONDS}s-${FUZZ_KILL_MAX_SECONDS}s"
    echo "  urls:       ${URLS[*]}"
    echo

    for round in $(seq 1 "$FUZZ_ROUNDS"); do
        echo "[$(ts)] ROUND $round/$FUZZ_ROUNDS starting random batch"
        for slot in $(seq 1 "$FUZZ_PARALLEL"); do
            (
                echo "[$(ts)] ROUND $round slot=$slot"
                run_random_job
            ) &
            sleep "$(random_start_delay)"
        done
        wait

        if [[ $((round % 2)) -eq 0 ]]; then
            run_difficult_sequence
        fi

        echo "[$(ts)] ROUND $round/$FUZZ_ROUNDS done"
    done

    echo "[$(ts)] fuzz run complete; logs in $LOG_DIR"
}

main "$@"
