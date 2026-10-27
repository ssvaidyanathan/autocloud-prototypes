"""Database connection and fault injection management for ata-inventory-api."""

import time
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy import create_engine
from sqlalchemy.pool import QueuePool
from .config import settings

logger = logging.getLogger(__name__)

# In-memory mock items for fallback and unit tests
INITIAL_ITEMS = [
    {"id": "item-101", "name": "Cloud TPU v5e Accelerator Pod", "stock": 50, "price_cents": 120000},
    {"id": "item-102", "name": "High-Memory Compute Node (n2-highmem-64)", "stock": 120, "price_cents": 45000},
    {"id": "item-103", "name": "NVMe Local SSD Storage Block 375GB", "stock": 300, "price_cents": 8000},
    {"id": "item-104", "name": "Direct Peering Interconnect 100Gbps", "stock": 15, "price_cents": 250000},
    {"id": "item-105", "name": "Titan Security Key FIDO2", "stock": 500, "price_cents": 3500},
]

MOCK_STORE: Dict[str, Dict[str, Any]] = {item["id"]: dict(item) for item in INITIAL_ITEMS}
LEAKED_CONNECTIONS: List[Any] = []

# Fault injection state
FAULT_STATE = {
    "error_5xx": False,
    "error_rate": 0.0,
    "latency_delay_ms": 0,
}

_connector = None


def get_connector():
    global _connector
    if _connector is None:
        try:
            from google.cloud.sql.connector import Connector
            _connector = Connector()
        except Exception as e:
            logger.warning(f"Could not initialize Cloud SQL Connector: {e}")
            _connector = None
    return _connector


def get_db_connection(user_override: Optional[str] = None):
    """Gets a connection to Cloud SQL PostgreSQL using IAM DB auth."""
    if settings.effective_mock_db:
        return None

    # Fails loudly (no built-in project values) instead of connecting as a guessed user.
    settings.require_db_settings()

    connector = get_connector()
    if not connector:
        return None

    db_user = user_override or settings.effective_db_user
    try:
        from google.cloud.sql.connector import IPTypes
        # Connect using IAM DB Auth
        conn = connector.connect(
            settings.instance_connection_name,
            "pg8000",
            user=db_user,
            db=settings.db_name,
            enable_iam_auth=settings.db_iam_auth,
            ip_type=IPTypes.PUBLIC,
        )
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to Cloud SQL: {e}")
        return None


# SQLAlchemy Engine with Connection Pooling
engine = create_engine(
    "postgresql+pg8000://",
    creator=get_db_connection,
    poolclass=QueuePool,
    pool_size=5,
    max_overflow=2,
    pool_timeout=30,
    pool_recycle=1800,
)


def list_items() -> List[Dict[str, Any]]:
    """List inventory items from DB or mock store."""
    check_and_apply_faults()

    if not settings.effective_mock_db:
        try:
            with engine.connect() as conn:
                result = conn.exec_driver_sql("SELECT id, name, stock, price_cents FROM inventory_items ORDER BY id;")
                rows = result.fetchall()
                return [
                    {"id": r[0], "name": r[1], "stock": r[2], "price_cents": r[3]}
                    for r in rows
                ]
        except Exception as e:
            logger.error(f"Error querying inventory_items: {e}")
            raise RuntimeError(f"Database query error: {e}") from e

    # Fallback to in-memory store
    return list(MOCK_STORE.values())


def reserve_item_stock(item_id: str, quantity: int) -> Dict[str, Any]:
    """Reserve item stock in DB or mock store."""
    check_and_apply_faults()

    if not settings.effective_mock_db:
        try:
            with engine.connect() as conn:
                with conn.begin():
                    # Transactional update
                    result = conn.exec_driver_sql(
                        "SELECT stock FROM inventory_items WHERE id = %s FOR UPDATE;",
                        (item_id,)
                    )
                    row = result.fetchone()
                    if not row:
                        return {"success": False, "error": "Item not found"}

                    current_stock = row[0]
                    if current_stock < quantity:
                        return {"success": False, "error": "Insufficient stock", "current_stock": current_stock}

                    new_stock = current_stock - quantity
                    conn.exec_driver_sql(
                        "UPDATE inventory_items SET stock = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (new_stock, item_id)
                    )
                    return {"success": True, "item_id": item_id, "remaining_stock": new_stock}
        except Exception as e:
            logger.error(f"DB error reserving stock: {e}")
            raise RuntimeError(f"Stock reservation internal error: {e}") from e

    # Fallback to mock store
    if item_id not in MOCK_STORE:
        return {"success": False, "error": "Item not found"}

    item = MOCK_STORE[item_id]
    if item["stock"] < quantity:
        return {"success": False, "error": "Insufficient stock", "current_stock": item["stock"]}

    item["stock"] -= quantity
    return {"success": True, "item_id": item_id, "remaining_stock": item["stock"]}


class UnknownItemsError(ValueError):
    """restock_items was asked for item ids that do not exist."""

    def __init__(self, missing: List[str]):
        super().__init__(f"Unknown item ids: {', '.join(missing)}")
        self.missing = missing


