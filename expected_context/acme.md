# Project Context — Acme Customer Portal

- **Project ID:** `acme`
- **Client domain:** `acme.example`
- **Status as of:** 2026-11-21
- **Pilot:** 2026-12-05 — **conditionally approved; final go/no-go pending**

## Purpose and scope
Customer portal with login, invoice history/PDF download, and support-request form. Online payments excluded from phase one. Chatbot explicitly deferred to phase two (approved Nov 11).

## Decisions and changes
- Original pilot target Dec 1; proposed Dec 5 on Nov 12; client conditionally approved Dec 5 on Nov 16 and reaffirmed Nov 20.
- Invoice filename bug discovered Nov 17, fixed and regression-tested Nov 19.
- Chatbot discussed Nov 5, not approved then; explicit decision Nov 11 to defer.

## Current blockers / risks
- **Open:** security sign-off pending. Penetration test has two low-severity items requiring documented mitigation (Nov 21).
- **Open:** deliver QA evidence and security sign-off by Dec 3 before final go/no-go.
- **Resolved:** invoice PDF filename bug (Nov 19).

## Owners / next actions
- Alex (ContextLab): provide QA results and penetration-test summary/mitigations by Dec 2–3.
- Jordan (Acme security): review mitigations and sign off.
- Mia (Acme): make final pilot go/no-go decision.

## Provenance
- `email-001`, `email-004`, `email-007`, `email-010`, `email-013`, `email-016`, `email-019`, `email-022`, `email-025`, `email-028`, `email-031`, `email-034`
- `meeting-001`, `meeting-004`

## Last update
2026-11-21 08:30 EST — Security review not yet approved; two low-severity mitigations outstanding.
