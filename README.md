# tdorm — TDengine 时序数据库 ORM（Python）

轻量级 **TDengine 3.x** 时序数据库 ORM：

- **声明式模型**：用类定义列与标签，自动生成 `CREATE DATABASE / CREATE STABLE / CREATE TABLE`
- **批量写入**：一次 `add(...)` 按标签自动分组 → 子表，单条 INSERT 内链式写入多子表，天然规避子表去重
- **链式查询**：`filter/where/order_by/limit` 构建 SELECT，聚合、`last_row`、按时间范围删除
- **类型安全**：datetime ⇄ 毫秒时间戳、REST 字符串 ⇄ Python 类型自动转换
- **防注入**：所有值统一转义后才拼入 SQL
- **零依赖导入**：`taospy` 惰性加载，未装驱动/没有服务器也可导入并做 SQL 级单测

## 安装

```bash
pip install taospy        # 官方驱动（唯一运行时依赖，建议 >=2.8.0 以支持原生绑定）
```

**从本仓库安装**（仓库已公开，无需账号）：

```bash
pip install "tdorm @ git+https://github.com/Pipluuup/TDegineORM.git"
# 等效写法： pip install git+https://github.com/Pipluuup/TDegineORM.git
```

写入其他项目的 `requirements.txt`：

```text
tdorm @ git+https://github.com/Pipluuup/TDegineORM.git
```

> pip 会读取仓库内 `pyproject.toml` 自动构建并安装运行时依赖 `taospy>=2.8.0`。
> `taospy` 惰性加载：连接前无需驱动，可先 `import tdorm` 做模型定义与 SQL 级单测。
> 详细安装与使用见 [`docs/install_and_use.md`](docs/install_and_use.md)。


## 快速上手

```python
from tdorm import TDEngine, Model, Field, Tag, Timestamp, Double, Bool, NChar

class DeviceReading(Model):
    __database__ = "demo"          # 库名
    __tablename__ = "device_reading"   # 超级表名（缺省=类名小写）

    device_id = Tag(NChar(32))     # 标签 TAGS
    region    = Tag(NChar(16))

    ts          = Field(Timestamp) # 时间戳主键（首列；可再定义其他时间列）
    temperature = Field(Double)
    humidity    = Field(Double)
    online      = Field(Bool, default=True)

engine = TDEngine(host="localhost", port=6030, user="root",
                  password="taosdata", database="demo", auto_create=True)

with engine:
    engine.create_database(keep=365, days=10)
    engine.create(DeviceReading)

    # 写入：按 (device_id, region) 自动决定子表名，一条语句链式写入
    engine.add(DeviceReading(ts="2023-01-01 00:00:00", temperature=25.5,
                             humidity=60, device_id="d1", region="beijing"))

    # 查询
    rows = (engine.query(DeviceReading)
            .filter(device_id="d1", temperature__gt=24)
            .order_by(DeviceReading.ts, desc=True)
            .limit(100)
            .all())
    for r in rows:
        print(r.ts, r.temperature)          # ts 已还原为 datetime

    avg = engine.query(DeviceReading).filter(device_id="d1").agg("AVG(temperature)")
    last = engine.query(DeviceReading).filter(device_id="d1").last_row()

    # 按时间范围删除（TDengine 要求 DELETE 带条件）
    engine.query(DeviceReading).where(DeviceReading.ts < some_ts).delete()
```

## 模型定义

| 概念 | 写法 | 说明 |
|---|---|---|
| 列 | `Field(Timestamp)` / `Field(Double)` | 时间戳主键必需；**允许多个时间列**（首列为主键，其余作普通列） |
| 标签 | `Tag(NChar(32))` | 有标签即超级表，标签取值决定子表名 |
| 默认值 | `Field(Double, default=50.0)` | 支持 callable（如 `default=utcnow`） |
| 库名/表名 | `__database__` / `__tablename__` | 表名缺省为类名小写 |
| TTL | `__ttl__ = 365` | 超级表级 TTL（TDengine 3.0.5+） |
| 自定义子表名 | `@classmethod __subtable_name__(cls, tag_values)` | 返回字符串即可 |

### 类型

| tdorm | TDengine DDL |
|---|---|
| `Timestamp` | `TIMESTAMP` |
| `TinyInt / SmallInt / Int / BigInt` | `TINYINT / SMALLINT / INT / BIGINT` |
| `UInt / UBigInt` | `UINT / UBIGINT`（3.x） |
| `Float / Double` | `FLOAT / DOUBLE` |
| `Bool` | `BOOL` |
| `Varchar(n) / NChar(n)` | `VARCHAR(n) / NCHAR(n)` |
| `Json` | `JSON`（仅标签，3.x） |

