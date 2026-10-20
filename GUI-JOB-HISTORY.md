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

Failed rows use pale red; needs-review rows use pale amber, including reviewed
failures. Static ASCII shape badges plus short Japanese labels remain visible
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
record. This is display metadata only: fault acceptance, terminal validity, raw
ownership, review flags and retry behavior remain unchanged. No historical
record is rewritten to infer a more specific cause.
