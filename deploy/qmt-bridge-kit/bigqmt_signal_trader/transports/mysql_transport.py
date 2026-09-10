"""MySQL transport for the BigQMT RPC bridge.

A compatibility-oriented backend for environments where Redis/ZMQ are
unavailable but a relational DB is. Latency is dominated by polling cadence,
so this is NOT a low-latency path (expect tens of ms); use it when the
deployment constraints rule out the others.

Schema (auto-created on first connect)::

    CREATE TABLE bigqmt_rpc_requests (
        request_id   VARCHAR(64) PRIMARY KEY,
        account_id   VARCHAR(64) NOT NULL,
        payload      MEDIUMTEXT NOT NULL,
        created_at   DOUBLE NOT NULL,
        claimed_at   DOUBLE NULL,
        INDEX idx_account_created (account_id, created_at)
    );
    CREATE TABLE bigqmt_rpc_responses (
        request_id   VARCHAR(64) PRIMARY KEY,
        payload      MEDIUMTEXT NOT NULL,
        created_at   DOUBLE NOT NULL
    );

The client ``INSERT``s a request row and polls ``bigqmt_rpc_responses`` by
``request_id``; the server ``SELECT ... FOR UPDATE SKIP LOCKED`` (or a
``claimed_at`` flag on older engines) claims a request, invokes the handler,
then ``INSERT``s the response row. Rows are cleaned up lazily by TTL.

Any DB-API 2.0 driver works (pymysql, mysql-connector, sqlite3 for tests).
Pass ``driver="pymysql"`` / ``"sqlite3"`` etc. via config.
"""

import json
import threading
import time
import uuid

from ..adapters.redis_common import decode_text
from ..redis_rpc import encode_rpc_request_payload, decode_rpc_request_payload
from .base import RpcTransport, TransportError, TransportTimeout


REQUESTS_TABLE = "bigqmt_rpc_requests"
RESPONSES_TABLE = "bigqmt_rpc_responses"


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS {requests} (
        request_id   VARCHAR(64) PRIMARY KEY,
        account_id   VARCHAR(64) NOT NULL,
        payload      MEDIUMTEXT NOT NULL,
        created_at   DOUBLE NOT NULL,
        claimed_at   DOUBLE NULL
    )""",
    """CREATE TABLE IF NOT EXISTS {responses} (
        request_id   VARCHAR(64) PRIMARY KEY,
        payload      MEDIUMTEXT NOT NULL,
        created_at   DOUBLE NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_{requests}_account_created ON {requests} (account_id, created_at)",
]


def _loads(raw):
    if isinstance(raw, dict):
        return dict(raw)
    text = decode_text(raw)
    text = decode_rpc_request_payload(text)
    return json.loads(text)


