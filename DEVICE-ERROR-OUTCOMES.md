# Device errors and terminal job outcomes

`DERBY_FIXED_END valid=1` describes the measured repetitions. It does not prove
that the application stopped successfully. New fixed workload plans explicitly
select `pocketjs-derby-fixed-v2`, with terminal contract
`end_owner_exit_stop_home_v2`. The owner-exit wire definition is pinned in the plan.

New trials require exactly one ordered sequence:

1. `DERBY_FIXED_END v=1 session=N reps=3 valid=1 quality=0 independent=0`
2. `DERBY_DETAIL_EXIT v=1 session=N clean=1`
3. `APP_STOPPED`, bare or under the firmware's `app` info logger
4. `HOME_READY`, bare or under the firmware's `shell` info logger
5. A quiet, complete receive boundary and confirmed port close

EXIT proves only the owner's normal-stop decision. It does not attest teardown,
the physical display, or the installed image. Exceptions, stop-hook failures,
timeouts, watchdogs, resets, missing/duplicate/mismatched/out-of-order EXIT, and
missing STOP/HOME still fail. In particular, `InternalError: interrupted` remains
a VM fault even after END, EXIT, STOP, or HOME. Normal owner stops use protocol
markers; no exception message is treated as an expected stop.

Firmware without the new EXIT contract cannot pass a new fixed workload request.
There is no fallback to the old success rule. A partial trial's bounded private
raw bytes and receipt remain available to its requesting principal when a job
needs review. Public diagnostics contain fixed categories and observed-marker
booleans, never exception text. Observed HOME alone does not clear uncertainty or
authorize another operation. Close failures retain the preceding fault category.

Historical job state and failure results are never rewritten by this change.
The `review` summary separates historical failure from resolved uncertainty;
current `status.safety` reports resource uncertainty and required next action.
Neither report grants authority or proves a physical device state. A pending
review requires inspection and, only where eligible and explicitly requested,
the existing HOME recovery flow. A resolved review retains the historical error.

Old fixed-workload reports preserve their private raw evidence and historical
validation flag, while reporting the legacy contract and
`terminal_success_verified=false`. Old streams are never upgraded to v2 proof.
New validation can flag a historical stream's device error without modifying its
stored job outcome. Work verification and terminal success are separate fields.

Validation uses synthetic serial bytes and public fixture data. It does not open
a real device, deploy software, restart a service, or change authorization.
