# Application Topology: Cymbal Orders demo

This file is the single source of truth for how the application's components depend on each other.
The triage agent reads it to group related alerts, find the origin (the component that broke first),
and work out the blast radius (everything that depends on the origin).

To adapt it to another application, edit the two tables below. No code changes are needed.

## Components

Every component that can raise an alert or be affected by one.

| Component | Type | Also known as (alert labels) | Notes |
|---|---|---|---|
| ata-orders-v1 | Apigee proxy | ata-apigee-orders-v1, orders-v1 | Public orders API (`/ata/orders/v1`) |
| ata-inventory-v1 | Apigee proxy | ata-apigee-inventory-v1, inventory-v1 | Public inventory API (`/ata/inventory/v1`) |
| ata-orders-api | Cloud Run | orders-api | Places orders and reserves stock |
| ata-inventory-api | Cloud Run | inventory-api | Stock levels and reservations |
| ata-inventory-db | Cloud SQL | inventory-db | PostgreSQL inventory database |

Types: `Apigee proxy`, `Cloud Run`, `Cloud SQL`.

## Dependencies

Read each row as "**From** depends on **To**". If **To** fails, **From** is affected.

| From | To | How |
|---|---|---|
| ata-orders-v1 | ata-orders-api | routes traffic to |
| ata-inventory-v1 | ata-inventory-api | routes traffic to |
| ata-orders-api | ata-inventory-api | calls |
| ata-inventory-api | ata-inventory-db | reads and writes |

## What the agent derives from this

Shown here for reference only. The agent computes this; you don't maintain it.

```
ata-orders-v1 ──► ata-orders-api ──┐
                                   ├──► ata-inventory-api ──► ata-inventory-db
ata-inventory-v1 ─────────────────┘
```

- If **ata-inventory-db** fails, the blast radius is ata-inventory-api, ata-orders-api, ata-orders-v1 and ata-inventory-v1.
- If **ata-orders-v1** returns 429s while ata-orders-api is healthy, the origin stays **ata-orders-v1**.
