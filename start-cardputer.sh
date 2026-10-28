# shellcheck shell=bash
# Start ONE authenticated broker from the packaged `cardputer` executable and a broker profile.
#
#   . ./start-cardputer.sh --exe /path/to/cardputer --profile /path/to/device.local.toml
#
# SOURCE this script from your existing bash prompt (". ./start-cardputer.sh", not
# "./start-cardputer.sh" or "bash start-cardputer.sh") and keep that prompt for clients: a
# child process cannot put the run tokens into its parent's environment, so the clients you
# start from this prompt would not be authenticated. This is the Linux counterpart of
# Start-Cardputer.ps1; the options mean the same.
#
# Options:
#   --exe FILE           packaged cardputer executable (required)
#   --profile FILE       broker profile .toml (required)
#   --url URL            loopback broker URL for the listener and readiness checks
#                        (default http://127.0.0.1:8765/mcp; a profile that listens elsewhere
#                        needs a matching --url)
#   --no-serial          leave out --enable-serial (a serial profile is then refused)
#   --key-registry FILE  use an existing key registry instead of the default one
#   --ready-timeout SEC  10..60 (default 20)
#   --log-dir DIR        where the broker log goes (default ${XDG_STATE_HOME:-~/.local/state}/cardputer)
#
# The profile is never read here; only the executable loads and validates it and refuses with
# its own reason (the last log lines are shown on a failed start). No USB operation is started.
# The broker runs as a background job of THIS shell; closing the terminal ends it, as closing
# the broker console does on Windows. Nothing here stops an existing process: stop your own
# broker with the "kill" command printed at the end, after checking that no job is active.

if [ -z "${BASH_VERSION-}" ]; then
    echo 'start-cardputer.sh needs bash.' >&2
    return 2 2>/dev/null || exit 2
fi
if ! (return 0 2>/dev/null); then
    echo 'Source this script instead of running it:  . ./start-cardputer.sh --exe ... --profile ...' >&2
    echo 'The run tokens must stay in this prompt for the clients you start from it.' >&2
    exit 2
fi

