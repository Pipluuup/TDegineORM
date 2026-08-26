"""内置 REST 传输层（TDengine taosAdapter 的 ``/rest/sql`` 接口）。

不依赖 taospy，适合**只有 REST 端口（默认 6041）开放**的部署。
行数据保持 JSON 原生形态（字符串时间戳 / 字符串数字 / bool / None），
由上层 ``dtype.from_db`` 还原为 Python 类型。
"""

from __future__ import annotations

import requests

from .exceptions import DriverError, TDormError
from .fields import Timestamp

__all__ = ["RestTransport"]

_RETRYABLE_PREFIXES = (
    "SELECT", "SHOW", "DESCRIBE", "CREATE", "DROP", "TRUNCATE",
    "USE", "DELETE", "ALTER",
)


class RestTransport:
    """TDengine REST 执行器：实现与 TDEngine/Session 相同的 runner 接口。"""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6041,
        user: str = "root",
        password: str = "taosdata",
        database: str = None,
        timeout: float = 30.0,
        ts_style: str = "ms",  # "ms"(epoch数字) | "iso"(ISO8601字符串)
        **kwargs,
    ):
        self.host = host
        self.port = int(port)
        self.database = database
        self.timeout = float(timeout)
        self.ts_style = "iso" if str(ts_style).lower() == "iso" else "ms"
        # SQL 字面量风格在 SQL 构建前就需生效，故在构造时即设置进程级标识
        Timestamp.sql_style = self.ts_style
        self._auth = (user, password)
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------ 内部
    def _url(self, db=None):
        db = db or self.database
        return "%s/rest/sql%s" % ("http://%s:%d" % (self.host, self.port), "/" + db if db else "")

    def _request(self, sql: str, db=None):
        try:
            resp = self._session.post(
                self._url(db), data=sql.encode("utf-8"),
                auth=self._auth, timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise DriverError("REST 请求失败（%s）: %s" % (self._url(db), exc)) from exc
        if resp.status_code != 200:
            raise DriverError("REST HTTP %d: %s" % (resp.status_code, resp.text[:300]))
        try:
            payload = resp.json()
        except ValueError:
            raise DriverError("REST 响应非 JSON: %s" % resp.text[:300]) from None
        code = payload.get("code", -1)
        if code != 0:
            raise TDormError(
                "TDengine REST 错误 %d: %s" % (code, payload.get("desc", "(无描述)"))
            )
        return payload

    def _run(self, sql: str, model=None, fetch: bool = False, retry: bool = False,
             params=None):
        if params is not None:
            raise DriverError("REST 传输不支持参数绑定：bind=True 仅限 native 协议")
        # SQL 字面量风格：进程内全局（tdorm 文档注明：同一进程混用多风格引擎需自行协调）
        Timestamp.sql_style = self.ts_style
        db = getattr(model, "__database__", None) if model is not None else None
        try:
            payload = self._request(sql, db=db)
        except TDormError:
            if retry and _is_retryable(sql):
                payload = self._request(sql, db=db)  # 幂等语句断连重试一次
            else:
                raise
        meta, data = payload.get("column_meta"), payload.get("data")
        if meta is None or data is None:
            # 非查询：受影响行数（DDL 为 0）
            return (payload.get("rows") or 0) if not fetch else ([], [])
        columns = [m[0] if isinstance(m, (list, tuple)) else m for m in meta]
        rows = data
        if fetch:
            return columns, rows
        return payload.get("rows") or len(rows)

    def _query(self, sql: str):
        return self._run(sql, fetch=True)

    def _execute(self, sql: str, model=None):
        return self._run(sql, model=model)

    def close(self):
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass


def _is_retryable(sql: str) -> bool:
    return sql.lstrip().upper().startswith(_RETRYABLE_PREFIXES)