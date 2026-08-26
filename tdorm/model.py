"""声明式模型：``Model`` 基类 + 元类 + ``Field``/``Tag`` 列运算符。

用法示例::

    class SensorData(Model):
        __tablename__ = "sensor_data"
        __database__ = "iot"

        device_id = Tag(NChar(64))
        location  = Tag(NChar(64))

        ts          = Field(Timestamp)   # 时间戳主键（第一列）
        temperature = Field(Double)
        humidity    = Field(Double)
"""

from __future__ import annotations

import hashlib
import re

from . import sql as _sql
from .exceptions import ConfigurationError, ValidationError
from .fields import Timestamp as _Timestamp

__all__ = ["Model", "Field", "Tag"]

# 字段名不允许与以下保留方法名冲突
_RESERVED_ATTRS = frozenset(
    {
        "query", "insert", "create", "delete", "drop", "save", "all",
        "table_name", "subtable_name", "database", "fields", "tags",
        "columns", "filter", "update", "count", "first", "last",
    }
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Field:
    """普通（数据）列。``__eq__`` 等运算符被重载为查询条件，与 SQLAlchemy 习惯一致。"""

    __hash__ = object.__hash__  # 重载 __eq__ 后保持可哈希

    def __init__(self, dtype, default=None, nullable: bool = True):
        # 兼容 Field(Timestamp) 与 Field(Timestamp()) 两种写法
        self.dtype = dtype() if isinstance(dtype, type) else dtype
        self.default = default
        self.nullable = nullable
        self.name = None  # 元类绑定

    # ---- 查询条件运算符 -------------------------------------------------
    def __eq__(self, other):
        if other is None:
            return _sql.Cond(self, "IS NULL")
        return _sql.Cond(self, "=", other)

    def __ne__(self, other):
        if other is None:
            return _sql.Cond(self, "IS NOT NULL")
        return _sql.Cond(self, "!=", other)

    def __lt__(self, other):
        return _sql.Cond(self, "<", other)

    def __le__(self, other):
        return _sql.Cond(self, "<=", other)

    def __gt__(self, other):
        return _sql.Cond(self, ">", other)

    def __ge__(self, other):
        return _sql.Cond(self, ">=", other)

    def in_(self, values):
        return _sql.Cond(self, "IN", list(values))

    def notin_(self, values):
        return _sql.Cond(self, "NOT IN", list(values))

    def like(self, pattern: str):
        return _sql.Cond(self, "LIKE", pattern)

    def notlike(self, pattern: str):
        return _sql.Cond(self, "NOT LIKE", pattern)

    def between(self, low, high):
        """注意：TDengine 不直接支持 BETWEEN，这里展开为 >= / <=。"""
        return (self >= low) & (self <= high)

    def is_null(self):
        return _sql.Cond(self, "IS NULL")

    def is_not_null(self):
        return _sql.Cond(self, "IS NOT NULL")

    def __repr__(self):  # pragma: no cover
        return "<Field %s %s>" % (self.name, self.dtype.sql)


class Tag(Field):
    """标签列：用于超级表 TAGS，取值决定子表名。"""

    def __repr__(self):  # pragma: no cover
        return "<Tag %s %s>" % (self.name, self.dtype.sql)


class ModelMeta(type):
    def __new__(mcs, name, bases, namespace):
        abstract = bool(namespace.get("__abstract__", False))
        tablename = namespace.get("__tablename__")

        # 继承父类的列与标签（先基类后子类，子类可覆盖同名定义）
        columns = {}
        tags = {}
        for base in bases:
            columns.update(getattr(base, "__columns__", {}))
            tags.update(getattr(base, "__tags__", {}))

        for key, value in namespace.items():
            if isinstance(value, Tag):
                tags[key] = value
            elif isinstance(value, Field):
                columns[key] = value

        # 字段名校验：合法标识符且不与保留方法名冲突
        for key in list(columns) + list(tags):
            if not _IDENT_RE.match(key):
                raise ValidationError(
                    "字段名 %r 不是合法的 TDengine 标识符（字母/数字/下划线，不能以数字开头）" % key
                )
            if key in _RESERVED_ATTRS:
                raise ValidationError("字段名 %r 与 ORM 保留名称冲突" % key)

        for key, f in columns.items():
            f.name = key
        for key, f in tags.items():
            f.name = key

        cls = super().__new__(mcs, name, bases, namespace)
        cls.__abstract__ = abstract
        cls.__columns__ = columns
        cls.__tags__ = tags
        cls.__tablename__ = tablename or name.lower()
        cls.__database__ = namespace.get("__database__")
        cls.__ttl__ = namespace.get("__ttl__")

        overlapping = set(columns) & set(tags)
        if overlapping:
            raise ValidationError("%s 中列名与标签名重复：%s" % (name, sorted(overlapping)))

        if not abstract:
            # 主键校验：TDengine 要求以 TIMESTAMP 主键列开头；
            # 业务上允许存在多个时间列（首列为主键，其余为普通列）
            pks = [k for k, f in columns.items() if isinstance(f.dtype, _Timestamp)]
            if not pks:
                raise ValidationError(
                    "%s 必须定义一个 Timestamp 类型的时间戳主键列（如 ts = Field(Timestamp)）" % name
                )
            cls.__primary_ts__ = pks[0]
        else:
            cls.__primary_ts__ = namespace.get("__primary_ts__", None)
        return cls


class SubtableNameMixin:
    @classmethod
    def make_subtable_name(cls, **tag_values) -> str:
        """按标签值生成子表名；可用 ``__subtable_name__`` 类方法自定义。"""
        if hasattr(cls, "__subtable_name__"):
            name = cls.__subtable_name__(tag_values)
            if not name:
                raise ValidationError("__subtable_name__ 返回了空表名")
            return name
        parts = [cls.__tablename__]
        for t in cls.__tags__:
            parts.append(_sanitize_tag_value(t, tag_values.get(t)))
        name = "_".join(parts)
        # TDengine 表名上限 192 字节：超长时截断并追加哈希后缀
        if len(name.encode("utf-8")) > 190:
            digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:12]
            name = name.encode("utf-8")[:160].decode("utf-8", "ignore") + "_" + digest
        return name


