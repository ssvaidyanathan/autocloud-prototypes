# Demo Traffic Load Generator

The `loadgen` tool generates continuous, realistic HTTP traffic against the `shop-api` service.

## Behavior
- **Traffic Mix**:
  - 50% `GET /catalog` (browsing products with real-time stock)
  - 30% `GET /catalog/{item_id}` (inspecting individual product items)
  - 20% `POST /checkout` (purchasing items and reserving inventory)
- **Telemetry**: Periodically logs request count, request throughput (req/s), response HTTP status codes, and latency percentiles (p50, p95, p99, avg).

## Usage

### Direct CLI
```bash
python loadgen.py --target http://localhost:8080 --concurrency 5 --rate 15.0
```

### Options
- `--target`: Target URL for `shop-api` (default: `http://localhost:8080` or `SHOP_API_URL` env var).
- `--concurrency`: Number of concurrent worker tasks (default: `5`).
- `--rate`: Overall request rate in requests/sec (default: `10.0`).
- `--duration`: Number of seconds to run (`0` for indefinite, default: `0`).
- `--interval`: Summary reporting interval in seconds (default: `5.0`).

### Docker
```bash
docker build -t shop-loadgen .
docker run --network host shop-loadgen --target http://localhost:8080
```
