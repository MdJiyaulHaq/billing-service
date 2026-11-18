# NOTES

## Questions I would have asked, and the assumptions I made instead

**Returns**

- *What happens to the money on a paid invoice?* — Assumed: the invoice total is
  reduced and `amount_paid` is left untouched, so `balance` goes negative and
  represents a credit owed to the customer. No cash-refund payout flow exists.
  Alternative would be a `Refund` model recording money actually handed back.
- *Should returned goods always go back into sellable stock?* — Assumed yes.
  A real shop needs a "damaged / not restocked" flag per return line.
- *Who can approve a return — any staff, or owner/accountant only?* — Assumed
  any authenticated staff of the business. `StaffProfile.role` exists and would
  be the natural place to enforce this.
- *Is there a return window (e.g. 7 days)?* — Assumed no limit; the invoice
  status is the only gate.
- *Should a fully-returned invoice be auto-cancelled?* — Assumed no. It stays
  `issued`/`paid` with `total = 0`, which keeps the audit trail clean and still
  excludes it from revenue via the sales report.
- *Credit note numbering* — Assumed per-business sequence displayed as `CN-{n}`,
  mirroring invoice numbering.

**Payment reminders**

- *What cadence does the business want?* — Assumed: first reminder any time
  after `due_date`, then at most one per invoice every 3 days, max 4 reminders
  per invoice, ever.
- *What are acceptable sending hours?* — Assumed 08:00–20:00 local time
  (`Asia/Kathmandu`, already the project `TIME_ZONE`).
- *One SMS per invoice or per customer?* — Assumed per customer: all of a
  customer's overdue invoices are grouped into a single message. This is the
  direct answer to the "several messages a day" complaint.
- *SMS provider semantics* — `send_sms` was a synchronous helper with no retry.
  Reminders call it per customer inside try/except; a failed send is simply not
  recorded, so the next hourly run retries it.
- *Should reminders respect a per-customer opt-out?* — Assumed out of scope;
  noted as next step.

## Changes to existing code, and why

- **`services.py` → `services/` package** (`invoices.py`, `returns.py`,
  `reminders.py`, `__init__.py` re-exporting the public API). Split by domain
  so each feature owns its logic; all previous import paths still work.
- **`record_payment` hardened**: now locks the invoice row
  (`select_for_update`), rejects payments on cancelled invoices
  (`InvalidInvoiceState`), non-positive amounts (`InvalidAmount`), and amounts
  above the balance (`Overpayment`). Previously any amount could be recorded on
  any invoice — returns make overpayment a real scenario.
- **`cancel_invoice` restocks only unreturned quantities**
  (`quantity - returned_qty` via the `with_returned_quantities` annotation).
  Previously it restocked every line, which would double-count stock after a
  partial return.
- **`payments` action now uses `self.get_object()`** instead of an unscoped
  `Invoice.objects.get(pk=...)`. The old code let any authenticated user pay
  another business's invoice by guessing an ID (IDOR). Same fix pattern as the
  existing `cancel` action.
- **Invoice totals now use `Decimal` instead of `float`** in
  `calculate_totals`/`recalculate_invoice_totals`. The float math could drift
  by paise; `VAT_RATE` is now `Decimal("0.13")`.
- **`PaymentCreateSerializer.amount`** gained `min_value=Decimal("0.01")`.
- **`InvoiceViewSet.returns` action** added: `GET` lists credit notes for an
  invoice, `POST` creates one. Both go through `get_object()`, so tenancy is
  enforced.
- **`OutstandingCustomersView` SQL injection fixed** — `search` was
  f-string interpolated into raw SQL, allowing boolean-based blind extraction
  of arbitrary tables (verified live against `auth_user`). Now passed as a
  bound `%s` parameter.
- **Gateway payment idempotency** — `process_gateway_payment` retries on
  any exception, so an SMS failure after `record_payment` could double-record
  the payment. Added a `gateway_ref` existence check in the task plus a
  conditional `UniqueConstraint` on `Payment.gateway_ref` (non-empty only) so
  the database itself rejects duplicates.
- **Celery beat configured**: `CELERY_TIMEZONE = "Asia/Kathmandu"` and an
  hourly `send-overdue-reminders-hourly` schedule; new `beat` service in
  `docker-compose.yml`. The task itself enforces the 08:00–20:00 window, so
  hourly runs outside it are cheap no-ops.
- **New models**: `Return` (credit note, unique `(business, number)`),
  `ReturnItem` (price snapshot per line), `PaymentReminder` (audit row per
  invoice per SMS, indexed on `(invoice, -sent_at)`).

## N+1 avoidance

- `with_returned_quantities()` annotates `returned_qty` with a single
  `Coalesce(Sum(...))` — used by returns, cancel, and totals recompute.
- `process_return` fetches the invoice with `select_for_update` +
  `Prefetch(items → product)` in one query.
- `get_overdue_invoices` annotates `outstanding`, `reminder_count`, and
  `last_reminder_at` via aggregates and `select_related("customer",
  "business")` — the whole reminder scan is one query.
- Return list endpoint prefetches `items → invoice_item → product`.

## What I'd do next with another day

1. **Fix `next_invoice_number` / `next_return_number` races properly** — a
   per-business sequence row with `select_for_update`, or a DB sequence.
   `Return` already has a unique constraint so collisions fail loudly; invoices
   have no constraint at all.
2. **Refund/payout flow** for overpaid (credited) invoices, plus a customer
   credit balance view.
3. **Damaged-goods flag** on return lines (restock vs write-off).
4. **Role-based permissions** — restrict returns/payments/refunds by
   `StaffProfile.role`.
5. **SMS delivery status tracking** — `PaymentReminder` could store provider
   message IDs and delivery receipts; add retry with backoff per send.
6. **Customer opt-out / reminder preferences** and per-business reminder
   policy settings (cadence, quiet hours) instead of module constants.
7. **PDF credit note** generation for the accountant (currently the API/JSON
   representation + admin is the document).
8. **Invoice PATCH hardening** — `InvoiceSerializer` exposes `fields =
   "__all__"`, so totals/status can be rewritten directly; restrict writable
   fields.
