"""inbox-to-task — email → MS To Do task creation.

Per-capability directory created 2026-06-02 (Phase 4 of the architectural
refactor). All modules here are thin re-exports pointing at the canonical
homes under `kavi_runtime/`. The directory is the Investigator's entry
point: when an email-classification or task-creation bug fires, this is
the one place to look.
"""
