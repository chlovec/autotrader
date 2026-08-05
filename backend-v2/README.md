# Backend v2

A from-scratch reimplementation of the autotrader backend (`backend/`, `engine/`, `db/`
at the repo root), living alongside v1 rather than replacing it in place. Still Python,
but with a different architecture than v1's FastAPI + SQLAlchemy + SQLite setup - the
specific framework, storage, and module layout are not decided yet.

## Status

Scaffolding only. Nothing here runs yet. v1 is unaffected and keeps running as-is until
v2 is ready to take over (or the two are run side by side - not yet decided).

## Open decisions

- Web framework (v1 uses FastAPI)
- Storage (v1 uses SQLite via SQLAlchemy)
- Whether v2 keeps v1's REST API contract (so the existing `frontend/` could point at
  either backend interchangeably) or is a free redesign
- Module/package layout
- How v1 and v2 relate at runtime: replacement, side-by-side, or staged migration