`Field(Timestamp)` 与 `Field(Timestamp())` 两种写法均可。

### 子表命名

默认 `{_tablename_}_{tag1}_{tag2}`；非法字符按 `_` + utf-8 hex 编码
（如 `node-1` → `node_2d1`），超 190 字节自动截断加哈希后缀。
标签值缺失时写入会报错。

## 引擎 API

| 方法 | 说明 |
|---|---|
| `TDEngine(host, port, user, password, database, timezone, protocol, auto_create, pool_size, pool_timeout, timeout, **kw)` | `protocol="native"`（默认，naospy）或 `"REST"`（内置传输，不依赖 taospy）；`timeout` 为 REST 请求超时（秒）；其余参数透传 |
| `create_database(database=None, keep=..., days=..., precision=...)` | 建库，支持 KEEP/DAYS/BUFFER/PRECISION 等 |
| `drop_database()` / `show_databases()` / `use(db)` | 库管理（REST 下 `use` 仅切换默认库路径） |
| `create(model, create_database=False)` | 建超级表/普通表（自动把时间戳放第一列） |
| `create_subtable(model, **tag_values)` | 显式建子表（通常不需要） |
| `drop(model)` / `truncate(model)` | 删表/清空 |
| `add(*instances, on_duplicate=None, batch_size=1000, auto_create=None, bind=False)` | 批量写入，返回行数；`bind=True` 走原生参数绑定 |
| `add_all(iterable, **kw)` | 同上 |
| `session(auto_commit=True)` | 会话：一条连接上连续执行多操作 |
| `query(model)` | 返回查询构建器 |
| `query(sql)` | 裸查询，返回 `(列名, 行)` |
| `execute(sql)` | 裸执行 DDL/DML |

写入默认按 1000 行一条语句分批；`with engine:` 仅作语义占位（close 会关闭连接池）。

## 连接池与会话

```python
engine = TDEngine(database="demo", pool_size=5)   # 多线程共享 5 条连接

# 会话：一条连接 + 事务语义
with engine.session() as s:
    s.create(DeviceReading)
    s.add(DeviceReading(ts=..., device_id="d1", ...), bind=False)
    rows = s.query(DeviceReading).filter(device_id="d1").all()
    # 正常退出自动 commit；抛异常自动 rollback
    # 也可显式 s.commit() / s.rollback() / s.begin()

engine.close()   # 关闭池
```

连接池特性：按需建连、池满阻塞（`pool_timeout` 秒后抛 `PoolTimeoutError`）、
坏连接自动丢弃并补建、幂等语句（SELECT/CREATE/DROP/TRUNCATE/DELETE 等）断链
自动重试一次（INSERT 不重试，避免重复写入）。

## REST 模式（仅开放 6041 端口时）

tdorm 内置 REST 传输层（直接调 taosAdapter 的 `/rest/sql`），**不依赖 taospy**，
适合多数工业部署"只开放 REST 端口"的场景：

```python
engine = TDEngine(host="tdengine.example.com", port=6041, user="root",
                  password="taosdata", database="your_db",
                  protocol="REST", timeout=30)
print(engine.show_databases())
stables, _ = engine.query("SHOW STABLES")
columns, rows = engine.query("DESCRIBE your_db.st_digital")
```

- 行值自动还原：字符串时间戳 → `datetime`、字符串数字 → `float/int`、`null` → `None`
- 限制：无参数绑定（`bind=True` 报错）、无事务、`pool_size` 忽略（传输层自带连接复用）
- 时间戳字面量风格 `ts_style="ms"|"iso"`：标准服务器用默认 `"ms"`（epoch 数字）；
  个别私有/改装平台只接受字符串时间戳（写入时报
  `1547: Timestamp data out of range`），此时设 `ts_style="iso"`（进程级开关）
  并以服务器 `SELECT NOW()` 为写入时间基准。
- 对真实服务器的验证请使用你自己的 TDengine 连接与库（连接参数用环境变量传入，
  示例中的主机/库名为占位）。

## 查询 API

