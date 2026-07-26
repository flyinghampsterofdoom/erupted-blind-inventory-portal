# Product Lifecycle Ordering-catalog evidence

Captured 2026-07-25 from the actual FastAPI/Jinja routes backed by a disposable PostgreSQL 16.12 database at schema `20260725_0008`. The synthetic production-shaped dataset contained 824 active/default mapped variations, 820 Ordering catalog-identity rows, four intentionally missing identities, one No Future Reorder product, one Archived product, and zero rows in both touchscreen cache tables.

No Square request or lifecycle mutation was made while capturing this evidence. The confirmation dialog was opened and cancelled. The disposable server and database were removed afterward.

| Evidence | Contract demonstrated |
|---|---|
| `desktop-default.png` | Real names from Ordering identity, 820/824 coverage warning, 823 non-archived population, supported/deferred filter boundary |
| `desktop-combined-search.png` | Product-name plus vendor filtering with stable total population context |
| `desktop-unknown-name-sku-search.png` and `desktop-selected-bulk.png` | SKU search remains functional without product names; explicit unknown-name state and retained unknown row |
| `desktop-selected-bulk.png` | Current-page-only selection, unknown-name row, and unchanged atomic lifecycle toolbar |
| `desktop-archived.png` | Archived Products recovery view and restore-to-prior-state evidence |
| `mobile-unknown-name.png` | Responsive coverage warning and owner-only refresh contract |
| `mobile-selected-bulk.png` | Responsive selected-row and bulk toolbar behavior |
| `mobile-batch-confirmation.png` | One confirmation per lifecycle batch; no confirmation was submitted |
| `mobile-archived.png` | Responsive Archived Products restore workflow |
