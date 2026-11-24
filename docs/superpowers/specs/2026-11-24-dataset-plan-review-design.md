# Dataset plan review corrections

The user approved four fixes in this order: reliable deselection through
extraction, navigation past an empty concept, contextual revision feedback,
and distinct zero-selection approval/revision behavior. Verify each before
starting the next. Do not deploy or change historical conversations.

1. Treat the approved plan's concept and required fields as the complete
   output selection. Operation, filter, join and reduction references may
   support SQL without becoming output permissions. Reject SQL projecting
   unchecked fields before execution, return repair feedback for the same
   frozen plan, and explicitly tell the model which outputs were approved.
2. Allow continuing past a concept whose fields are all unchecked. Selection
   state must survive navigation and switching review modes.
3. Send an optional active concept ID with plan revision decisions. Validate
   it against the review. Give the model explicit selected/excluded fields
   and that scope so checkbox intent resolves vague feedback where possible.
4. Disable empty final approval with an explanation. Reject empty approval
   through the API before consuming its interrupt. Accept nonempty revision
   feedback with zero selections, retaining the original draft and explicit
   exclusions as revision context rather than validating an empty executable
   candidate. Preserve cancellation and legacy callers without scope.

Use Python 3.12, existing dependencies, focused regression tests, and a
dedicated real FastAPI/compiled frontend/provider browser smoke per fix.
Each real smoke runs once with a five-minute limit and preserves diagnostics.
Rebuild frontend/dist and refresh its manifest for every frontend change.

Alternatives considered: changing only the navigation button leaves the
observed extraction failure intact; handling checkbox changes only through
natural language adds ambiguity and model turns. The approved approach fixes
the existing structured review contract at each boundary.
