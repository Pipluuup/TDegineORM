"""纯 SQL 生成模块（不依赖驱动，可离线单测）。

所有值一律经由 :func:`quote_literal` 转义后拼入 SQL，避免注入；
DDL / DML / INSERT / SELECT / DELETE 的字符串都从这里产出。
"""

from __future__ import annotations

from .exceptions import QueryError, ValidationError
from .fields import Timestamp

__all__ = [
    "quote_ident",
    "quote_literal",
    "create_database_sql",
    "drop_database_sql",
    "create_stable_sql",
    "create_table_sql",
    "create_subtable_sql",
    "drop_table_sql",
    "insert_sql",
    "select_sql",
    "agg_sql",
    "delete_sql",
    "truncate_sql",
    "Cond",
    "GroupExpr",
]

# 常见 TDengine 关键字：命中时标识符用反引号包裹
RESERVED = frozenset(
    """
    select insert delete update create drop alter truncate use show describe
    from where into values using on as distinct table tables database databases
    stable stables tag tags column columns partition group order by limit offset
    slimit soffset interval sliding fill asc desc join left right inner full cross
    count sum avg min max first last last_row spread stddev percentile top bottom
    diff twa irate interpolate now true false null and or not like in between is
    timestamp int bigint tinyint smallint uint ubigint float double bool binary
    nchar varchar json geometry keep days buffer wal precision replica cache ttl
    cachemodel singlestable duration watermark maxrows minrows
    """.split()
)

_IDENT_RE = None


def _ident_ok(name: str) -> bool:
    global _IDENT_RE
    import re

    if _IDENT_RE is None:
        _IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    return bool(_IDENT_RE.match(name)) and name.lower() not in RESERVED


def quote_ident(name: str) -> str:
    """标识符（表名 / 列名 / 标签名）安全引用。"""
    if isinstance(name, str) and _ident_ok(name):
        return name
    return "`" + str(name).replace("`", "``") + "`"


def quote_literal(value, dtype=None) -> str:
    """把值转成 TDengine 字面量；``dtype`` 存在时先做 coerce 归一化。

    - None        -> NULL
    - bool        -> TRUE / FALSE
    - int/float   -> 数字直写
    - str         -> 单引号包裹，内部单引号翻倍转义

    时间戳按 ``Timestamp.sql_style``：默认 epoch 毫秒数字；
    "iso" 风格输出 ISO8601 UTC 字符串（适配只接受字符串时间戳的私有平台）。
    """
    if value is None:
        return "NULL"
    if dtype is not None:
        value = dtype.coerce(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        if dtype is not None and isinstance(dtype, Timestamp) and Timestamp.sql_style == "iso":
            return _iso_ts_literal(int(value))
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise TypeError("无法将 %r 转换为 SQL 字面量" % (value,))


def _iso_ts_literal(epoch_ms: int) -> str:
    """epoch 毫秒 -> ``'YYYY-MM-DDTHH:MM:SS.SSSZ'``（UTC）字符串字面量。"""
    from datetime import datetime, timedelta, timezone

    dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=epoch_ms)
    return "'%sZ'" % dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------- 条件表达式

class Expr:
    """条件表达式基类：支持 & / | / ~ 组合。"""

    def __and__(self, other):
        return GroupExpr(self, other, "AND")

    def __or__(self, other):
        return GroupExpr(self, other, "OR")

    def __invert__(self):
        return NotExpr(self)

    def sql(self) -> str:  # pragma: no cover
        raise NotImplementedError


class NotExpr(Expr):
    def __init__(self, inner: Expr):
        self.inner = inner

    def sql(self) -> str:
        return "NOT (%s)" % self.inner.sql()


class Cond(Expr):
    """单条比较条件：``col op literal`` 或 ``col IS [NOT] NULL``。"""

    def __init__(self, field, op: str, value=None):
        self.field = field
        self.op = op
        self.value = value

    def sql(self) -> str:
        col = quote_ident(self.field.name)
        if self.op in ("IS NULL", "IS NOT NULL"):
            return "%s %s" % (col, self.op)
        if self.op == "IN" or self.op == "NOT IN":
            values = list(self.value)
            if not values:
                return "TRUE" if self.op == "IN" else "FALSE"
            literals = ",".join(quote_literal(v, self.field.dtype) for v in values)
            return "%s %s (%s)" % (col, self.op, literals)
        return "%s %s %s" % (col, self.op, quote_literal(self.value, self.field.dtype))

    def __repr__(self):  # pragma: no cover
        return "<Cond %s>" % self.sql()


