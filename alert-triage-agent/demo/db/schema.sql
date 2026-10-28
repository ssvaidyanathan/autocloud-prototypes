-- Schema initialization for ATA Inventory Service
--
-- Run as the postgres user, with the psql variable iam_user set to the stack's
-- Cloud SQL IAM database user, "<NAME_PREFIX>-inventory-sa@<PROJECT_ID>.iam"
-- (created by the database Terraform root, output iam_user). For example:
--
--   psql "host=127.0.0.1 port=5432 dbname=inventory user=postgres" \
--     -v iam_user="${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam" \
--     -f demo/db/schema.sql
--
-- (through the Cloud SQL Auth Proxy), or inside `gcloud sql connect
-- "${NAME_PREFIX}-inventory-db" --user=postgres --database=inventory`:
--
--   \set iam_user 'ata-inventory-sa@my-project.iam'
--   \i demo/db/schema.sql
--
-- If iam_user is not set, the GRANT below is a syntax error and the script stops
-- (ON_ERROR_STOP), so the grant cannot be skipped silently.
\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS inventory_items (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    stock INTEGER NOT NULL CHECK (stock >= 0),
    price_cents INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Seed initial items
INSERT INTO inventory_items (id, name, stock, price_cents)
VALUES 
    ('item-101', 'Cloud TPU v5e Accelerator Pod', 50, 120000),
    ('item-102', 'High-Memory Compute Node (n2-highmem-64)', 120, 45000),
    ('item-103', 'NVMe Local SSD Storage Block 375GB', 300, 8000),
    ('item-104', 'Direct Peering Interconnect 100Gbps', 15, 250000),
    ('item-105', 'Titan Security Key FIDO2', 500, 3500)
ON CONFLICT (id) DO UPDATE 
SET stock = EXCLUDED.stock,
    price_cents = EXCLUDED.price_cents,
    updated_at = CURRENT_TIMESTAMP;

-- The inventory API (demo/ata-inventory-api/app/db.py) reads the catalog
-- (SELECT), reserves and restocks stock (UPDATE), and locks rows with
-- SELECT ... FOR UPDATE, which needs UPDATE. It never inserts or deletes rows.
GRANT SELECT, UPDATE ON inventory_items TO :"iam_user";
