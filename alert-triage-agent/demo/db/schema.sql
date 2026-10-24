-- Schema initialization for ATA Inventory Service

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
