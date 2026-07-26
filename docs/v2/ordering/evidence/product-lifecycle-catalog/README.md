# Product Lifecycle Ordering-catalog evidence

## Production owner-canary evidence

Authenticated production evidence was captured on 2026-07-25 with owner principal `6` against deployed commit `0eac95e22ac24543554193d8d7600cce11f7d505` and schema revision `20260725_0008`.

The owner explicitly accepted the single unresolved catalog-identity gap:

- expected mapped identities: `824`
- covered/named identities: `823`
- missing identities: `1`
- coverage: `99.88%`
- missing SKU: `Y956832`
- missing Square variation ID: `ELA77RJ6VMTS56DD2OOHLIZ7`
- vendor: `Vapetasia`
- current classification: absent from the current Square catalog response; deleted or otherwise unavailable remains unresolved

The missing variation remains visible, searchable by SKU, labeled `Product name unavailable`, and available for owner-controlled lifecycle management. Product-name search does not treat the SKU as a product name. The workspace continues to report partial catalog coverage and does not infer a deleted, discontinued, archived, or No Future Reorder state.

| Evidence | Viewport | Purpose |
|---|---:|---|
| [Desktop default](./production-desktop-default.jpg) | 1440 × 1000 | Default Product Lifecycle workspace, coverage state, filters, toolbar, and table |
| [Desktop Clickmate filter](./production-desktop-clickmate.jpg) | 1440 × 1000 | Product-name filtering and result readability |
| [Desktop selected rows](./production-desktop-selected.jpg) | 1440 × 1000 | Selection state and active bulk toolbar |
| [Desktop confirmation dialog](./production-desktop-confirmation.jpg) | 1440 × 1000 | Lifecycle confirmation dialog without submitting a mutation |
| [Archived Products restore view](./production-desktop-archived.jpg) | 1440 × 1000 | Archived-product recovery workspace |
| [Laptop Clickmate filter](./production-laptop-clickmate.jpg) | 1100 × 900 | Laptop responsive layout and filtering |
| [Mobile default](./production-mobile-default.jpg) | 390 × 844 | Mobile default responsive layout |
| [Mobile selected rows](./production-mobile-selected.jpg) | 390 × 844 | Mobile selection and bulk-action controls |
| [Mobile unavailable identity](./production-mobile-unknown-name.jpg) | 390 × 844 | Partial-coverage warning and accepted unavailable-name state |

At all three tested viewport sizes, the document had no horizontal overflow. Search, filters, selection, bulk controls, note input, buttons, labels, focus indicators, and lifecycle information remained usable and legible. Rows were selected and the confirmation dialog was opened only for evidence; no lifecycle action was submitted.

## Pre-deployment route evidence

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