def restock_items(stock_by_id: Dict[str, int]) -> List[Dict[str, Any]]:
    """Set the stock of each given item to the given absolute value (idempotent).

    All or nothing: if any id does not exist, nothing is changed and UnknownItemsError
    names the missing ids (items are never created: name and price are not known here).
    Returns [{"id", "stock"}] for the given ids, as read back after the update.
    Faults are not applied: this is an admin action for the demo reset.
    """
    ids = sorted(stock_by_id)
    if not settings.effective_mock_db:
        try:
            with engine.connect() as conn:
                with conn.begin():
                    missing = []
                    for item_id in ids:
                        result = conn.exec_driver_sql(
                            "UPDATE inventory_items SET stock = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                            (stock_by_id[item_id], item_id),
                        )
                        if result.rowcount != 1:
                            missing.append(item_id)
                    if missing:
                        # Raising inside conn.begin() rolls the whole restock back.
                        raise UnknownItemsError(missing)
                    rows = conn.exec_driver_sql(
                        "SELECT id, stock FROM inventory_items ORDER BY id;"
                    ).fetchall()
        except UnknownItemsError:
            raise
        except Exception as e:
            logger.error(f"DB error restocking items: {e}")
            raise RuntimeError(f"Restock internal error: {e}") from e
        wanted = set(ids)
        return [{"id": r[0], "stock": r[1]} for r in rows if r[0] in wanted]

    missing = [item_id for item_id in ids if item_id not in MOCK_STORE]
    if missing:
        raise UnknownItemsError(missing)
    for item_id in ids:
        MOCK_STORE[item_id]["stock"] = stock_by_id[item_id]
    return [{"id": item_id, "stock": MOCK_STORE[item_id]["stock"]} for item_id in ids]


def leak_connections(count: int = 10) -> Dict[str, Any]:
    """Leaking database connections without closing them (Scenario S6)."""
    leaked_this_call = 0
    errors = []
    for i in range(count):
        if not settings.effective_mock_db:
            try:
                # Open direct connection to Cloud SQL so we can reach target saturation
                # (>20 backends) without being limited by SQLAlchemy client QueuePool (max 7).
                conn = get_db_connection()
                if conn is not None:
                    try:
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1;")
                    except Exception as q_err:
                        logger.warning(f"Error executing keepalive on leaked conn: {q_err}")
                    LEAKED_CONNECTIONS.append(conn)
                    leaked_this_call += 1
                else:
                    errors.append("get_db_connection returned None")
            except Exception as e:
                logger.error(f"Error leaking connection from pool: {e}")
                errors.append(str(e))
        else:
            # Mock connection leak simulation
            dummy_conn = {"id": f"mock-conn-{len(LEAKED_CONNECTIONS)}", "active": True}
            LEAKED_CONNECTIONS.append(dummy_conn)
            leaked_this_call += 1

    return {
        "status": "leaked",
        "leaked_this_call": leaked_this_call,
        "total_leaked": len(LEAKED_CONNECTIONS),
        "errors": errors if errors else None,
    }


def hold_table_lock(duration_seconds: int = 180) -> Dict[str, Any]:
    """Acquires table/row lock for scenario S7 (BEGIN; SELECT * FROM inventory_items FOR UPDATE;)."""
    if not settings.effective_mock_db:
        try:
            conn = get_db_connection()
            if conn is not None:
                cursor = conn.cursor()
                cursor.execute("BEGIN;")
                cursor.execute("SELECT * FROM inventory_items FOR UPDATE;")
                LEAKED_CONNECTIONS.append(conn)
                return {"status": "lock_acquired", "duration_seconds": duration_seconds, "mode": "cloudsql"}
            else:
                logger.error("Could not obtain connection for table lock")
                return {"status": "error", "error": "Could not connect to database"}
        except Exception as e:
            logger.error(f"Error acquiring lock: {e}")
            return {"status": "error", "error": str(e)}

    # Mock mode fallback
    dummy_conn = {"id": f"mock-lock-conn-{len(LEAKED_CONNECTIONS)}", "locked": True}
    LEAKED_CONNECTIONS.append(dummy_conn)
    return {"status": "lock_acquired", "duration_seconds": duration_seconds, "mode": "mock"}


def reset_connections_and_faults() -> Dict[str, Any]:
    """Closes all leaked connections and clears active fault injections."""
    closed_count = 0
    for conn in LEAKED_CONNECTIONS:
        try:
            if hasattr(conn, "rollback"):
                try:
                    conn.rollback()
                except Exception:
                    pass
            if hasattr(conn, "close"):
                conn.close()
            closed_count += 1
        except Exception:
            pass
    LEAKED_CONNECTIONS.clear()

    # Reset connection pool
    try:
        engine.dispose()
    except Exception:
        pass


    # Reset fault state
    FAULT_STATE["error_5xx"] = False
    FAULT_STATE["error_rate"] = 0.0
    FAULT_STATE["latency_delay_ms"] = 0

    # Reset in-memory store
    global MOCK_STORE
    MOCK_STORE = {item["id"]: dict(item) for item in INITIAL_ITEMS}

    return {
        "status": "reset",
        "connections_closed": closed_count,
        "faults_cleared": True,
    }


def set_fault(fault_type: str, rate: float = 1.0, delay_ms: int = 0):
    if fault_type == "500" or fault_type == "5xx":
        FAULT_STATE["error_5xx"] = True
        FAULT_STATE["error_rate"] = rate
    elif fault_type == "latency":
        FAULT_STATE["latency_delay_ms"] = delay_ms


def check_and_apply_faults():
    """Applies active fault delays or raises exceptions if configured."""
    if FAULT_STATE["latency_delay_ms"] > 0:
        time.sleep(FAULT_STATE["latency_delay_ms"] / 1000.0)

    if FAULT_STATE["error_5xx"]:
        import random
        if random.random() <= FAULT_STATE["error_rate"]:
            raise RuntimeError("Database connection failure simulated: pg8000.exceptions.DatabaseError: sorry, too many clients already")
