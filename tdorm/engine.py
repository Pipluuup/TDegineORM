"""TDEngine 连接与数据访问入口。

驱动 ``taospy`` 采用惰性导入：未安装时只在真正连接时报错，
因此本包在没有驱动、没有服务器的情况下也可以被 import 并离线测试 SQL。
"""

from __future__ import annotations

from contextlib import contextmanager

from . import sql as _sql
from .exceptions import ConfigurationError, DriverError, ValidationError
from .pool import ConnectionPool
from .query import Query
from .rest import RestTransport
from .session import Session

__all__ = ["TDEngine"]

# 幂等操作集合：连接中断时的重试只针对这些（避免 INSERT 被重复执行）
_RETRYABLE_PREFIXES = (
    "SELECT", "SHOW", "DESCRIBE", "CREATE", "DROP", "TRUNCATE",
    "USE", "DELETE", "ALTER",
)


def _is_retryable(sql: str) -> bool:
    return sql.lstrip().upper().startswith(_RETRYABLE_PREFIXES)


class TDEngine:
    """TDengine 访问引擎。

    参数与 ``taos.connect`` 对齐，常用：
    ::

        engine = TDEngine(host="localhost", port=6030, user="root",
                          password="taosdata", database="demo")
        with engine:
            engine.create_database()
            engine.create(SensorData)
            engine.add(SensorData(ts=..., temperature=25.5, ...))
            rows = engine.query(SensorData).filter(temperature__gt=24).all()

    连接策略：
    - 默认每个操作一条短连接；
    - ``pool_size=N`` 时启用连接池，供多线程与 ``with engine.session()`` 复用；
    - ``bind=True`` 时 INSERT 走 taospy 原生参数绑定（仅 native 协议）。
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6030,
        user: str = "root",
        password: str = "taosdata",
        database: str = None,
        timezone: str = None,
        protocol: str = "native",  # "native" 或 "REST"
        auto_create: bool = False,
        pool_size: int = None,
        pool_timeout: float = 5.0,
        timeout: float = None,  # REST 请求超时（秒），默认 30
        **kwargs,
    ):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.timezone = timezone
        self.protocol = str(protocol).upper()
        if self.protocol not in ("NATIVE", "REST"):
            raise ConfigurationError("protocol 只能是 'native' 或 'REST'，得到 %r" % protocol)
        self._extra = kwargs
        self._auto_create = bool(auto_create)
        self._created = set()  # 已确认建过的 (database, tablename)，避免重复 DDL
        self._timeout = float(timeout) if timeout is not None else 30.0

        if self.protocol == "REST":
            # 内置 REST 传输（不依赖 taospy；无法使用参数绑定与连接池）
            self._rest = RestTransport(
                host=host, port=port, user=user, password=password,
                database=database, timeout=self._timeout,
                ts_style=kwargs.pop("ts_style", "ms"), **kwargs,
            )
            self._pool = None
        else:
            self._rest = None
            self._pool = (
                ConnectionPool(self._connect, int(pool_size), pool_timeout)
                if pool_size else None
            )

    # ------------------------------------------------------------ 连接
    def _connect(self):
        try:
            import taos
        except ImportError:  # pragma: no cover
            raise DriverError(
                "未安装 taospy 驱动，请执行: pip install taospy "
                "（或在获取驱动前无法连接 TDengine）"
            ) from None
        params = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "protocol": "REST" if self.protocol == "REST" else "native",
        }
        if self.database:
            params["database"] = self.database
        if self.timezone:
            params["timezone"] = self.timezone
        params.update(self._extra)
        try:
            return taos.connect(**params)
        except Exception as exc:  # pragma: no cover
            raise DriverError("连接 TDengine 失败: %s" % exc) from exc

    def connect(self):
        """兼容 SQLAlchemy 风格的显式连接方法。"""
        return self

    def close(self):
        """关闭连接池（native）或 REST 会话；短连接模式下无操作。"""
        if self._rest is not None:
            self._rest.close()
            return self
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ 基础执行
    def _run_conn(self, conn, sql: str, model=None, fetch: bool = False, params=None):
        cur = conn.cursor()
        try:
            db = getattr(model, "__database__", None) if model is not None else None
            if db and db != self.database:
                cur.execute("USE %s" % _sql.quote_ident(db))
            cur.execute(sql, params)
            if fetch:
                columns = [d[0] for d in (cur.description or [])]
                rows = cur.fetchall() or []
                return columns, rows
            try:
                return cur.affected_rows
            except Exception:  # pragma: no cover
                return None
        finally:
            if hasattr(cur, "close"):
                try:
                    cur.close()
                except Exception:  # pragma: no cover
                    pass

    def _run(self, sql: str, model=None, fetch: bool = False, retry: bool = True,
             params=None):
        """执行一条 SQL。REST 模式直接交内置传输层；native 模式走连接池。"""
        if self._rest is not None:
            return self._rest._run(sql, model=model, fetch=fetch, retry=retry, params=params)
        if self._pool is None:
            conn = self._connect()
            try:
                return self._run_conn(conn, sql, model, fetch, params)
            finally:
                try:
                    conn.close()
                except Exception:  # pragma: no cover
                    pass

        conn = self._pool.acquire()
        # 注意：不能在 try 内直接 return（return 会跳过 else 子句，
        # 导致连接永不归还、每次 acquire 都新建连接）
        try:
            result = self._run_conn(conn, sql, model, fetch, params)
        except Exception:
            self._pool.discard(conn)
            if not (retry and _is_retryable(sql)):
                raise
            conn = self._pool.acquire()
            try:
                result = self._run_conn(conn, sql, model, fetch, params)
            except Exception:
                self._pool.discard(conn)
                raise
        self._pool.release(conn)
        return result

    def execute(self, sql: str):
        """执行任意 SQL（DDL / DML），返回受影响行数或 None。"""
        return self._run(sql)

    def query(self, target):
        """字符串 -> 裸查询返回 ``(列名, 行)``；模型类 -> 链式查询构建器。"""
        if isinstance(target, str):
            return self._run(target, fetch=True)
        return Query(self, target)

    def _query(self, sql):
        """Query 构建器内部使用的查询入口。"""
        return self._run(sql, fetch=True)

    def _execute(self, sql, model=None):
        """Query 构建器内部使用的写入口（DELETE 等）。"""
        return self._run(sql, model=model)

    # ------------------------------------------------------------ 会话
    @contextmanager
    def session(self, auto_commit: bool = True):
        """在一条连接上连续执行多个操作（native 有池则复用池连接）。

        REST 模式下无事务语义，会话仅共享 requests.Session 连接。
        ::

            with engine.session() as s:
                s.create(Model)
                s.add(Model(...), Model(...))
                rows = s.query(Model).filter(...).all()
        """
        if self._rest is not None:
            sess = RestSession(self)
            try:
                yield sess
            finally:
                sess.close()
            return
        conn = self._pool.acquire() if self._pool is not None else self._connect()
        sess = Session(self, conn)
        try:
            yield sess
            if auto_commit:
                sess.commit()
        except Exception:
            try:
                sess.rollback()
            except Exception:  # pragma: no cover
                pass
            raise
        finally:
            sess.close()

    # ------------------------------------------------------------ 数据库
    def use(self, database: str):
        if self._rest is not None:
            # REST 无会话状态：仅切换后续请求的默认库
            self._rest.database = database
            return
        self.execute("USE %s" % _sql.quote_ident(database))

    def create_database(self, database: str = None, **opts):
        """建库；``database`` 缺省用引擎默认库。选项如 keep/days/buffer/precision。"""
        db = database or self.database
        if not db:
            raise ConfigurationError("create_database 需要 database 参数或引擎默认库")
        return self.execute(_sql.create_database_sql(db, **opts))

    def drop_database(self, database: str = None):
        db = database or self.database
        if not db:
            raise ConfigurationError("drop_database 需要 database 参数或引擎默认库")
        return self.execute(_sql.drop_database_sql(db))

    def show_databases(self):
        _, rows = self.query("SHOW DATABASES")
        return [r[0] for r in rows]

    # ------------------------------------------------------------ 表结构
    def create(self, model, create_database: bool = False, if_not_exists: bool = True,
               _runner=None):
        """创建超级表（含标签）或普通表。``create_database=True`` 时先建库。"""
        runner = _runner or self
        if create_database:
            db = model.__database__ or self.database
            if not db:
                raise ConfigurationError("create_database=True 需要模型带 __database__ 或引擎默认库")
            runner.create_database(db)
        sql = (
            _sql.create_stable_sql(model, if_not_exists)
            if model.__tags__
            else _sql.create_table_sql(model, if_not_exists)
        )
        ret = runner._run(sql, model=model)
        if ret is not None:
            key = (model.__database__, model.__tablename__)
            self._created.discard(key)
        return ret

    def create_subtable(self, model, **tag_values):
        """显式创建子表（通常无需：add() 时会用 USING TAGS 自动建）。"""
        name = model.make_subtable_name(**tag_values)
        return self._run(_sql.create_subtable_sql(model, name, tag_values), model=model)

    def drop(self, model, _runner=None):
        runner = _runner or self
        return runner._run(_sql.drop_table_sql(model), model=model)

    def truncate(self, model, _runner=None):
        runner = _runner or self
        return runner._run(_sql.truncate_sql(model), model=model)

    def _ensure_created(self, model, runner=None):
        runner = runner or self
        key = (model.__database__, model.__tablename__)
        if key in self._created:
            return
        self.create(model, _runner=runner)
        self._created.add(key)

    # ------------------------------------------------------------ 写入
    def add(self, *instances, on_duplicate=None, batch_size: int = 1000,
            auto_create=None, bind: bool = False, retry: bool = False):
        """批量写入。实例自动按标签归类到各子表。

        - ``on_duplicate``: None | "update" | "ignore"（TDengine 3.x upsert）
        - ``batch_size``: 单条 INSERT 语句的行数上限（TDengine 上限 16383）
        - ``auto_create``: True 时先执行 CREATE STABLE/TABLE IF NOT EXISTS
        - ``bind``: True 时走 taospy 原生参数绑定（仅 native 协议，
          不能与 ``on_duplicate`` 同用），适合需要二进制/超长字符串的场景
        """
        return self._add(instances, runner=self, on_duplicate=on_duplicate,
                         batch_size=batch_size, auto_create=auto_create,
                         bind=bind, retry=retry)

    def _add(self, instances, runner=None, on_duplicate=None, batch_size: int = 1000,
             auto_create=None, bind: bool = False, retry: bool = False):
        runner = runner or self
        auto = self._auto_create if auto_create is None else bool(auto_create)
        if not instances:
            return 0
        if bind and on_duplicate is not None:
            raise ValidationError("bind=True 时不支持 on_duplicate（原生绑定无 upsert 语法）")
        if bind and self.protocol == "REST":
            raise ValidationError("bind=True 仅支持 native 协议连接")

        by_model = {}
        for inst in instances:
            by_model.setdefault(type(inst), []).append(inst)

        total = 0
        for model, insts in by_model.items():
            if auto:
                self._ensure_created(model, runner=runner)
            rows = []
            for inst in insts:
                coldict = {c: getattr(inst, c) for c in model.__columns__}
                if coldict.get(model.__primary_ts__) is None:
                    raise ValidationError(
                        "%s 缺少时间戳主键 %s 的值" % (model.__name__, model.__primary_ts__)
                    )
                tagvals = {t: getattr(inst, t) for t in model.__tags__}
                if tagvals:
                    name = model.make_subtable_name(**tagvals)
                else:
                    name = model.__tablename__
                rows.append((name, tagvals, coldict))

            if bind:
                total += self._add_bind(runner, model, rows, batch_size)
            else:
                total += self._add_literal(runner, model, rows, batch_size, on_duplicate, retry)
        return total

    def _add_literal(self, runner, model, rows, batch_size, on_duplicate, retry):
        total = 0
        for i in range(0, len(rows), batch_size):
            chunk = rows[i : i + batch_size]
            groups = {}
            for name, tagvals, coldict in chunk:
                entry = groups.setdefault(name, (tagvals, []))
                if entry[0] != tagvals:
                    entry[0] = tagvals
                entry[1].append([coldict[c] for c in model.__columns__])
            sql = _sql.insert_sql(model, groups, on_duplicate=on_duplicate)
            runner._run(sql, model=model, retry=retry)
            total += len(chunk)
        return total

    def _add_bind(self, runner, model, rows, batch_size):
        """原生参数绑定插入（taospy taos 2.8+ stmt2 绑定）。

        SQL 形状: ``INSERT INTO ? USING <stable> TAGS (?, ...) VALUES (?, ...)``
        参数为"按行"列表：每行 = [子表名, *标签值, *列值]，顺序与 SQL 中
        的 ``?`` 一一对应；绑定值还原为 Python 原生形态（时间戳 -> datetime）。
        每条语句绑定一个子表的多行数据。
        """
        cols = list(model.__columns__)
        tags = list(model.__tags__)
        tag_marks = ", ".join("?" for _ in tags)
        col_marks = ", ".join("?" for _ in cols)

        if tags:
            stmt = "INSERT INTO ? USING %s TAGS (%s) VALUES (%s)" % (
                _sql.quote_ident(model.__tablename__), tag_marks, col_marks,
            )
        else:
            stmt = "INSERT INTO ? VALUES (%s)" % col_marks

        # 按子表分组
        groups = {}
        for name, tagvals, coldict in rows:
            entry = groups.setdefault(name, [tagvals, []])
            if entry[0] != tagvals:
                entry[0] = tagvals
            entry[1].append([coldict[c] for c in cols])

        total = 0
        for name, (tagvals, values_rows) in groups.items():
            for i in range(0, len(values_rows), batch_size):
                chunk = values_rows[i : i + batch_size]
                datas = []
                for vrow in chunk:
                    row = [name]
                    row += [model.__tags__[t].dtype.from_db(tagvals.get(t)) for t in tags]
                    row += [
                        model.__columns__[c].dtype.from_db(vrow[j]) for j, c in enumerate(cols)
                    ]
                    datas.append(row)
                runner._run(stmt, model=model, retry=False, params=datas)
                total += len(chunk)
        return total

    def add_all(self, instances, **kwargs):
        """等价 ``add(*instances, **kwargs)``。"""
        return self.add(*list(instances), **kwargs)

    # ------------------------------------------------------------ 删除
    def delete(self, model, *conds, **kwargs):
        """按条件删除。至少需要一个条件（通常按时间范围）。"""
        return self.query(model).filter(**kwargs).where(*conds).delete()