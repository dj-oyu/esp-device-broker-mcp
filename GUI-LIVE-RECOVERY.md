# Read-only live stream recovery

The GUI reads one Server-Sent Events stream, `GET /operator/events`, with the
same authentication, loopback/origin guard and principal scoping as the other
operator routes. The first event is a full `snapshot`; each observed broker
revision then sends one `delta` (bursts are coalesced into one capture; a
principal change sends `reset`). Each data event is one compact JSON envelope,
`{"ok": true, "schema_version": 1, "kind": ..., "cursor": ...}`, with an
`id: <epoch>:<sequence>` line. `Last-Event-ID` is not used: every (re)connect
gets a full snapshot under a new cursor. An idle stream sends the SSE comment
`: ping` every 25 seconds, and the client read timeout remains 35 seconds. At
most 8 streams are open; more are refused before the stream starts with the
JSON error `unavailable`. A refusal after the stream started is one final
`{"ok": false, "error": code}` event; a grant revoked while open ends the stream
like an HTTP 401/403. A stream that ends or drops is a retryable disconnect.
Server shutdown ends open streams before uvicorn waits for open responses.

A transient transport or allowlisted broker failure does not permanently stop
the view. Recovery waits 1, 2, 4, 8, 16, up to 30 seconds with 20 percent
jitter, and creates a new view generation that opens a new stream for a full
authenticated snapshot. Sixty seconds of validated events (heartbeats included)
resets the backoff. Manual Reconnect resets it immediately.

Only connect/read/write/pool timeouts, known network errors, an ended stream,
HTTP 408/429/500/502/503/504, and the fixed JSON error codes
unavailable/operator_timeline_unavailable are retried. Redirects, authentication
failures, other HTTP rejections, unknown server codes and unexpected exceptions
stop automatic retry. Invalid envelopes,
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
