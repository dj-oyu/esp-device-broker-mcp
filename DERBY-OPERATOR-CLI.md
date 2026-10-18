# DERBY approval launch entry — retired

The former product approval, capture-policy and operator-launch flows are retired.
Do not arm a slot, enter an approval keyword, create a permission rule or invoke
an approval command. Legacy mutation routes authenticate and check scopes before
returning `410 Gone`; they never execute work.

Use [prepare and explicit execute](README.md#prepare-and-explicitly-execute).
The authenticated requesting principal may retrieve its successful capture within
the original retention period without a separate image-delivery decision.
See [capture](CAPTURE.md) and [bounded HOME recovery](HOME-RECOVERY.md).

Old approval/grant records remain history only. Upgrade cancels legacy pending
work without replay and preserves uncertain-running recovery barriers.
This change does not alter agent or host platform approval controls.
The previous design remains available in Git history; it is not a current runbook.
