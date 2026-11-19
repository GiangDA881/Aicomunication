# ADR-0004: Only human maintainers change the verified knowledge set

Status: accepted (2026-11-19)

Agents and debates can only create `proposed` records. Verify, reject, supersede and deprecate
require a human with the project `maintainer` role. Every change to the verified set bumps
`projects.context_version`, which is how drift is detected. Agent consensus is never treated as
verification.
