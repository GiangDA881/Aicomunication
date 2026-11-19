# ADR-0003: PostgreSQL as the event bus, no Redis in the prototype

Status: accepted (2026-11-19)

Events must survive restarts and be auditable; Postgres already stores them. Delivery uses a
row per (event, recipient) with status pending/acked/failed/expired, picked with
`FOR UPDATE SKIP LOCKED` on Postgres. Dedup is a unique `idempotency_key`.
Redis can be added later for rate-limit counters or pub/sub fan-out if latency requires it.
