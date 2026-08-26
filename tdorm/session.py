"""会话：在一条共享连接上连续执行多个操作（配合连接池复用）。

用法::

    with engine.session() as s:
        s.create_database(keep=30)
        s.create(Model)
        s.add(Model(ts=..., ...))     # 同一连接执行
        s.execute("INSERT ...")       # 裸语句
        rows = s.query(Model).filter(...).all()
    # 退出时自动 commit
"""

from __future__ import annotations

from . import sql as _sql
from .query import Query

__all__ = ["Session", "RestSession"]


class Session:
    """基于单条连接的执行器，对外暴露与 TDEngine 相近的子集。"""

    def __init__(self, engine, conn):
        self._engine = engine
        self._conn = conn
        self._cursor = None
        self._closed = False

    # ------------------------------------------------------------ 内部
    def _get_cursor(self):
        if self._cursor is None:
            self._cursor = self._conn.cursor()
        return self._cursor

    def _use(self, model=None):
        db = getattr(model, "__database__", None) if model is not None else None
        if db and db != self._engine.database:
            self._get_cursor().execute("USE %s" % _sql.quote_ident(db))

    def _run(self, sql, model=None, fetch=False, retry=False, params=None):
        cur = self._get_cursor()
        self._use(model)
        cur.execute(sql, params)
        if fetch:
            columns = [d[0] for d in (cur.description or [])]
            return columns, cur.fetchall() or []
        try:
            return cur.affected_rows
        except Exception:  # pragma: no cover
            return None

    def _query(self, sql):
        return self._run(sql, fetch=True)

    def _execute(self, sql, model=None):
        return self._run(sql, model=model)

    # ------------------------------------------------------------ 事务
    def commit(self):
        if self._conn is not None and hasattr(self._conn, "commit"):
            self._conn.commit()

    def rollback(self):
        if self._conn is not None and hasattr(self._conn, "rollback"):
            self._conn.rollback()

    def begin(self):
        """显式开启事务（native 协议）。"""
        self._get_cursor().execute("BEGIN")

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._cursor is not None and hasattr(self._cursor, "close"):
            try:
                self._cursor.close()
            except Exception:  # pragma: no cover
                pass
        pool = self._engine._pool
        if pool is not None:
            pool.release(self._conn)
        else:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover
                pass
        self._conn = None

    # ------------------------------------------------------------ 公共 API
    def execute(self, sql: str):
        return self._run(sql)

    def query(self, target):
        """字符串 -> 裸查询 ``(列名, 行)``；模型类 -> 链式查询。"""
        if isinstance(target, str):
            return self._run(target, fetch=True)
        return Query(self, target)

    def use(self, database: str):
        self._get_cursor().execute("USE %s" % _sql.quote_ident(database))

    def create(self, model, create_database: bool = False, if_not_exists: bool = True):
        if create_database:
            db = model.__database__ or self._engine.database
            if not db:
                from .exceptions import ConfigurationError

                raise ConfigurationError("create_database=True 需要模型带 __database__ 或引擎默认库")
            self.create_database(db)
        stmt = (
            _sql.create_stable_sql(model, if_not_exists)
            if model.__tags__
            else _sql.create_table_sql(model, if_not_exists)
        )
        return self._run(stmt, model=model)

    def drop(self, model):
        return self._run(_sql.drop_table_sql(model), model=model)

    def truncate(self, model):
        return self._run(_sql.truncate_sql(model), model=model)

    def create_database(self, database: str = None, **opts):
        db = database or self._engine.database
        if not db:
            from .exceptions import ConfigurationError

            raise ConfigurationError("create_database 需要 database 参数或引擎默认库")
        return self._run(_sql.create_database_sql(db, **opts))

    def add(self, *instances, **kwargs):
        """同 ``TDEngine.add``，但复用本会话的连接。"""
        return self._engine._add(instances, runner=self, **kwargs)

    def add_all(self, instances, **kwargs):
        return self.add(*list(instances), **kwargs)

    def delete(self, model, *conds, **kwargs):
        return self.query(model).filter(**kwargs).where(*conds).delete()


class RestSession:
    """REST 会话：无长连接与事务，委托内置 RestTransport 执行。"""

    def __init__(self, engine):
        self._engine = engine
        self._transport = engine._rest
        self._closed = False

    # ---- runner 接口 -------------------------------------------------
    def _run(self, sql, model=None, fetch=False, retry=False, params=None):
        return self._transport._run(sql, model=model, fetch=fetch,
                                    retry=retry, params=params)

    def _query(self, sql):
        return self._run(sql, fetch=True)

    def _execute(self, sql, model=None):
        return self._run(sql, model=model)

    # ---- 事务占位（REST 无事务） --------------------------------------
    def begin(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 底层 requests.Session 由 engine.close() 统一关闭，此处只做标记

    # ---- 公共 API ----------------------------------------------------
    def execute(self, sql: str):
        return self._run(sql)

    def query(self, target):
        if isinstance(target, str):
            return self._run(target, fetch=True)
        return Query(self, target)

    def use(self, database: str):
        # REST 无会话状态：仅切换后续请求的默认库
        self._transport.database = database

    def create(self, model, create_database: bool = False, if_not_exists: bool = True):
        if create_database:
            db = model.__database__ or self._engine.database
            if not db:
                from .exceptions import ConfigurationError

                raise ConfigurationError("create_database=True 需要模型带 __database__ 或引擎默认库")
            self.create_database(db)
        stmt = (
            _sql.create_stable_sql(model, if_not_exists)
            if model.__tags__
            else _sql.create_table_sql(model, if_not_exists)
        )
        return self._run(stmt, model=model)

    def drop(self, model):
        return self._run(_sql.drop_table_sql(model), model=model)

    def truncate(self, model):
        return self._run(_sql.truncate_sql(model), model=model)

    def create_database(self, database: str = None, **opts):
        db = database or self._engine.database
        if not db:
            from .exceptions import ConfigurationError

            raise ConfigurationError("create_database 需要 database 参数或引擎默认库")
        return self._run(_sql.create_database_sql(db, **opts))

    def add(self, *instances, **kwargs):
        return self._engine._add(instances, runner=self, **kwargs)

    def add_all(self, instances, **kwargs):
        return self.add(*list(instances), **kwargs)

    def delete(self, model, *conds, **kwargs):
        return self.query(model).filter(**kwargs).where(*conds).delete()