# Job history presentation

The live GUI lists the latest 32 registrations, newest first, independent of
state, review flags and retained images. Registration sequence is durable and
assigned on the first plan/job insert. Executing a prepared plan, finishing,
reviewing or updating logs does not move its row. The sequence, not a changing
wall clock or a random job identifier, breaks ties and survives restart.
No job, plan, outcome or review record is deleted or rewritten by this display.

An additive `timeline_order` table and insert triggers record display metadata.
Existing executed jobs are seeded by their creation timestamp and insertion
order. Legacy unexecuted plans have no creation timestamp: their existing plan
insertion order is preserved as an undated prefix older than dated jobs. Exact
interleaving of these legacy entries cannot be reconstructed. No migration-time
clock value is presented as their creation time. New registrations have UTC
creation timestamps; original execution/finish evidence remains in job history.

Confirmed failed outcomes use pale red; unknown outcomes use pale amber.
Confirmation status is a separate column, not a background-color decision. Static ASCII shape badges plus short Japanese labels remain visible
when Windows selected-row colors override backgrounds. Unreviewed and reviewed
history have distinct text. High-contrast mode uses system text/background
colors with the same badges. No emoji font, animation, fabricated progress,
folding, state-priority sorting or automatic review is introduced.

History entries outside the existing 32-row bound are not deleted. Current broker
activity, historical review presence and current ownership uncertainty remain
separate broker status fields; an old active/review/image row is no longer pinned
above newer registrations. Viewing history never starts or retries a job.

Native Windows acceptance: default and high-contrast themes, selected/unselected
failed and both review variants, Japanese fallback fonts, 100/150/200 percent DPI,
keyboard focus/selection/scroll preservation, and a prepare-to-terminal sequence.
Track the pre-existing Tk top-visible-row anchor issue separately. This change
does not deploy, restart or operate a physical device.

Selected failed/review jobs show a bold short reason derived only from fixed
result/category/detail tokens. Timeline SQL extracts only two bounded diagnostic
scalars, never raw serial evidence or a full diagnostic document. Unknown details
are omitted. A generic fault does not establish OOM: old records without a
specific hint remain generic. Future existing-fault observations can attach a
fixed OOM hint from an explicit OOM record, or an APP_LOAD compile-out-of-memory
record. The startup-failure label additionally requires an observed START_FAILED;
otherwise only the memory-shortage observation is shown. This is display metadata only: fault acceptance, terminal validity, raw
ownership, review flags and retry behavior remain unchanged. No historical
record is rewritten to infer a more specific cause.


## Display model axes

This GUI-only follow-up does not migrate or redefine backend states. Lifecycle,
outcome, diagnostic reason and historical acknowledgement are separate fields;
current resource/ownership uncertainty remains the independent broker status.
The raw backend record remains visible in selected details for diagnosis.

Legacy needs_review is not an outcome. Only serial_fault_marker,
derby_fixed_trial_invalid, serial_capture_invalid, serial_capture_timeout and
source_cache_scan_limit establish a failed requested result in this conservative
presentation mapping. Other legacy needs_review records show result unknown,
including restart, missing device acknowledgements, uncertain close/write and unknown
codes. This list does not change admission, cancellation or recovery gates.

Unreviewed/acknowledged badges occupy their own column. Acknowledging a record
never makes its result successful or known. A confirmed fault stays failed after
acknowledgement; an unknown write/completion stays unknown. A failed measurement
also does not establish the current physical device state. The existing raw
needs_device_review property and all backend histories remain unchanged.

The broker's existing execution_uncertain flag is now read for display alongside
ownership_uncertain. Older snapshots that omit it show not reported, never false.
These current fields are not inferred from review acknowledgements and are not
used to change existing GUI admission/cancellation/recovery decisions.
