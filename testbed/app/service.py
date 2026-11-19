"""Testbed application services: gateway, orders, inventory, payment (design §12).

One codebase and one image (safeops-testbed). SERVICE_NAME selects the role, and the
version-controlled pod template and ConfigMaps set its behavior (decision 0007).
"""

from __future__ import annotations

import os
import threading
import time

from app.common import App, Response, call, is_timeout, log, warm_cache

SERVICE = os.environ["SERVICE_NAME"]
_cache = warm_cache()
app = App()


def _ms(name: str, default: int) -> float:
    return int(os.environ.get(name, str(default))) / 1000.0


if SERVICE == "gateway":
    ORDERS_URL = os.environ["ORDERS_URL"]
    ORDERS_TIMEOUT_S = _ms("ORDERS_TIMEOUT_MS", 800)

    @app.route("GET", "/checkout")
    def checkout(body: dict, query: dict) -> Response:
        try:
            status, data = call(
                f"{ORDERS_URL}/orders", "POST", {"item": "widget", "qty": 1}, ORDERS_TIMEOUT_S
            )
        except Exception as exc:
            timed_out = is_timeout(exc)
            log("warn", "orders unreachable", error=type(exc).__name__, timeout=timed_out)
            return Response(504 if timed_out else 502, {"error": "orders unreachable"})
        if status != 200:
            log("warn", "orders failed", upstream_status=status)
            return Response(502, {"error": "orders failed", "upstream_status": status})
        return Response(200, {"order_id": data.get("order_id")})

    app.ready.set()

elif SERVICE == "orders":
    from prometheus_client import Counter, Gauge
    from psycopg_pool import ConnectionPool, PoolTimeout

    INVENTORY_URL = os.environ["INVENTORY_URL"]
    PAYMENT_URL = os.environ["PAYMENT_URL"]
    UPSTREAM_TIMEOUT_S = _ms("UPSTREAM_TIMEOUT_MS", 300)
    # From db-pool-config (ConfigMap). Read once at start, so a patch takes effect on the
    # composite's rollout-restart. max_pool_size is a real ConnectionPool(max_size=...).
    MAX_POOL_SIZE = int(os.environ["DB_MAX_POOL_SIZE"])
    CONN_TIMEOUT_S = _ms("DB_CONNECTION_TIMEOUT_MS", 2000)

    POOL_MAX = Gauge("db_pool_max_size", "Configured maximum pool size", ["service"])
    POOL_SIZE = Gauge("db_pool_size", "Open connections in the pool", ["service"])
    POOL_AVAILABLE = Gauge("db_pool_available", "Idle connections in the pool", ["service"])
    POOL_WAITING = Gauge(
        "db_pool_requests_waiting", "Requests waiting for a connection", ["service"]
    )
    POOL_TIMEOUTS = Counter(
        "db_pool_timeouts_total", "Checkouts that hit the pool timeout", ["service"]
    )
    POOL_MAX.labels(SERVICE).set(MAX_POOL_SIZE)

    conninfo = (
        f"host={os.environ['DB_HOST']} dbname={os.environ['DB_NAME']} "
        f"user={os.environ['DB_USER']} password={os.environ['DB_PASSWORD']} connect_timeout=5"
    )
    pool = ConnectionPool(
        conninfo,
        min_size=1,
        max_size=MAX_POOL_SIZE,
        timeout=CONN_TIMEOUT_S,
        open=False,
        name="orders",
    )
    pool.open(wait=False)

    def _init_schema() -> None:
        while True:
            try:
                with pool.connection(timeout=5) as conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS orders ("
                        " id bigserial PRIMARY KEY, item text NOT NULL, qty int NOT NULL,"
                        " created_at timestamptz NOT NULL DEFAULT now())"
                    )
                app.ready.set()
                log(
                    "info",
                    "schema ready",
                    max_pool_size=MAX_POOL_SIZE,
                    connection_timeout_s=CONN_TIMEOUT_S,
                )
                return
            except Exception as exc:
                log("warn", "database not ready", error=type(exc).__name__)
                time.sleep(2)

    def _pool_stats() -> None:
        while True:
            stats = pool.get_stats()
            POOL_SIZE.labels(SERVICE).set(stats.get("pool_size", 0))
            POOL_AVAILABLE.labels(SERVICE).set(stats.get("pool_available", 0))
            POOL_WAITING.labels(SERVICE).set(stats.get("requests_waiting", 0))
            time.sleep(2)

    threading.Thread(target=_init_schema, daemon=True).start()
    threading.Thread(target=_pool_stats, daemon=True).start()

    @app.route("POST", "/orders")
    def create_order(body: dict, query: dict) -> Response:
        for name, url in (
            ("inventory", f"{INVENTORY_URL}/reserve"),
            ("payment", f"{PAYMENT_URL}/charge"),
        ):
            try:
                status, _ = call(url, "POST", body, UPSTREAM_TIMEOUT_S)
            except Exception as exc:
                log(
                    "warn",
                    "upstream unreachable",
                    upstream=name,
                    error=type(exc).__name__,
                    timeout=is_timeout(exc),
                )
                return Response(502, {"error": f"{name} unreachable"})
            if status != 200:
                log("warn", "upstream failed", upstream=name, upstream_status=status)
                return Response(502, {"error": f"{name} failed", "upstream_status": status})
        try:
            with pool.connection(timeout=CONN_TIMEOUT_S) as conn:
                row = conn.execute(
                    "INSERT INTO orders (item, qty) VALUES (%s, %s) RETURNING id",
                    (body.get("item", "widget"), int(body.get("qty", 1))),
                ).fetchone()
        except PoolTimeout:
            POOL_TIMEOUTS.labels(SERVICE).inc()
            log("error", "database connection pool exhausted", max_pool_size=MAX_POOL_SIZE)
            return Response(503, {"error": "database connection pool exhausted"})
        except Exception as exc:
            log("error", "database error", error=type(exc).__name__)
            return Response(500, {"error": "database error"})
        return Response(200, {"order_id": row[0]})

elif SERVICE in ("inventory", "payment"):
    WORK_S = _ms("WORK_MS", 5)
    route = "/reserve" if SERVICE == "inventory" else "/charge"

    @app.route("POST", route)
    def handle(body: dict, query: dict) -> Response:
        time.sleep(WORK_S)
        return Response(200, {"ok": True})

    app.ready.set()

else:
    raise SystemExit(f"unknown SERVICE_NAME {SERVICE!r}")


if __name__ == "__main__":
    app.serve()
