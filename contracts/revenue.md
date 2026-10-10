---
contract: net_revenue
version: 1
status: approved
owner: demo finance policy (the approved definition for this environment)
rules:
  payment_statuses_included: [CAPTURED]
  refund_statuses_subtracted: [SUCCEEDED]
  refund_amount_basis: refund_amount
  business_timezone: Asia/Kolkata
  currency_handling: separate
  reconciliation_tolerance: 0.01
---

# Revenue metric contract

The approved definition of `net_revenue` for the order-to-revenue pipeline.
The block above is the machine-readable form that the quality checks
enforce; the text below explains it. Where they differ, the block wins.

- **Metric:** `net_revenue`
- **Grain:** reporting currency and calendar day in the business timezone
- **Include:** payments whose `payment_status` is `CAPTURED`, at the paid
  amount, on the business day of `paid_at`
- **Subtract:** refunds whose `refund_status` is `SUCCEEDED`, on the business
  day the refund was created
- **Exclude:** failed payments; orders with no captured payment; refunds that
  are `PENDING` or `FAILED`
- **Partial refunds:** subtract `refund_amount` (the amount actually
  refunded), never the full order amount
- **Currency:** aggregate each currency separately; no conversion rule is
  approved
- **Time basis:** `Asia/Kolkata`
- **Reconciliation:** net revenue derived from the source by this definition
  must equal the analytics `daily_revenue.net_revenue` total per currency
  within 0.01
- **Changes:** changing any rule here requires a new contract version and an
  explicit approval. A pipeline repair may bring the transformation back in
  line with this contract; it may not change the contract.

## Source status values

| Table | Column | Values |
|---|---|---|
| `oltp.payments` | `payment_status` | `CAPTURED`, `FAILED` |
| `oltp.refunds` | `refund_status` | `PENDING`, `SUCCEEDED`, `FAILED` |

This is a demo policy, not a universal accounting definition.
