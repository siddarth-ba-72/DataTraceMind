---
contract: orders
version: 1
status: approved
owner: demo analytics policy (the approved definition for this environment)
rules:
  fact_orders_scope: all_orders
  business_key: order_id
  business_timezone: Asia/Kolkata
---

# Orders data contract

The approved definition of the `fact_orders` dataset.

- **Grain:** exactly one row per source order (`order_id`)
- **Scope:** every order in `oltp.orders` appears in `fact_orders`, whatever
  its status and whether or not it has a payment. Unpaid and cancelled orders
  are valid orders.
- **Order count:** `daily_revenue.order_count` counts orders by the business
  day of `created_at`
- **Revenue is separate:** whether an order contributes revenue is decided by
  the revenue contract (`net_revenue`), not by whether the order row exists.
  Order-count correctness and revenue correctness are different checks.
- **Uniqueness:** `order_id` is unique in `fact_orders`; `payment_id` is
  unique in `fact_payments`; `refund_id` is unique in `fact_refunds`
- **Completeness:** every source row has a matching fact row, and no fact row
  references a source row that does not exist
