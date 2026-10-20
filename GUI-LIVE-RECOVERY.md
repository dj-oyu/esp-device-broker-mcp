# Read-only live subscription recovery

The GUI uses bounded JSON long polling, not SSE. Broker idle heartbeats remain
25 seconds and client read timeout remains 35 seconds. A transient transport or
allowlisted broker failure no longer permanently stops the view. Recovery waits
1, 2, 4, 8, 16, up to 30 seconds with 20 percent jitter, and creates a new view
generation with cursor=null for a full authenticated snapshot. Sixty seconds of
validated responses resets the backoff. Manual Reconnect resets it immediately.

Only connect/read/write/pool timeouts, known network errors, HTTP 408/429/500/502/
503/504, and the fixed JSON error codes unavailable/operator_timeline_unavailable
are retried. Redirects, authentication failures, other HTTP rejections, unknown
server codes and unexpected exceptions stop automatic retry. Invalid envelopes,
reducer continuity failures or delivery overflow permit one full resync; repetition
before a stable minute stops retry. Existing bounds, token origin/redirect rules,
authorization checks and stale-generation fences remain in place.

Connection details retain at most 32 safe failure records and display the latest
four: UTC, generation, elapsed milliseconds, HTTP status, fixed reason and an
allowlisted error code. Bodies, arbitrary exception text, URLs, credentials and
unknown server error strings are never copied. Last validated response time is
separate from job timestamps. A disconnected view keeps its last validated jobs
and disables actions; it does not claim the broker or those jobs stopped.

The retry timer is owned by the Tk drain loop and respects close and pending
operator actions. Transport replacement cancels outstanding read tasks. It never
replays execute, cancel, release, reorder or any device request. A new broker
epoch continues to clear old selection under the existing reducer contract.
No broker restart, deployment, permission change or device operation is included.

Native Windows verification should cover sleep/wake and transient loopback loss,
manual Reconnect during backoff, closing during backoff, a running job continuing
while the display is stale, and safe rejection diagnostics. Use synthetic HTTP
faults for authentication/status/invalid-data cases; do not disrupt active device
measurement merely to induce a network error.
