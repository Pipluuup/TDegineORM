"""查询构建器：链式调用生成 SELECT / DELETE，结果映射到模型实例。"""

from __future__ import annotations

from . import sql as _sql
from .exceptions import ConfigurationError, QueryError, ValidationError
from .model import Field as _Field

__all__ = ["Query"]

_FILTER_OPS = {
    "eq": "=",
    "ne": "!=",
    "gt": ">",
    "ge": ">=",
    "lt": "<",
    "le": "<=",
    "like": "LIKE",
    "notlike": "NOT LIKE",
    "in": "IN",
    "notin": "NOT IN",
    "isnull": "IS NULL",
    "notnull": "IS NOT NULL",
}


class Query:
    """``engine.query(Model)`` 返回的链式查询对象。``runner`` 是具备
    ``_query(sql)`` / ``_execute(sql)`` 能力的执行器（TDEngine 或 Session）。"""

    def __init__(self, runner, model):
        self._runner = runner
        self._model = model
        self._conds = None
        self._select_cols = list(model.__columns__)
        self._with_tags = False
        self._order_by = None
        self._limit = None
        self._offset = None
        self._group_by = None
        self._partition_by = None
        self._interval = None
        self._sliding = None
        self._fill = None

    # ------------------------------------------------------------ 链式方法
    def where(self, *conds):
        """叠加条件，多个条件按 AND 组合。"""
        combined = None
        for c in conds:
            combined = c if combined is None else (combined & c)
        if combined is None:
            raise ValidationError("where() 需要至少一个条件")
        self._conds = combined if self._conds is None else (self._conds & combined)
        return self

    def filter(self, **kwargs):
        """键值对过滤：``filter(device_id='d1', temperature__gt=30)``。

        支持 ``__op`` 后缀：eq/ne/gt/ge/lt/le/like/notlike/in/notin/isnull/notnull。
        """
        for key, value in kwargs.items():
            if "__" in key:
                col, op = key.rsplit("__", 1)
            else:
                col, op = key, "eq"
            field = self._column(col)
            if op == "between":
                try:
                    low, high = value
                except (TypeError, ValueError):
                    raise ValidationError("between 需要 (low, high) 二元组，如 humidity__between=(40, 60)")
                cond = field.between(low, high)
            else:
                if op not in _FILTER_OPS:
                    raise ValidationError(
                        "不支持的操作符 %r（可用: %s）" % (op, ", ".join(_FILTER_OPS))
                    )
                if op == "in" or op == "notin":
                    cond = field.in_(value) if op == "in" else field.notin_(value)
                elif op == "isnull":
                    cond = field.is_null()
                elif op == "notnull":
                    cond = field.is_not_null()
                else:
                    cond = _sql.Cond(field, _FILTER_OPS[op], value)
            self._conds = cond if self._conds is None else (self._conds & cond)
        return self

    def select(self, *cols):
        """自定义查询列（Field 实例或列名字符串）。"""
        names = []
        for c in cols:
            if isinstance(c, _Field):
                names.append(c.name)
            elif isinstance(c, str):
                names.append(c)
            else:
                raise ValidationError("select() 只接受 Field 或列名字符串")
        if not names:
            raise ValidationError("select() 需要至少一列")
        self._select_cols = names
        return self

    def with_tags(self, on: bool = True):
        """把标签列一并 SELECT 出来（结果实例上可读标签值）。"""
        self._with_tags = on
        return self

    def order_by(self, *cols, desc: bool = False):
        """排序。接受 Field / 列名 / 或含空格的原始子句（如 ``"ts DESC"``）。"""
        if not cols:
            raise ValidationError("order_by() 需要至少一列")
        clauses = []
        for c in cols:
            if isinstance(c, _Field):
                clauses.append("%s %s" % (_sql.quote_ident(c.name), "DESC" if desc else "ASC"))
            elif isinstance(c, str):
                if " " in c:
                    clauses.append(c)  # 原始子句，调用方自行负责
                else:
                    clauses.append("%s %s" % (_sql.quote_ident(c), "DESC" if desc else "ASC"))
            else:
                raise ValidationError("order_by() 只接受 Field / 列名 / 原始子句")
        self._order_by = clauses
        return self

    def limit(self, n: int, offset: int = 0):
        self._limit = int(n)
        self._offset = int(offset) if offset else 0
        return self

    def group_by(self, *cols):
        self._group_by = [c.name if isinstance(c, _Field) else c for c in cols]
        return self

    def partition_by(self, *cols):
        self._partition_by = [c.name if isinstance(c, _Field) else c for c in cols]
        return self

    def interval(self, interval: str, sliding: str = None, fill: str = None):
        """时间窗口聚合，如 ``interval('1m', sliding='30s', fill='PREV')``。"""
        self._interval = interval
        self._sliding = sliding
        self._fill = fill
        return self

    # ------------------------------------------------------------ 执行
    @property
    def sql(self) -> str:
        """当前 SELECT 语句（调试用）。"""
        return self._select_sql()

    def _select_sql(self) -> str:
        cols = list(self._select_cols)
        if self._with_tags:
            cols += [t for t in self._model.__tags__ if t not in cols]
        return _sql.select_sql(
            self._model,
            cols,
            self._conds,
            order_by=self._order_by,
            limit=self._limit,
            offset=self._offset,
            group_by=self._group_by,
            partition_by=self._partition_by,
            interval=self._interval,
            sliding=self._sliding,
            fill=self._fill,
        )

    def _column(self, name: str):
        if name in self._model.__columns__:
            return self._model.__columns__[name]
        if name in self._model.__tags__:
            return self._model.__tags__[name]
        raise ValidationError("%s 没有字段或标签: %s" % (self._model.__name__, name))

    def _map_rows(self, columns, rows):
        model = self._model
        mapping = []
        for idx, colname in enumerate(columns):
            if colname in model.__columns__:
                mapping.append((idx, model.__columns__[colname].dtype))
            elif colname in model.__tags__:
                mapping.append((idx, model.__tags__[colname].dtype))
            else:
                mapping.append((idx, None))
        result = []
        for row in rows:
            obj = model()
            for idx, dtype in mapping:
                value = row[idx]
                if dtype is not None:
                    value = dtype.from_db(value)
                key = columns[idx]
                if key in model.__columns__ or key in model.__tags__:
                    setattr(obj, key, value)
            result.append(obj)
        return result

    def all(self):
        """执行查询，返回模型实例列表。"""
        columns, rows = self._runner._query(self._select_sql())
        return self._map_rows(columns, rows)

    def one(self):
        """返回第一条或 None。"""
        saved_limit, saved_offset = self._limit, self._offset
        self._limit = 1
        self._offset = 0
        try:
            items = self.all()
        finally:
            self._limit, self._offset = saved_limit, saved_offset
        return items[0] if items else None

    def scalars(self, col=None):
        """返回单列值列表；``col`` 缺省时取查询的第一列。"""
        if col is not None:
            self.select(col)
        if len(self._select_cols) != 1:
            raise ValidationError("scalars() 需要恰好一列，可以用 select(field) 指定")
        field_name = self._select_cols[0]
        field = self._column(field_name)
        dtype = field.dtype
        columns, rows = self._runner._query(self._select_sql())
        ideal = columns.index(field_name) if field_name in columns else 0
        return [dtype.from_db(r[ideal]) for r in rows]

    def count(self) -> int:
        """``COUNT(*)`` 返回行数。"""
        sql = _sql.agg_sql(self._model, "COUNT(*)", self._conds)
        _, rows = self._runner._query(sql)
        if not rows:
            return 0
        try:
            return int(rows[0][0])
        except (TypeError, ValueError):
            raise QueryError("COUNT(*) 返回值无法转换: %r" % (rows[0][0],))

    def agg(self, expr: str):
        """聚合查询，``expr`` 为原始表达式，如 ``AVG(temperature)`` / ``MAX(humidity)``。

        返回第一个单元格（配合 ``interval()`` 时返回最后一行窗口的结果）。
        """
        sql = _sql.agg_sql(
            self._model, expr, self._conds,
            group_by=self._group_by,
            partition_by=self._partition_by,
            interval=self._interval,
            sliding=self._sliding,
            fill=self._fill,
            order_by=self._order_by,
            limit=self._limit,
            offset=self._offset,
        )
        _, rows = self._runner._query(sql)
        return rows[0][0] if rows else None

    # ------------------------------------------------------------ 聚合助手
    def _col_expr(self, col) -> str:
        """把 Field / 列名 / 原始表达式归一为 SQL 片段。"""
        if isinstance(col, _Field):
            return _sql.quote_ident(col.name)
        if isinstance(col, str):
            if "(" in col or " " in col or any(op in col for op in "+-*/<>="):
                return col  # 原始表达式，调用方负责
            return _sql.quote_ident(col)
        raise ValidationError("聚合列只接受 Field / 列名 / 原始表达式，得到 %r" % (col,))

    def sum(self, col):
        return self._agg_value("SUM", col)

    def avg(self, col):
        return self._agg_value("AVG", col)

    def max(self, col):
        return self._agg_value("MAX", col)

    def min(self, col):
        return self._agg_value("MIN", col)

    def spread(self, col):
        """时间窗口内最大值-最小值。"""
        return self._agg_value("SPREAD", col)

    def stddev(self, col):
        return self._agg_value("STDDEV", col)

    def twa(self, col):
        """时间加权平均（需配合 ``interval()``）。"""
        return self._agg_value("TWA", col)

    def irate(self, col):
        """瞬时增长率（需配合 ``interval()``）。"""
        return self._agg_value("IRATE", col)

    def first(self, col):
        """窗口内第一个值。"""
        return self._agg_value("FIRST", col)

    def last(self, col):
        """窗口内最后一个值。"""
        return self._agg_value("LAST", col)

    def percentile(self, col, p: float):
        """分位数（0-100 或 0-1，取决于版本约定）。"""
        value = self.agg("PERCENTILE(%s, %g)" % (self._col_expr(col), p))
        if isinstance(col, _Field) and value is not None:
            return col.dtype.from_db(value)
        return value

    def _agg_value(self, fn: str, col):
        value = self.agg("%s(%s)" % (fn, self._col_expr(col)))
        if isinstance(col, _Field) and value is not None:
            return col.dtype.from_db(value)
        return value

    def top(self, col, k: int = 10):
        """数值最大的 k 行（聚合值），返回标量列表。"""
        return self._agg_rows("TOP", col, k)

    def bottom(self, col, k: int = 10):
        """数值最小的 k 行（聚合值），返回标量列表。"""
        return self._agg_rows("BOTTOM", col, k)

    def _agg_rows(self, fn: str, col, k: int):
        expr = "%s(%s, %d)" % (fn, self._col_expr(col), int(k))
        sql = _sql.agg_sql(self._model, expr, self._conds,
                           partition_by=self._partition_by,
                           interval=self._interval, sliding=self._sliding, fill=self._fill)
        _, rows = self._runner._query(sql)
        values = [r[0] for r in rows]
        if isinstance(col, _Field):
            return [col.dtype.from_db(v) for v in values if v is not None]
        return values

    def diff(self, col):
        """逐行差值，返回 ``[(ts, diff), ...]`` 元组列表（不映射为模型实例）。"""
        expr = "DIFF(%s)" % self._col_expr(col)
        sql = _sql.agg_sql(self._model, expr, self._conds,
                           partition_by=self._partition_by,
                           interval=self._interval, sliding=self._sliding, fill=self._fill)
        _, rows = self._runner._query(sql)
        return list(rows)

    def last_row(self):
        """按时间戳取最后一行（等价 LAST_ROW，但支持 where 过滤），返回实例或 None。"""
        pk = self._model.__primary_ts__
        saved_order = self._order_by
        self._order_by = ["%s DESC" % _sql.quote_ident(pk)]
        try:
            return self.one()
        finally:
            self._order_by = saved_order

    def delete(self):
        """按当前条件删除（必须带 where 条件）。"""
        sql = _sql.delete_sql(self._model, self._conds)
        return self._runner._execute(sql)