# Absolute path of an existing regular file, or failure.
_cardputer_abs_file() {
    local directory
    [ -f "$1" ] || return 1
    directory=$(cd -- "$(dirname -- "$1")" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "${directory%/}" "$(basename -- "$1")"
}

# True when something already accepts a TCP connection on 127.0.0.1:$1.
_cardputer_port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# 64 hex characters from the kernel's random source; never printed or saved.
_cardputer_token() {
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
}

# One bounded authenticated `status` call (read-only): no lease, USB or device command.
_cardputer_status() {
    local exe=$1 url=$2 directory=$3
    if command -v timeout >/dev/null 2>&1; then
        (cd -- "$directory" && timeout 5 "$exe" --url "$url" --auth-source environment status 2>/dev/null)
    else
        (cd -- "$directory" && "$exe" --url "$url" --auth-source environment status 2>/dev/null)
    fi
}

cardputer_start_broker() {
    local exe="" profile="" url="http://127.0.0.1:8765/mcp" serial=1 key_registry=""
    local ready_timeout=20 log_dir=""
    while [ $# -gt 0 ]; do
        case $1 in
            --no-serial) serial=0; shift; continue ;;
            --exe | --profile | --url | --key-registry | --ready-timeout | --log-dir)
                if [ $# -lt 2 ]; then echo "$1 needs a value." >&2; return 2; fi ;;
            *) echo "Unknown option: $1" >&2; return 2 ;;
        esac
        case $1 in
            --exe) exe=$2 ;;
            --profile) profile=$2 ;;
            --url) url=$2 ;;
            --key-registry) key_registry=$2 ;;
            --ready-timeout) ready_timeout=$2 ;;
            --log-dir) log_dir=$2 ;;
        esac
        shift 2
    done

    if [ -z "$exe" ] || [ -z "$profile" ]; then
        echo 'Both --exe (path to cardputer) and --profile (broker profile TOML) are required.' >&2
        return 2
    fi
    local exe_path profile_path
    exe_path=$(_cardputer_abs_file "$exe") || { echo 'The --exe file was not found.' >&2; return 2; }
    [ -x "$exe_path" ] || { echo 'The --exe file is not executable (chmod +x).' >&2; return 2; }
    profile_path=$(_cardputer_abs_file "$profile") || { echo 'The --profile file was not found.' >&2; return 2; }
    case $profile_path in
        *.toml) ;;
        *) echo '--profile must be a broker profile .toml file.' >&2; return 2 ;;
    esac
    if [[ ! $url =~ ^http://(127\.0\.0\.1|localhost):([0-9]+)(/[^[:space:]]*)?$ ]] || [ "${BASH_REMATCH[2]}" -lt 1 ]; then
        echo '--url must be a loopback http URL such as http://127.0.0.1:8765/mcp.' >&2
        return 2
    fi
    local port=${BASH_REMATCH[2]}
    case $ready_timeout in
        '' | *[!0-9]*) echo '--ready-timeout must be 10..60.' >&2; return 2 ;;
    esac
    if [ "$ready_timeout" -lt 10 ] || [ "$ready_timeout" -gt 60 ]; then
        echo '--ready-timeout must be 10..60.' >&2
        return 2
    fi
    if [ -n "$key_registry" ]; then
        key_registry=$(_cardputer_abs_file "$key_registry") || { echo 'The --key-registry file was not found.' >&2; return 2; }
    fi
    # The broker and the readiness check run from the profile folder, so a relative state_dir
    # resolves there, exactly as in the Windows launcher.
    local profile_dir
    profile_dir=$(dirname -- "$profile_path")
    [ -n "$log_dir" ] || log_dir=${XDG_STATE_HOME:-$HOME/.local/state}/cardputer
    local log="$log_dir/broker.log"

    if _cardputer_port_open "$port"; then
        echo "Startup refused (listener_exists on 127.0.0.1:$port). If your own broker is running, check that no job is active, then stop it; if the profile uses another port, pass --url. No existing broker or credentials were changed." >&2
        return 1
    fi
    (umask 077 && mkdir -p -- "$log_dir" && : >>"$log") || { echo "Cannot create the log $log." >&2; return 1; }

    local had_local=0 had_remote=0 previous_local="" previous_remote=""
    if [ -n "${CARDPUTER_LOCAL_TOKEN+x}" ]; then had_local=1; previous_local=$CARDPUTER_LOCAL_TOKEN; fi
    if [ -n "${CARDPUTER_REMOTE_TOKEN+x}" ]; then had_remote=1; previous_remote=$CARDPUTER_REMOTE_TOKEN; fi
    # Both fresh values are made together, only after the existing-listener check.
    local new_local new_remote
    new_local=$(_cardputer_token)
    new_remote=$(_cardputer_token)
    while [ -z "$new_local" ] || [ "$new_local" = "$new_remote" ]; do new_remote=$(_cardputer_token); done
    export CARDPUTER_LOCAL_TOKEN=$new_local CARDPUTER_REMOTE_TOKEN=$new_remote
    new_local="" new_remote=""

    # Arguments carry paths only. Tokens travel only through the inherited environment.
    # --enable-serial only permits the backend; USB opens only on approved device work.
    local command=("$exe_path" --auth-source environment serve --config "$profile_path")
    [ -z "$key_registry" ] || command+=(--key-registry "$key_registry")
    command+=(--require-auth)
    [ "$serial" -eq 0 ] || command+=(--enable-serial)
    (cd -- "$profile_dir" && exec "${command[@]}") </dev/null >>"$log" 2>&1 &
    local pid=$!

    local deadline=$((SECONDS + ready_timeout)) status="" reason=readiness_timeout ready=0
    while [ "$SECONDS" -lt "$deadline" ]; do
        if ! kill -0 "$pid" 2>/dev/null; then reason=broker_exited; break; fi
        status=$(_cardputer_status "$exe_path" "$url" "$profile_dir") || status=""
        if [[ $status =~ \"ok\":\ *true ]] && [[ $status =~ \"backend\":\ *\"([^\"]+)\" ]]; then
            local backend=${BASH_REMATCH[1]}
            if [[ $status =~ \"(active_job|owner)\":\ *(\"|\{|\[) ]] || [[ $status =~ \"hardware_connected\":\ *true ]]; then
                reason=broker_not_idle
            else
                ready=1
            fi
            break
        fi
        sleep 0.5
    done
    if [ "$ready" -ne 1 ]; then
        if [ "$had_local" -eq 1 ]; then export CARDPUTER_LOCAL_TOKEN=$previous_local; else unset CARDPUTER_LOCAL_TOKEN; fi
        if [ "$had_remote" -eq 1 ]; then export CARDPUTER_REMOTE_TOKEN=$previous_remote; else unset CARDPUTER_REMOTE_TOKEN; fi
        echo "Authenticated startup not confirmed ($reason). The executable's own refusal is in $log (for example serial_runtime_opt_in_required, or a profile error); if --enable-serial was the cause rerun with --no-serial; if the profile uses another port, pass --url." >&2
        tail -n 10 -- "$log" >&2
        if kill -0 "$pid" 2>/dev/null; then
            echo "A broker (pid $pid) is still running. Do not rerun until you have inspected it and stopped your own failed or late-starting broker; no broker was killed." >&2
        fi
        return 1
    fi
    # Not exported: plain shell variables for the sibling scripts and for "kill".
    CARDPUTER_BROKER_PID=$pid
    _CARDPUTER_EXE=$exe_path _CARDPUTER_PROFILE=$profile_path _CARDPUTER_PROFILE_DIR=$profile_dir
    _CARDPUTER_LOG_DIR=$log_dir _CARDPUTER_URL=$url
    echo "Ready: authenticated $backend broker is idle. USB is closed; no device command was sent."
    echo "Profile: $profile_path"
    echo "Log: $log"
    echo 'Use THIS original prompt for client commands:'
    printf '  %q --url %q --auth-source environment status\n' "$exe_path" "$url"
    echo "Stop the idle broker with: kill $pid   (clear the tokens with: unset CARDPUTER_LOCAL_TOKEN CARDPUTER_REMOTE_TOKEN)"
    if [[ $status =~ \"needs_review\":\ *true ]]; then
        echo 'Warning: earlier work needs offline review before new device jobs can run.' >&2
    fi
    return 0
}

cardputer_start_broker "$@"