| 方法 | 说明 |
|---|---|
| `where(*conds)` | 条件叠加（AND），支持 `&`/`|`/`~` |
| `filter(**kw)` | `device_id="d1"`、`temperature__gt=24`；操作符 `eq/ne/gt/ge/lt/le/like/notlike/in/notin/isnull/notnull/between` |
| `order_by(Field, desc=True)` / `order_by("ts DESC")` | 排序 |
| `limit(n, offset=0)` | 分页 |
| `group_by / partition_by / interval(inter, sliding, fill)` | 分组与时间窗口聚合 |
| `select(*cols)` / `with_tags()` | 自选列 / 连同标签列返回 |
| `all() / one()` | 取结果（映射为实例） |
| `scalars(col)` | 单列值列表 |
| `count() / agg("AVG(temperature)")` | 通用聚合 |
| `sum/avg/max/min/spread/stddev(col)` | 单值聚合（返回标量） |
| `twa/irate/first/last(col)` | 时间窗口聚合（配合 `interval()`） |
| `percentile(col, p)` | 分位数（标量） |
| `top(col, k) / bottom(col, k)` | 前 k 大 / 前 k 小（标量列表） |
| `diff(col)` | 逐行差值，返回 `[(ts, diff), ...]` |
| `last_row()` | 按时间戳取最后一行（支持过滤），等价 LAST_ROW |
| `delete()` | 按条件删除（必须带条件） |

列运算符：`== != > >= < <=`、`in_ / notin_ / like / notlike / between / is_null`。

```python
q = engine.query(DeviceReading)
q.filter(device_id="d1", humidity__between=(40, 60))
q.filter(device_id="d1").where((DeviceReading.temperature > 30) |
                               (DeviceReading.temperature < -5))

avg = q.filter(device_id="d1").avg(DeviceReading.temperature)      # 25.4
avg_w = q.filter(device_id="d1").interval("10m", sliding="2m", fill="PREV").twa(DeviceReading.temperature)
top3 = q.filter(device_id="d1").top(DeviceReading.temperature, 3)  # [30.2, 29.8, 29.1]
diffs = q.filter(device_id="d1").diff(DeviceReading.temperature)   # [(ts, diff), ...]
```

## 原生参数绑定（bind）

默认 INSERT 走转义后的字面量 SQL；`bind=True` 时改用 taospy 的 stmt2 原生
参数绑定（仅 native 协议，性能更好，适合长字符串 / 特殊字符数据）：

```python
engine.add(*rows, bind=True)                 # 不能与 on_duplicate 同用
```

内部按 taospy 2.8+ 的绑定协议组织参数：

```sql
INSERT INTO ? USING meter TAGS (?) VALUES (?, ?)
```

参数为"按行"列表（每行 = 子表名、标签值、列值，顺序与 `?` 一一对应），
绑定值自动还原为 Python 原生形态（时间戳 -> datetime）。每条语句绑定一个
子表的多行数据。（可用性取决于 taospy 版本的绑定协议实现。）

## Upsert（3.x）

```python
engine.add(*rows, on_duplicate="update")   # 主键冲突即覆盖（ON CONFLICT DO UPDATE）
engine.add(*rows, on_duplicate="ignore")   # 冲突跳过（ON CONFLICT DO NOTHING）
```

## 时区约定

- naive datetime：按**本机时区**解释（与 taospy 默认一致）；aware datetime：按绝对时刻。
- 读取时：时间戳统一还原为**本机时区**的 aware datetime。
- 建议业务侧统一使用 aware（如 `datetime.now().astimezone()`）避免歧义。

## 裸 SQL 与安全

所有值（列值、标签值、条件值）都经 `quote_literal` 转义后拼入 SQL，
字符串单引号翻倍，无注入面。但 `query(sql)` / `execute(sql)` / `agg(expr)` /
`order_by("原始子句")` 这类**透传入口**由调用方负责，勿拼接不可信输入。

## 测试

离线单测（模型元数据 / SQL 生成 / 类型转换 / 连接池 / REST 传输）**不随仓库发布**，
存于本地开发环境。本地运行：

```bash
$env:PYTHONPATH = "$PWD\.deps" ; python -S -m pytest -q
# 系统已装 pytest 时： python -m pytest -q
```

对真实服务器的验证请使用你自己的 TDengine 连接与库（只读操作），连接参数用
环境变量 `TDENGINE_HOST / TDENGINE_PORT / TDENGINE_USER / TDENGINE_PASSWORD / TDENGINE_DATABASE` 传入。

## 已知限制

- `bind=True` 原生绑定按 taospy 2.8+ 的 stmt2 协议实现；旧版本 taospy（1.x/早期 2.x）
  的绑定 API 不同，请升级驱动或使用默认的字面量 INSERT。
- `BETWEEN` 展开为 `>= AND <=`（TDengine 直接把列与值合并为两条件）。
- 标签值/子表命名冲突（如 `None` 与字符串 `"null"`）时会合并到同一子表，生产建议用
  `__subtable_name__` 自定义命名规则。
- 只面向 TDengine 3.x 语法；2.x 请勿使用。