class GroupExpr(Expr):
    """AND / OR 组合，每个操作数单独成括号，保证优先级正确。"""

    def __init__(self, lhs: Expr, rhs: Expr, joiner: str):
        self.lhs = lhs
        self.rhs = rhs
        self.joiner = joiner

    def sql(self) -> str:
        return "(%s) %s (%s)" % (self.lhs.sql(), self.joiner, self.rhs.sql())

    def __repr__(self):  # pragma: no cover
        return "<GroupExpr %s>" % self.sql()


def _cond_sql(cond) -> str:
    if cond is None:
        return ""
    return cond.sql()


# ---------------------------------------------------------------- DDL

def create_database_sql(name: str, **opts) -> str:
    """``CREATE DATABASE IF NOT EXISTS <db> [KEEP n DAYS n ...]``"""
    parts = ["CREATE DATABASE IF NOT EXISTS", quote_ident(name)]
    known = {
        "keep": "KEEP %d",
        "days": "DAYS %d",
        "buffer": "BUFFER %d",
        "cache_model": "CACHEMODEL '%s'",
        "wal_level": "WAL_LEVEL %d",
        "precision": "PRECISION '%s'",
        "replica": "REPLICA %d",
        "cache": "CACHE %s",
        "groups": "GROUPS %d",
        "quorum": "QUORUM %d",
        "strict": "STRICT '%s'",
        "duration": "DURATION %d",
        "maxrows": "MAXROWS %d",
        "minrows": "MINROWS %d",
        "watermark": "WATERMARK %d",
        "singlestable": "SINGLESTABLE %d",
        "ttl": "TTL %d",
    }
    clauses = []
    for key, value in opts.items():
        if value is None:
            continue
        k = key.lower().replace("-", "_")
        if k in known:
            clauses.append(known[k] % value)
        else:
            # 未知参数透传：键转大写、下划线转空格
            clauses.append("%s %s" % (k.upper().replace("_", " "), value))
    if clauses:
        parts.append(" ".join(clauses))
    return " ".join(parts)


def drop_database_sql(name: str) -> str:
    return "DROP DATABASE IF EXISTS %s" % quote_ident(name)


def _column_defs(model) -> str:
    cols = list(model.__columns__)
    pk = model.__primary_ts__
    ordered = [pk] + [c for c in cols if c != pk]  # 时间戳主键必须是第一列
    return ", ".join("%s %s" % (quote_ident(c), model.__columns__[c].dtype.sql) for c in ordered)


def _tag_defs(model) -> str:
    return ", ".join(
        "%s %s" % (quote_ident(t), model.__tags__[t].dtype.sql) for t in model.__tags__
    )


def create_stable_sql(model, if_not_exists: bool = True) -> str:
    if not model.__tags__:
        raise ValidationError("%s 没有标签列，请使用 create_table_sql" % model.__name__)
    head = "CREATE STABLE IF NOT EXISTS" if if_not_exists else "CREATE STABLE"
    sql = "%s %s (%s) TAGS (%s)" % (
        head,
        quote_ident(model.__tablename__),
        _column_defs(model),
        _tag_defs(model),
    )
    ttl = getattr(model, "__ttl__", None)
    if ttl:
        sql += " TTL %d" % int(ttl)
    return sql


def create_table_sql(model, if_not_exists: bool = True) -> str:
    if model.__tags__:
        raise ValidationError("%s 含标签列，请使用 create_stable_sql" % model.__name__)
    head = "CREATE TABLE IF NOT EXISTS" if if_not_exists else "CREATE TABLE"
    return "%s %s (%s)" % (head, quote_ident(model.__tablename__), _column_defs(model))


def create_subtable_sql(model, subtable: str, tag_values: dict) -> str:
    """``CREATE TABLE IF NOT EXISTS <sub> USING <stable> TAGS (...)``"""
    tags = list(model.__tags__)
    literals = ",".join(quote_literal(tag_values.get(t), model.__tags__[t].dtype) for t in tags)
    return "CREATE TABLE IF NOT EXISTS %s USING %s TAGS (%s)" % (
        quote_ident(subtable),
        quote_ident(model.__tablename__),
        literals,
    )


def drop_table_sql(model) -> str:
    kind = "STABLE" if model.__tags__ else "TABLE"
    return "DROP %s IF EXISTS %s" % (kind, quote_ident(model.__tablename__))


def truncate_sql(model) -> str:
    return "TRUNCATE TABLE %s" % quote_ident(model.__tablename__)


# ---------------------------------------------------------------- DML

