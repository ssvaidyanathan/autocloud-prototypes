# Application Topology: Cymbal Orders demo

This file is the single source of truth for how the application's components depend on each other.
The triage agent reads it to group related alerts, find the origin (the component that broke first),
and work out the blast radius (everything that depends on the origin).

To adapt it to another application, edit the two tables below. No code changes are needed.

`{prefix}` stands for the stack's `NAME_PREFIX` (for example `ata`). The loader substitutes it
when the agent or tools start, so one file serves every stack. Startup fails if `NAME_PREFIX` is
unset or invalid, or if any `{...}` placeholder is left after substitution.

## Components

Every component that can raise an alert or be affected by one.

| Component | Type | Also known as (alert labels) | Notes |
|---|---|---|---|
| {prefix}-orders-v1 | Apigee proxy | {prefix}-apigee-orders-v1, orders-v1 | Public orders API (`/{prefix}/orders/v1`) |
| {prefix}-inventory-v1 | Apigee proxy | {prefix}-apigee-inventory-v1, inventory-v1 | Public inventory API (`/{prefix}/inventory/v1`) |
| {prefix}-orders-api | Cloud Run | orders-api | Places orders and reserves stock |
| {prefix}-inventory-api | Cloud Run | inventory-api | Stock levels and reservations |
| {prefix}-inventory-db | Cloud SQL | inventory-db | PostgreSQL inventory database |

Types: `Apigee proxy`, `Cloud Run`, `Cloud SQL`.

## Dependencies

Read each row as "**From** depends on **To**". If **To** fails, **From** is affected.

| From | To | How |
|---|---|---|
| {prefix}-orders-v1 | {prefix}-orders-api | routes traffic to |
| {prefix}-inventory-v1 | {prefix}-inventory-api | routes traffic to |
| {prefix}-orders-api | {prefix}-inventory-api | calls |
| {prefix}-inventory-api | {prefix}-inventory-db | reads and writes |

## What the agent derives from this

Shown here for reference only. The agent computes this; you don't maintain it.

```
{prefix}-orders-v1 ──► {prefix}-orders-api ──┐
                                   ├──► {prefix}-inventory-api ──► {prefix}-inventory-db
{prefix}-inventory-v1 ─────────────────┘
```

- If **{prefix}-inventory-db** fails, the blast radius is {prefix}-inventory-api, {prefix}-orders-api, {prefix}-orders-v1 and {prefix}-inventory-v1.
- If **{prefix}-orders-v1** returns 429s while {prefix}-orders-api is healthy, the origin stays **{prefix}-orders-v1**.
