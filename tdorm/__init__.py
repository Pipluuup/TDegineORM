"""tdorm —— 轻量级 TDengine 3.x 时序数据库 ORM。

快速上手::

    from tdorm import TDEngine, Model, Field, Tag, Timestamp, Double, NChar

    class SensorData(Model):
        __database__ = "iot"
        __tablename__ = "sensor_data"
        device_id = Tag(NChar(64))     # 标签（TAGS -> 子表）
        location  = Tag(NChar(32))

        ts          = Field(Timestamp)  # 时间戳主键
        temperature = Field(Double)
        humidity    = Field(Double)

    engine = TDEngine(database="iot")
    engine.create_database()
    engine.create(SensorData)
    engine.add(SensorData(ts="2023-01-01 00:00:00", temperature=25.5,
                          humidity=60, device_id="d1", location="beijing"))
    rows = engine.query(SensorData) \\
        .filter(device_id="d1", temperature__gt=24) \\
        .order_by(SensorData.ts, desc=True).limit(10).all()
"""

from .engine import TDEngine
from .exceptions import (
    ConfigurationError,
    DriverError,
    PoolTimeoutError,
    QueryError,
    TDormError,
    ValidationError,
)
from .fields import (
    BigInt,
    Bool,
    Double,
    Float,
    Int,
    Json,
    NChar,
    SmallInt,
    Timestamp,
    TinyInt,
    UBigInt,
    UInt,
    Varchar,
)
from .model import Field, Model, Tag
from .query import Query
from .session import Session

__version__ = "0.5.0"

__all__ = [
    "TDEngine",
    "Model",
    "Field",
    "Tag",
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
    "Query",
    "Session",
    "TDormError",
    "ConfigurationError",
    "ValidationError",
    "QueryError",
    "DriverError",
    "PoolTimeoutError",
    "__version__",
]


def create_engine(**kwargs) -> TDEngine:
    """便捷工厂：``create_engine(host=..., database=...)``。"""
    return TDEngine(**kwargs)