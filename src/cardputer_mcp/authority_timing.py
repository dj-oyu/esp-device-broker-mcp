"""How long a worker thread waits for the event loop to answer an authority check.

A build or fetch runs for minutes and asks the loop whether it is still allowed to
continue. The loop can be busy for several seconds with another client's request
(a source-cache scan, hashing a firmware image for a flash plan). That stall is
not a revoked authority, so the wait is generous; a loop that stays unresponsive
longer than this still fails closed.
"""

AUTHORITY_CHECK_SECONDS = 30.0
