"""tdorm 异常定义。"""


class TDormError(Exception):
    """所有 tdorm 异常的基类。"""


class ConfigurationError(TDormError):
    """配置错误：连接参数、驱动缺失等。"""


class ValidationError(TDormError):
    """模型定义或数据校验错误。"""


class QueryError(TDormError):
    """查询构建或执行错误。"""


class DriverError(TDormError):
    """底层驱动（taospy）相关错误。"""


class PoolTimeoutError(TDormError):
    """连接池获取连接超时。"""