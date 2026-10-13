import io
import json
import logging
from datetime import datetime, timezone

import pytest
from inventory_app import JsonLogFormatter as InventoryJsonLogFormatter
from inventory_app import logger as inventory_logger
from shop_app import JsonLogFormatter as ShopJsonLogFormatter
from shop_app import logger as shop_logger


@pytest.mark.parametrize(
    "formatter_cls,service_name",
    [
        (ShopJsonLogFormatter, "shop-api"),
        (InventoryJsonLogFormatter, "inventory-svc"),
    ],
)
def test_json_log_formatter_fields(
    formatter_cls: type[ShopJsonLogFormatter | InventoryJsonLogFormatter],
    service_name: str,
) -> None:
    formatter = formatter_cls(service=service_name)
    record = logging.LogRecord(
        name=service_name,
        level=logging.INFO,
        pathname="app.py",
        lineno=42,
        msg="Order processed successfully: order_id=%s",
        args=("order-123",),
        exc_info=None,
    )

    formatted = formatter.format(record)
    data = json.loads(formatted)

    assert data["severity"] == "INFO"
    assert data["message"] == "Order processed successfully: order_id=order-123"
    assert data["serviceContext"] == {"service": service_name}

    # Validate ISO 8601 UTC timestamp parsing
    ts = datetime.fromisoformat(data["timestamp"])
    assert ts.tzinfo is not None
    assert ts.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.parametrize(
    "formatter_cls,service_name",
    [
        (ShopJsonLogFormatter, "shop-api"),
        (InventoryJsonLogFormatter, "inventory-svc"),
    ],
)
def test_json_log_formatter_severities(
    formatter_cls: type[ShopJsonLogFormatter | InventoryJsonLogFormatter],
    service_name: str,
) -> None:
    formatter = formatter_cls(service=service_name)

    levels = [
        (logging.DEBUG, "DEBUG"),
        (logging.INFO, "INFO"),
        (logging.WARNING, "WARNING"),
        (logging.ERROR, "ERROR"),
        (logging.CRITICAL, "CRITICAL"),
    ]

    for level_num, level_name in levels:
        record = logging.LogRecord(
            name=service_name,
            level=level_num,
            pathname="app.py",
            lineno=10,
            msg=f"Test message for {level_name}",
            args=(),
            exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert data["severity"] == level_name
        assert data["message"] == f"Test message for {level_name}"
        assert data["serviceContext"]["service"] == service_name


def test_json_log_formatter_exception_trace() -> None:
    formatter = ShopJsonLogFormatter(service="shop-api")
    try:
        raise ValueError("Simulated database timeout")
    except ValueError:
        import sys

        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="shop-api",
        level=logging.ERROR,
        pathname="app.py",
        lineno=99,
        msg="Database operation failed",
        args=(),
        exc_info=exc_info,
    )

    data = json.loads(formatter.format(record))
    assert data["severity"] == "ERROR"
    assert "Database operation failed" in data["message"]
    assert "ValueError: Simulated database timeout" in data["message"]
    assert "Traceback (most recent call last):" in data["message"]


def test_logger_stream_emission() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ShopJsonLogFormatter(service="shop-api"))

    test_logger = logging.getLogger("shop-api-stream-test")
    test_logger.setLevel(logging.INFO)
    test_logger.addHandler(handler)
    test_logger.propagate = False

    test_logger.warning("Downstream latency elevated: %d ms", 450)
    output = stream.getvalue().strip()
    data = json.loads(output)

    assert data["severity"] == "WARNING"
    assert data["message"] == "Downstream latency elevated: 450 ms"
    assert data["serviceContext"] == {"service": "shop-api"}
    assert "timestamp" in data


def test_app_loggers_configured_with_json_formatter() -> None:
    # shop_app logger verification
    shop_formatters = [
        h.formatter for h in shop_logger.handlers if isinstance(h.formatter, ShopJsonLogFormatter)
    ]
    assert len(shop_formatters) >= 1
    assert shop_formatters[0].service == "shop-api"

    # inventory_app logger verification
    inv_formatters = [
        h.formatter
        for h in inventory_logger.handlers
        if isinstance(h.formatter, InventoryJsonLogFormatter)
    ]
    assert len(inv_formatters) >= 1
    assert inv_formatters[0].service == "inventory-svc"
