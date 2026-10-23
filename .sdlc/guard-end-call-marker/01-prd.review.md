# Review: 01-prd.md (guard-end-call-marker)
VERDICT: GREEN

1. [minor] The Scope line says a rejected marker-only reply "falls through" to the never-go-silent branch, but that branch is gated on `not directives` (`services/conversation/pipeline.py:1382`), so with the rejected EndCallDirective still in `directives` it never fires and the caller gets dead air — Scope bullet 5 / AC16 — fix: state that a rejected EndCallDirective is removed from the turn's directive list (or the branch gate excludes rejected markers) so AC16's fallback is actually reachable.
