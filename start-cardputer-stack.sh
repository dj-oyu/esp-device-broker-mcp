# shellcheck shell=bash
# Start the broker, the Claude gateway and the read-only web UI, in that order. There is no
# queue-manager window on Linux (the Linux build has no GUI).
#
#   . ./start-cardputer-stack.sh --exe /path/to/cardputer --profile /path/to/device.local.toml
#
# Same options as start-cardputer.sh, and sourced the same way (". ./...", not "./..."), so the
# broker's run tokens stay in this prompt for your clients. This is the Linux counterpart of
# Start-CardputerStack.ps1 without its GUI step.
#
# 1. start-cardputer.sh starts the authenticated broker in the background and waits until it is idle.
# 2. `cardputer gateway --config <profile>` runs in the background. Funnel opens there only when
#    the profile's [gateway] sets funnel = true.
# 3. `cardputer webui --config <profile>` runs in the background. tailscale serve (tailnet only)
#    runs there only when the profile's [webui] sets serve = true.
#    Steps 2 and 3 get no CARDPUTER_* or OPENAI_* variables: each reads only the key files named
#    in the profile. Without its section a helper prints *_config_missing and exits; the others
#    are unaffected. Their output goes to gateway.log and webui.log next to broker.log.
#
# Nothing here stops an existing process; stop each with the "kill" command printed for it. See
# CLAUDE-GATEWAY.md and WEB-UI.md for their one-time setup.

if [ -z "${BASH_VERSION-}" ]; then
    echo 'start-cardputer-stack.sh needs bash.' >&2
    return 2 2>/dev/null || exit 2
fi
if ! (return 0 2>/dev/null); then
    echo 'Source this script instead of running it:  . ./start-cardputer-stack.sh --exe ... --profile ...' >&2
    exit 2
fi

# $1 = subcommand (gateway or webui). Uses the variables the broker script leaves behind.
_cardputer_start_helper() {
    local subcommand=$1 log="$_CARDPUTER_LOG_DIR/$1.log" name pid
    local drop=()
    # The helpers must not inherit any run token or API key from this prompt.
    while IFS= read -r name; do
        case $name in CARDPUTER_* | OPENAI_*) drop+=(-u "$name") ;; esac
    done < <(compgen -e)
    (umask 077 && : >>"$log") || { echo "Cannot create the log $log." >&2; return 1; }
    (cd -- "$_CARDPUTER_PROFILE_DIR" && exec env "${drop[@]}" "$_CARDPUTER_EXE" "$subcommand" --config "$_CARDPUTER_PROFILE") \
        </dev/null >>"$log" 2>&1 &
    pid=$!
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "The $subcommand is running (pid $pid, log $log). Stop it with: kill $pid"
    else
        echo "The $subcommand exited; this is expected without its table in the profile. Last log lines ($log):"
        tail -n 5 -- "$log"
    fi
}

_cardputer_start_stack() {
    local here
    here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
    # 1. Broker (refuses if one is already listening; nothing is stopped or changed then).
    # shellcheck source=start-cardputer.sh
    . "$here/start-cardputer.sh" "$@" || return $?
    # 2 and 3. Helpers.
    _cardputer_start_helper gateway
    _cardputer_start_helper webui
    echo 'Closing this prompt ends the broker, the gateway and the web UI; device jobs are unaffected by the helpers.'
}

_cardputer_start_stack "$@"