class MysqlTransport(RpcTransport):
    """Polling-based transport over a relational DB.

    Both client and server open a short-lived connection per operation to keep
    the implementation driver-agnostic and avoid cross-thread cursor state.
    For high throughput a connection pool would help; this backend targets
    compatibility, not throughput.
    """

    name = "mysql"

    def __init__(
        self,
        driver="pymysql",
        connect_kwargs=None,
        requests_table=REQUESTS_TABLE,
        responses_table=RESPONSES_TABLE,
        account_id="",
        print_prefix="[bigqmt_rpc]",
        poll_interval_seconds=0.02,
        row_ttl_seconds=120,
        background_threads=True,
        pool_config=None,
        use_pool=True,
    ):
        super(MysqlTransport, self).__init__(account_id=account_id, print_prefix=print_prefix)
        self.driver = driver
        self.connect_kwargs = dict(connect_kwargs or {})
        self.requests_table = requests_table
        self.responses_table = responses_table
        self.poll_interval_seconds = max(0.001, float(poll_interval_seconds))
        self.row_ttl_seconds = int(row_ttl_seconds)
        self.background_threads = bool(background_threads)
        self._thread = None
        self._schema_ready = False
        self.use_pool = bool(use_pool)
        self.pool_config = dict(pool_config or {})
        self._pool = None
        # paramstyle: mysql drivers use "format" (%s), sqlite3 uses "qmark" (?).
        # Resolved lazily on first connect.
        self._placeholder = None

    def _resolve_placeholder(self, mod):
        style = getattr(mod, "paramstyle", "format")
        if style == "qmark":
            return "?"
        return "%s"  # format / pyformat / default

    def _ph(self):
        # Return the placeholder char (resolving lazily).
        if self._placeholder is None:
            try:
                mod = __import__(self.driver)
                self._placeholder = self._resolve_placeholder(mod)
            except ImportError:
                self._placeholder = "%s"
        return self._placeholder

    def _sql(self, template):
        """Render a SQL template: fill {t}/{requests}/{responses} table names
        and swap the standard ``%s`` placeholder for the driver's paramstyle."""
        return template.format(
            t=None,  # not used; callers format table names themselves
            requests=self.requests_table,
            responses=self.responses_table,
        ).replace("__PH__", self._ph())

    @classmethod
    def from_config(cls, config, account_id="", print_prefix="[bigqmt_rpc]"):
        config = dict(config or {})
        driver = config.get("driver", "pymysql")
        connect_kwargs = dict(config.get("connect_kwargs") or {})
        # Allow flat keys (host/port/user/...) as a convenience.
        for key in ("host", "port", "user", "password", "database", "charset"):
            if key in config and key not in connect_kwargs:
                connect_kwargs[key] = config[key]
        return cls(
            driver=driver,
            connect_kwargs=connect_kwargs,
            requests_table=config.get("requests_table", REQUESTS_TABLE),
            responses_table=config.get("responses_table", RESPONSES_TABLE),
            account_id=config.get("account_id", account_id),
            print_prefix=print_prefix,
            poll_interval_seconds=float(config.get("poll_interval_seconds", 0.02)),
            row_ttl_seconds=int(config.get("row_ttl_seconds", 120)),
            background_threads=bool(config.get("background_threads", True)),
            pool_config=config.get("pool_config"),
            use_pool=bool(config.get("use_pool", True)),
        )

    # -- driver access / connection pool ----------------------------------
    def _import_driver(self):
        try:
            return __import__(self.driver)
        except ImportError as exc:  # pragma: no cover - depends on env
            raise TransportError(
                "db driver %r is required for the mysql transport: %s"
                % (self.driver, exc)
            )

    def _build_pool(self):
        """Create a DBUtils PooledDB backed by the configured driver.

        Works with any DB-API 2.0 driver (pymysql, mysql.connector, sqlite3,
        ...). Pool sizing comes from ``pool_config``; connection kwargs are
        forwarded to the driver's ``connect()``.
        """
        try:
            from dbutils.pooled_db import PooledDB
        except ImportError as exc:
            raise TransportError(
                "DBUtils is required for the mysql transport connection pool: %s" % exc
            )
        driver = self._import_driver()
        cfg = dict(self.pool_config)
        # Sensible defaults for an RPC workload: small idle pool, modest cap,
        # reuse connections across threads. Callers override via pool_config.
        mincached = cfg.pop("mincached", 1)
        maxcached = cfg.pop("maxcached", 4)
        maxshared = cfg.pop("maxshared", 3)
        maxconnections = cfg.pop("maxconnections", 8)
        blocking = cfg.pop("blocking", True)
        maxusage = cfg.pop("maxusage", 0)
        reset = cfg.pop("reset", True)
        # Whatever remains in cfg is treated as extra creator kwargs (e.g.
        # ping, setsession) and merged under the connect kwargs.
        extra = cfg
        connect_kwargs = self._pooled_connect_args()
        connect_kwargs.update(extra)
        return PooledDB(
            creator=driver,
            mincached=mincached,
            maxcached=maxcached,
            maxshared=maxshared,
            maxconnections=maxconnections,
            blocking=blocking,
            maxusage=maxusage,
            reset=reset,
            **connect_kwargs
        )

    def _pooled_connect_args(self):
        """Return the kwargs to forward to the driver's connect().

        Stripped of empty credential fields so drivers that reject empty
        username/password (e.g. pymysql with auth plugin) don't choke.
        """
        cfg = dict(self.connect_kwargs)
        if not cfg.get("user") and "user" in cfg:
            cfg.pop("user")
        if not cfg.get("password") and "password" in cfg:
            cfg.pop("password")
        return cfg

    def _connect(self):
        if not self.use_pool:
            return self._import_driver().connect(**self.connect_kwargs)
        if self._pool is None:
            self._pool = self._build_pool()
        # PooledDB.connection() hands out a pooled connection; calling .close()
        # on it returns it to the pool rather than closing the underlying socket.
        return self._pool.connection()

    def _ensure_schema(self):
        if self._schema_ready:
            return
        ctx = {"requests": self.requests_table, "responses": self.responses_table}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for stmt in _SCHEMA:
                try:
                    cur.execute(stmt.format(**ctx))
                except Exception:
                    # "CREATE INDEX IF NOT EXISTS" is not supported on some
                    # MySQL versions; the index is an optimization, ignore failure.
                    pass
            conn.commit()
            self._schema_ready = True
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _now(self):
        return time.time()

    # -- client side ------------------------------------------------------
    def send_request(self, request, timeout_seconds):
        self._ensure_schema()
        request = dict(request)
        request.setdefault("request_id", uuid.uuid4().hex)
        request_id = str(request["request_id"])
        request.setdefault("account_id", self.account_id)
        payload = encode_rpc_request_payload(request)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                self._sql(
                    "INSERT INTO {requests} (request_id, account_id, payload, created_at, claimed_at) "
                    "VALUES (__PH__, __PH__, __PH__, __PH__, NULL)"
                ),
                (request_id, str(request.get("account_id") or self.account_id), payload, self._now()),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass

        deadline = time.time() + float(timeout_seconds)
        while time.time() < deadline:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute(
                    self._sql("SELECT payload FROM {responses} WHERE request_id = __PH__"),
                    (request_id,),
                )
                row = cur.fetchone()
                if row:
                    payload = row[0]
                    try:
                        cur.execute(
                            self._sql("DELETE FROM {responses} WHERE request_id = __PH__"),
                            (request_id,),
                        )
                        conn.commit()
                    except Exception:
                        pass
                    return _loads(payload)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(self.poll_interval_seconds)
        raise TransportTimeout("mysql rpc timeout: %s" % request.get("method"))

    # -- server side ------------------------------------------------------
    def start_receiving(self, on_request, background_threads=None):
        super(MysqlTransport, self).start_receiving(on_request)
        self._ensure_schema()
        if background_threads is None:
            background_threads = self.background_threads
        if not background_threads:
            print("%s mysql polling table=%s background_threads=False" % (
                self.print_prefix, self.requests_table))
            return
        self._thread = threading.Thread(
            target=self._poll_loop, name="bigqmt-mysql-rpc", daemon=True
        )
        self._thread.start()
        print("%s mysql started polling table=%s" % (
            self.print_prefix, self.requests_table))

    def _poll_loop(self):
        while self._running:
            try:
                self._claim_and_handle_batch()
            except Exception as exc:
                if not self._running:
                    break
                print("%s mysql poll failed: %s" % (self.print_prefix, exc))
                time.sleep(0.5)
                continue
            time.sleep(self.poll_interval_seconds)

    def _claim_and_handle_batch(self, max_items=20):
        conn = self._connect()
        claimed = []
        try:
            cur = conn.cursor()
            # Claim rows: mark claimed_at so concurrent servers skip them.
            # Uses an atomic UPDATE ... WHERE claimed_at IS NULL with a cap.
            cur.execute(
                self._sql(
                    "SELECT request_id, payload FROM {requests} WHERE account_id = __PH__ "
                    "AND claimed_at IS NULL ORDER BY created_at ASC LIMIT __PH__"
                ),
                (self.account_id, int(max_items)),
            )
            rows = cur.fetchall()
            now = self._now()
            for request_id, payload in rows:
                cur.execute(
                    self._sql(
                        "UPDATE {requests} SET claimed_at = __PH__ WHERE request_id = __PH__ "
                        "AND claimed_at IS NULL"
                    ),
                    (now, request_id),
                )
                if cur.rowcount > 0:
                    claimed.append((request_id, payload))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        for request_id, payload in claimed:
            try:
                request = _loads(payload)
                request["request_id"] = request_id
            except Exception as exc:
                print("%s mysql decode failed: %s" % (self.print_prefix, exc))
                self._delete_request(request_id)
                continue
            try:
                self.deliver(request)
            except Exception as exc:
                print("%s mysql deliver failed: %s" % (self.print_prefix, exc))
            self._delete_request(request_id)

    def _delete_request(self, request_id):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                self._sql("DELETE FROM {requests} WHERE request_id = __PH__"),
                (request_id,),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def send_response(self, request, response):
        request_id = str(
            response.get("request_id") or request.get("request_id") or ""
        )
        payload = encode_rpc_request_payload(response)
        # DELETE-then-INSERT is portable across MySQL and sqlite (avoids the
        # MySQL-only ON DUPLICATE KEY / REPLACE syntax). One connection, one txn.
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                self._sql("DELETE FROM {responses} WHERE request_id = __PH__"),
                (request_id,),
            )
            cur.execute(
                self._sql(
                    "INSERT INTO {responses} (request_id, payload, created_at) "
                    "VALUES (__PH__, __PH__, __PH__)"
                ),
                (request_id, payload, self._now()),
            )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print("%s mysql response write failed: %s" % (self.print_prefix, exc))
        finally:
            try:
                conn.close()
            except Exception:
                pass
                conn.close()
            except Exception:
                pass

    # -- non-background drain (strategy adjust thread) --------------------
    def drain_request_queue(self, max_items=20):
        if not self._running:
            return 0
        before = 0  # _claim_and_handle_batch handles its own count
        self._claim_and_handle_batch(max_items=max_items)
        return 0

    def stop(self):
        super(MysqlTransport, self).stop()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(1.0)
        self._thread = None
        # Close the connection pool so background connections are released.
        if self._pool is not None:
            try:
                self._pool.close()
            except Exception:
                pass
            self._pool = None