def _sanitize_tag_value(tag_name: str, value) -> str:
    """把标签值编码成合法的子表名片段（可逆性不做保证，保证唯一与合法）。"""
    if value is None:
        return "null"
    text = str(value)
    out = []
    for ch in text:
        if "a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch == "_":
            out.append(ch)
        else:
            out.append("_" + ch.encode("utf-8").hex())
    frag = "".join(out) or "null"
    if frag[0].isdigit():
        frag = "_" + frag
    return frag


class Model(SubtableNameMixin, metaclass=ModelMeta):
    """ORM 模型基类。"""

    __abstract__ = True

    def __init__(self, **kwargs):
        # 先按默认值初始化所有列与标签
        for key, f in self.__columns__.items():
            value = kwargs.pop(key, None)
            if value is None and f.default is not None:
                value = f.default() if callable(f.default) else f.default
            if value is not None:
                value = f.dtype.coerce(value)
            setattr(self, key, value)
        for key, f in self.__tags__.items():
            value = kwargs.pop(key, None)
            if value is None and f.default is not None:
                value = f.default() if callable(f.default) else f.default
            if value is not None:
                value = f.dtype.coerce(value)
            setattr(self, key, value)
        if kwargs:
            raise ValidationError(
                "%s 没有字段: %s" % (type(self).__name__, ", ".join(sorted(kwargs)))
            )

    # ---- 便捷元信息 -----------------------------------------------------
    @classmethod
    def database(cls) -> str:
        return cls.__database__

    @classmethod
    def table_name(cls) -> str:
        return cls.__tablename__

    @property
    def subtable_name(self) -> str:
        """当前实例对应的子表名（需已设置全部标签值）。"""
        if not self.__tags__:
            return self.__tablename__
        missing = [t for t in self.__tags__ if getattr(self, t, None) is None]
        if missing:
            raise ValidationError(
                "%s 缺少标签值: %s" % (type(self).__name__, ", ".join(missing))
            )
        return type(self).make_subtable_name(**{t: getattr(self, t) for t in self.__tags__})

    def __repr__(self):  # pragma: no cover
        pk = self.__primary_ts__
        ts = getattr(self, pk, None)
        return "<%s %s=%r>" % (type(self).__name__, pk, ts)