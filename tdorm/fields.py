"""TDengine 数据类型与字段定义。

每个 TDType 负责两件事：
- ``coerce``：把 Python 值转换成可写入 TDengine 的规范值（例如 datetime -> 毫秒时间戳）；
- ``from_db``：把驱动返回的行值还原成 Python 值（例如 REST 接口返回的字符串时间 -> datetime）。
"""

from __future__ import annotations

import datetime as _dt
import re

__all__ = [
    "TDType",
    "Timestamp",
    "TinyInt",
    "SmallInt",
    "Int",
    "BigInt",
    "UInt",
    "UBigInt",
    "Float",
    "Double",
    "Bool",
    "Varchar",
    "NChar",
    "Json",
]

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _parse_ts_str(text: str) -> int:
    """把时间字符串解析为毫秒时间戳。支持常见格式与尾部时区偏移。"""
    text = text.strip()
    if not text:
        raise ValueError("空字符串无法解析为时间戳")
    # 纯数字字符串按毫秒处理（REST 接口可能返回这样的字符串）
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    norm = text.replace("Z", "+00:00")
    # 仅剥离【末尾】且【值域合法】的时区偏移：±HH / ±HH:MM / ±HHMM（时区最大 ±14h）
    m = re.search(r"([+-])(?:[0-9]|0\d|1[0-4])(?::?[0-5]\d)?$", norm)
    tz = None
    if m:
        sign = 1 if m.group(1) == "+" else -1
        frag = m.group(0)[1:]
        if ":" in frag:
            hh, mm = frag.split(":")
        else:
            hh, mm = frag[:2], frag[2:]
        tz = _dt.timezone(sign * _dt.timedelta(hours=int(hh), minutes=int(mm or 0)))
        norm = norm[: m.start()]
    body = norm.strip()
    for fmt in _TS_FORMATS:
        try:
            dt = _dt.datetime.strptime(body, fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError("无法解析时间字符串: %r" % (text,))
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp() * 1000)


class TDType:
    """数据类型基类。子类必须设置 ``sql``（DDL 中的类型名）。"""

    sql: str = None  # type: ignore[assignment]

    def coerce(self, value):
        """Python 值 -> TDengine 规范值（写入前）。"""
        return value

    def from_db(self, value):
        """驱动行值 -> Python 值（读取后）。"""
        return value

    def __repr__(self):  # pragma: no cover
        return "<%s sql=%s>" % (type(self).__name__, self.sql)


_EPOCH_UTC = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _epoch_to_local(seconds: float) -> _dt.datetime:
    """epoch 秒数 -> 本机时区的 aware datetime（跨平台安全）。"""
    try:
        return _dt.datetime.fromtimestamp(seconds).astimezone()
    except (OSError, OverflowError, ValueError):
        return (_EPOCH_UTC + _dt.timedelta(seconds=seconds)).astimezone()


class Timestamp(TDType):
    """时间戳主键列。写入时统一转成毫秒时间戳整数。

    ``sql_style`` 模块级控制 SQL 字面量输出：
    - "ms"（默认）：epoch 毫秒数字，标准 TDengine 均支持；
    - "iso"：ISO8601 UTC 字符串（``'2026-08-01T00:00:00.000Z'``），
      用于只接受字符串时间戳的私有/改装平台。
    """

    sql = "TIMESTAMP"
    sql_style = "ms"  # "ms" | "iso"

    def coerce(self, value):
        if value is None or isinstance(value, int):
            return value
        if isinstance(value, bool):  # bool 是 int 子类，先排除
            raise TypeError("布尔值不能作为时间戳")
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            return _parse_ts_str(value)
        if isinstance(value, bytes):
            return _parse_ts_str(value.decode("utf-8"))
        if isinstance(value, _dt.datetime):
            # naive 时间按本机时区解释（与 taospy 默认 timezone 行为保持一致）
            return int(value.timestamp() * 1000)
        if isinstance(value, _dt.date):
            return int(_dt.datetime(value.year, value.month, value.day).timestamp() * 1000)
        raise TypeError("无法将 %r 转换为 TIMESTAMP" % (value,))

    def from_db(self, value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            # 用 UTC 基准 + offset 计算，规避 Windows 上 fromtimestamp 的 epoch 附近限制
            return _epoch_to_local(value / 1000.0)
        if isinstance(value, str):
            return _epoch_to_local(_parse_ts_str(value) / 1000.0)
        if isinstance(value, _dt.datetime):
            return value
        raise TypeError("无法将 %r 还原为时间戳" % (value,))


class _IntType(TDType):
    def coerce(self, value):
        if value is None or isinstance(value, bool):
            return value if value is not None else None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            return int(value)
        raise TypeError("无法将 %r 转换为 %s" % (value, self.sql))

    def from_db(self, value):
        if value is None:
            return None
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            return value


class TinyInt(_IntType):
    sql = "TINYINT"


class SmallInt(_IntType):
    sql = "SMALLINT"


class Int(_IntType):
    sql = "INT"


class BigInt(_IntType):
    sql = "BIGINT"


class UInt(_IntType):
    sql = "UINT"


class UBigInt(_IntType):
    sql = "UBIGINT"


class _FloatType(TDType):
    def coerce(self, value):
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str):
            return float(value)
        raise TypeError("无法将 %r 转换为 %s" % (value, self.sql))

    def from_db(self, value):
        if value is None:
            return None
        if isinstance(value, float):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            return value


class Float(_FloatType):
    sql = "FLOAT"


class Double(_FloatType):
    sql = "DOUBLE"


class Bool(TDType):
    sql = "BOOL"

    def coerce(self, value):
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "t", "yes", "on"):
                return True
            if low in ("false", "0", "f", "no", "off"):
                return False
            raise ValueError("无法将 %r 解析为 BOOL" % (value,))
        raise TypeError("无法将 %r 转换为 BOOL" % (value,))

    def from_db(self, value):
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return self.coerce(value)
        return value


class _StrType(TDType):
    def __init__(self, length: int):
        if not isinstance(length, int) or length <= 0:
            raise ValueError("字符串类型长度必须为正整数")
        self.length = length

    def coerce(self, value):
        if value is None:
            return None
        if isinstance(value, bytes):
            value = value.decode("utf-8")
        if not isinstance(value, str):
            value = str(value)
        if len(value.encode("utf-8")) > self.length:
            raise ValueError(
                "%s 长度超限: %d > %d（按 UTF-8 字节计）" % (self.sql, len(value.encode("utf-8")), self.length)
            )
        return value

    def from_db(self, value):
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)


class Varchar(_StrType):
    """变长字符串（按字节计数）。TDengine 3.x 中 VARCHAR 与 BINARY 等价。"""

    def __init__(self, length: int = 64):
        super().__init__(length)
        self.sql = "VARCHAR(%d)" % self.length


class NChar(_StrType):
    """Unicode 字符串（按字符存储）。"""

    def __init__(self, length: int = 64):
        super().__init__(length)
        self.sql = "NCHAR(%d)" % self.length


class Json(TDType):
    """JSON 类型，TDengine 3.x 中仅可用于标签列。"""

    sql = "JSON"

    def coerce(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        import json

        return json.dumps(value, ensure_ascii=False)

    def from_db(self, value):
        if value is None:
            return None
        return value