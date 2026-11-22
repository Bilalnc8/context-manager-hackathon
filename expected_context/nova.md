# Project Context — Nova Inventory Sync

- **Project ID:** `nova`
- **Client domain:** `nova.example`
- **Status as of:** 2026-11-21
- **Target pilot:** 2026-12-08 — **not yet cleared for launch**

## Purpose and scope
Inventory sync from warehouse to storefront. Target refresh every 15 minutes; queue updates during warehouse maintenance (01:00–01:30 EST), retry afterward, and show stale-stock status to operators.

## Decisions and changes
- Preserve SKU identifiers as strings, including leading zeroes. Use the approved crosswalk.
- Sandbox credentials supplied Nov 6; production credentials approved Nov 18 and to be delivered securely via secrets vault.
- Seven SKU reconciliation exceptions outstanding Nov 17; resolved in staging Nov 19.
- Production cutover rehearsal scheduled Dec 3 (not completed).

## Current blockers / risks
- **Open:** complete Dec 3 cutover rehearsal, including rollback.
- **Open:** verify stale-stock alert is visible to operators before pilot sign-off.
- **Resolved:** production credential approval; SKU staging exceptions.

## Owners / next actions
- Priya (ContextLab): run cutover/rollback rehearsal Dec 3.
- Daniel (Nova): verify operator-facing stale-stock alert and confirm readiness.

## Provenance
- `email-002`, `email-005`, `email-008`, `email-011`, `email-014`, `email-017`, `email-020`, `email-023`, `email-026`, `email-029`, `email-032`, `email-035`
- `meeting-002`, `meeting-005`

## Last update
2026-11-21 10:40 EST — Dec 3 cutover rehearsal scheduled; not yet performed.
