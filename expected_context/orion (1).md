# Project Context — Orion Analytics Dashboard

- **Project ID:** `orion`
- **Client domain:** `orion.example`
- **Status as of:** 2026-11-21
- **Demo target:** 2026-12-10

## Purpose and scope
Executive analytics dashboard with weekly regional revenue, conversion rate, and support response-time KPIs. Recognized revenue = revenue net of refunds, attributed to billing region. Customer churn is deferred until after launch.

## Decisions and changes
- Churn requested as an unapproved proposal Nov 12; product director declined inclusion Nov 16.
- West region data missing for Nov 6–9, identified Nov 17; corrected feed validated Nov 20.
- Executives must access all regions; managers only their own regions.

## Current blockers / risks
- **Open:** negative-access permissions tests by Dec 7. Filtering is implemented in staging but not fully tested.
- **Resolved:** missing West region data after Nov 20 validation.

## Owners / next actions
- Sam (ContextLab): finish role-based access negative tests by Dec 7.
- Ella (Orion): confirm December 10 demo attendee list.

## Provenance
- `email-003`, `email-006`, `email-009`, `email-012`, `email-015`, `email-018`, `email-021`, `email-024`, `email-027`, `email-030`, `email-033`, `email-036`
- `meeting-003`, `meeting-006`

## Last update
2026-11-21 12:25 EST — Filtering implemented in staging; security/negative-access testing remains open.