def insert_sql(model, groups: dict, on_duplicate=None) -> str:
    """生成 INSERT 语句。

    ``groups``: {表名: (tags值dict或None, [值元组列表])}，值元组与
    ``model.__columns__`` 的声明顺序一致。多子表可在一条语句内链式插入。

    ``on_duplicate``: None | "update" | "ignore"，对应 TDengine 3.x 的
    ``ON CONFLICT DO UPDATE / DO NOTHING``（upsert）。
    """
    cols = list(model.__columns__)
    col_list = "(%s)" % ", ".join(quote_ident(c) for c in cols)
    has_tags = bool(model.__tags__)
    segments = []
    for name, (tag_values, rows) in groups.items():
        if not rows:
            continue
        value_parts = []
        for row in rows:
            literals = ", ".join(
                quote_literal(row[i], model.__columns__[cols[i]].dtype) for i in range(len(cols))
            )
            value_parts.append("(%s)" % literals)
        seg = "INSERT INTO %s" % quote_ident(name)
        if has_tags:
            tag_lits = ",".join(
                quote_literal(tag_values.get(t), model.__tags__[t].dtype)
                for t in model.__tags__
            )
            seg += " USING %s TAGS (%s)" % (quote_ident(model.__tablename__), tag_lits)
        seg += " %s VALUES %s" % (col_list, " ".join(value_parts))
        if on_duplicate == "update":
            seg += " ON CONFLICT DO UPDATE"
        elif on_duplicate == "ignore":
            seg += " ON CONFLICT DO NOTHING"
        elif on_duplicate is not None:
            raise ValidationError(
                "on_duplicate 只支持 None / 'update' / 'ignore'，得到 %r" % (on_duplicate,)
            )
        segments.append(seg)
    if not segments:
        raise QueryError("没有可写入的数据")
    return "\n".join(segments)


# ---------------------------------------------------------------- 查询

def _select_col(col: str) -> str:
    """查询列表中的列：含 ``(`` 或空格的视为原始表达式（如 COUNT(*)），否则安全引用。"""
    if isinstance(col, str) and ("(" in col or " " in col):
        return col
    return quote_ident(col)


def select_sql(
    model,
    cols,
    cond=None,
    *,
    order_by=None,
    limit=None,
    offset=None,
    group_by=None,
    partition_by=None,
    interval=None,
    sliding=None,
    fill=None,
) -> str:
    parts = ["SELECT %s FROM %s" % (", ".join(_select_col(c) for c in cols), quote_ident(model.__tablename__))]
    if cond is not None:
        parts.append("WHERE %s" % _cond_sql(cond))
    if partition_by:
        parts.append("PARTITION BY %s" % ", ".join(quote_ident(c) for c in partition_by))
    if interval is not None:
        seg = "INTERVAL(%s)" % interval
        if sliding is not None:
            seg += " SLIDING(%s)" % sliding
        if fill is not None:
            seg += " FILL(%s)" % fill
        parts.append(seg)
    if group_by:
        parts.append("GROUP BY %s" % ", ".join(quote_ident(c) for c in group_by))
    if order_by:
        parts.append("ORDER BY %s" % ", ".join(order_by))
    if limit is not None:
        tail = "LIMIT %d" % limit
        if offset:
            tail += " OFFSET %d" % offset
        parts.append(tail)
    return "\n".join(parts)


def agg_sql(
    model,
    expr: str,
    cond=None,
    *,
    group_by=None,
    partition_by=None,
    interval=None,
    sliding=None,
    fill=None,
    order_by=None,
    limit=None,
    offset=None,
) -> str:
    """聚合查询：``expr`` 为原始聚合表达式，如 ``COUNT(*)``、``AVG(temperature)``。"""
    parts = ["SELECT %s FROM %s" % (expr, quote_ident(model.__tablename__))]
    if cond is not None:
        parts.append("WHERE %s" % _cond_sql(cond))
    if partition_by:
        parts.append("PARTITION BY %s" % ", ".join(quote_ident(c) for c in partition_by))
    if interval is not None:
        seg = "INTERVAL(%s)" % interval
        if sliding is not None:
            seg += " SLIDING(%s)" % sliding
        if fill is not None:
            seg += " FILL(%s)" % fill
        parts.append(seg)
    if group_by:
        parts.append("GROUP BY %s" % ", ".join(quote_ident(c) for c in group_by))
    if order_by:
        parts.append("ORDER BY %s" % ", ".join(order_by))
    if limit is not None:
        parts.append("LIMIT %d" % limit + (" OFFSET %d" % offset if offset else ""))
    return "\n".join(parts)


def delete_sql(model, cond) -> str:
    if cond is None:
        raise ValidationError(
            "TDengine 的 DELETE 必须带有条件（通常按时间范围），请先用 where(...) 限定"
        )
    return "DELETE FROM %s WHERE %s" % (quote_ident(model.__tablename__), _cond_sql(cond))