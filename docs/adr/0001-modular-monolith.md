# ADR-0001: Modular monolith with two processes

Status: accepted (2026-11-19)

Context: one server, a small team, and the strongest requirement is consistent authorization.
Decision: one Python package `agenthub`; processes `api` (FastAPI) and `bot` (discord.py) both call
the same service layer, which is the only place authorization happens.
Consequences: simple deploy and testing; scaling beyond one host would need the bus worker split
out first, which the event table already allows.
