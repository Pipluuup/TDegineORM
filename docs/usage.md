# tdorm 使用指南

`tdorm` 是一个轻量级 **TDengine 3.x** 时序数据库 ORM，目标是把「建库、建表、批量写入、链式查询、删除」用最少的样板代码做完，同时保持 0 外部依赖可导入（未装驱动/没有服务器也能做 SQL 级单测）。

本文档按「连接 → 建模型 → 建库建表 → 写入 → 查询 → 会话事务 → 进阶与限制」顺序介绍，各段均含可直接运行的示例代码。

---

## 目录

- [1. 安装](#1-安装)
- [2. 连接引擎](#2-连接引擎)
- [3. 定义模型](#3-定义模型)
- [4. 建库与建表](#4-建库与建表)
- [5. 写入数据](#5-写入数据)
- [6. 查询数据](#6-查询数据)
- [7. 会话与事务](#7-会话与事务)
- [8. 连接池与多线程](#8-连接池与多线程)
- [9. REST 模式的特殊说明](#9-rest-模式的特殊说明)
- [10. 类型转换与时间戳](#10-类型转换与时间戳)
- [11. 安全性：转义与重试](#11-安全性转义与重试)
- [12. 离线测试](#12-离线测试)
- [13. 常见问题](#13-常见问题)

---

## 1. 安装

唯一运行时依赖是官方驱动 `taospy`（建议 `>=2.8.0`，启用原生参数绑定）：

```bash
pip install taospy
```

> **惰性加载**：`taospy` 只在真正建立 native 连接时才被导入。因此即使没装驱动、没有服务器，也可以 `import tdorm`、定义模型、构造引擎、生成 SQL——这是离线单测的基础。

**受限网络环境**（无法直连 PyPI，如本机 TLS 外网被阻断）：用项目内置脚本经代理下载 wheel 再离线安装，详见 [README](../README.md)「安装」一节。

```bash
python tools/fetch_wheels.py                 # 把依赖 wheel 下载到 .wheels/
python -m pip install --no-index --find-links .wheels \
    --target .deps taospy iso8601==1.0.2 requests \
    typing-extensions==4.14.0 pytest ...
# 用独立依赖环境跑测试：
$env:PYTHONPATH = "$PWD\.deps" ; python -S -m pytest -q
```

---

## 2. 连接引擎

用 `TDEngine` 创建引擎。参数与 `taos.connect` 对齐：

```python
from tdorm import TDEngine

engine = TDEngine(
    host="localhost",
    port=6030,            # native 协议默认 6030；REST 默认 6041
    user="root",
    password="taosdata",
    database="demo",      # 可选，默认库
    protocol="native",    # "native"（默认）或 "REST"
    auto_create=False,    # True 时 add() 前自动 CREATE STABLE/TABLE
    pool_size=None,       # >0 时启用连接池
    pool_timeout=5.0,     # 池满时的等待超时（秒）
    timeout=30.0,         # REST 请求超时（秒）
)
```

支持的协议：

| 协议 | 端口 | 依赖 | 连接池 | 参数绑定 | 事务 |
|---|---|---|---|---|---|
| `native` | 6030 | 需 `taospy` | 可选 | ✅ `bind=True` | ✅ |
| `REST` | 6041 | 内置（仅需 `requests`） | ❌ | ❌ | ❌ |

**推荐用法**——配合 `with` 块管理生命周期：

```python
with engine:
    engine.create_database(keep=365, days=10)
    # ... 所有操作
# 退出时自动 close（关闭连接池 / REST 会话）
```

默认模式是「每操作一条短连接」：引擎本身不持有长连接，`engine.execute/query/...` 每次即时建连并释放，适合一次性脚本。需要复用连接请用[连接池](#8-连接池与多线程)或[会话](#7-会话与事务)。

其他入口：

```python
from tdorm import create_engine
engine = create_engine(host="localhost", database="demo")   # 便捷工厂
engine.use("demo")                # 切换当前库
engine.show_databases()           # -> ["demo", ...]
```

**离线创建引擎也安全**：`TDEngine()` 构造时不连接服务器，只有真正执行 SQL 才会建连。

---

## 3. 定义模型

用类声明表结构：`Field` 是普通（数据）列，`Tag` 是标签列（超级表 TAGS）。**至少需要一个 `Timestamp` 类型的列作为时间戳主键**，且必须是首列。

```python
from tdorm import Model, Field, Tag, Timestamp, Double, Bool, NChar

class DeviceReading(Model):
    __database__ = "demo"               # 库名
    __tablename__ = "device_reading"    # 超级表名（缺省 = 类名小写）

    device_id = Tag(NChar(32))          # 标签：决定子表划分
    region    = Tag(NChar(16))
 
    ts          = Field(Timestamp)      # 时间戳主键（第一列）
    temperature = Field(Double)
    humidity    = Field(Double)
    online      = Field(Bool, default=True)
```

### 元信息

| 属性 | 含义 |
|---|---|
| `__database__` | 库名。**注意：不继承**，每个具体模型需显式声明；不声明则跟随引擎默认库 |
| `__tablename__` | 表名/超级表名，缺省 = 类名小写（也**不继承**） |
| `__ttl__` | 数据的存活时长（秒/小时的整数），附加 `TTL` 子句 |
| `__abstract__` | 抽象基类：只做列复用，不校验、不建表 |
| `__subtable_name__` | 类方法 `__subtable_name__(tag_values) -> str`，自定义子表命名 |

### 列 / 标签

- `Tag(dtype)` → 标签（`TAGS (...)`）。**没有标签的模型就是普通表**（生成 `CREATE TABLE` 而非 `CREATE STABLE`）。
- `Field(dtype, default=None, nullable=True)` → 普通列；`default` 可以是常量或可调用对象（如 `default=time.time`）。
- 列名不能与 ORM 保留方法名冲突（`query/create/save/filter/...`），且必须是合法标识符。
- **继承**：子类自动继承父类的列与标签定义；子类可覆盖同名定义。但 `__database__`/`__tablename__` 需在子类重新声明。

```python
class Base(Model):
    __abstract__ = True            # 抽象：只为复用列定义
    ts = Field(Timestamp)
    device_id = Tag(NChar(32))

class TempReading(Base):
    __database__ = "iot"           # 记得重新声明
    __tablename__ = "temp_reading"
    value = Field(Double)

class HumReading(Base):
    __database__ = "iot"
    __tablename__ = "hum_reading"
    value = Field(Double, default=50.0)
```

### 多时间列

业务上允许定义多个 `Timestamp` 列：**第一个为时间戳主键**，其余时间列按普通列处理（TDengine 3.x 支持）。

### 数据类型

| Python 类型 | SQL 类型 | 说明 |
|---|---|---|
| `Timestamp` | `TIMESTAMP` | 时间戳主键；写入统一归一化为毫秒戳 |
| `TinyInt` / `SmallInt` | `TINYINT` / `SMALLINT` | |
| `Int` / `BigInt` | `INT` / `BIGINT` | |
| `UInt` / `UBigInt` | `UINT` / `UBIGINT` | |
| `Float` / `Double` | `FLOAT` / `DOUBLE` | |
| `Bool` | `BOOL` | 接受 True/False/0/1/"true" 等 |
| `Varchar(n)` | `VARCHAR(n)` | 变长字符串，按字节计数 |
| `NChar(n)` | `NCHAR(n)` | Unicode 字符串，按字符计数 |
| `Json` | `JSON` | 仅可用于标签列 |

---

## 4. 建库与建表

```python
# 建库（IF NOT EXISTS）；database 缺省用引擎默认库
engine.create_database(keep=365, days=10, precision="ms", wal_level=2)
engine.drop_database()                  # DROP DATABASE IF EXISTS

# 建表：有 Tag 生成 CREATE STABLE，无 Tag 生成 CREATE TABLE
engine.create(DeviceReading)
engine.create(DeviceReading, create_database=True)   # 先建库再建表

engine.drop(DeviceReading)              # DROP STABLE IF EXISTS
engine.truncate(DeviceReading)          # TRUNCATE TABLE
```

`create_database` 支持的选项（透传为 TDengine 数据库参数）：`keep / days / buffer / cache_model / wal_level / precision / replica / cache / groups / quorum / strict / duration / maxrows / minrows / watermark / singlestable / ttl`。传 `None` 的选项会被忽略；未列出的键按「大写、下划线转空格」原样透传。

> 实例化后若调用了 `create_database(...)`，`engine.query("SHOW DATABASES")` 即可确认建库成功。

---

## 5. 写入数据

### 基本批量写入

`add(*instances, ...)` 是核心入口：实例**自动按标签值归类到各子表**，单条 `INSERT` 语句内链式写入多个子表，天然规避子表去重问题。

```python
from datetime import datetime

now = datetime.now()

batch = [
    DeviceReading(
        ts=now, temperature=25.5, humidity=60.0,
        device_id="d1", region="beijing",
    ),
    DeviceReading(
        ts=now, temperature=24.1, humidity=62.0,
        device_id="d2", region="shanghai",
    ),
]
n = engine.add(*batch, auto_create=True)   # auto_create: 写前自动建表；返回写入行数
```

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `on_duplicate` | `None` | `"update"` / `"ignore"` → TDengine 3.x upsert（`ON CONFLICT DO UPDATE/DO NOTHING`） |
| `batch_size` | `1000` | 单条 INSERT 的行数上限（TDengine 上限 16383） |
| `auto_create` | 继承引擎设置 | `True` 时先 `CREATE ... IF NOT EXISTS`（引擎有已建表缓存，不重复 DDL） |
| `bind` | `False` | 走 taospy 原生参数绑定（仅 native；不能与 `on_duplicate` 同用） |
| `retry` | `False` | 幂等语句断线重试开关 |

```python
# upsert：主键+标签冲突时更新或忽略
engine.add(DeviceReading(ts=now, temperature=30.0, device_id="d1", region="beijing"),
           on_duplicate="update")

# 原生参数绑定：适合二进制/超长字符串，性能更高（需 native 协议）
engine.add(*batch, bind=True)

# Session 内写入，复用会话连接
with engine.session() as s:
    s.add(*batch)
```

### 子表命名

默认子表名 = `<表名>_<标签1值>_<标签2值>...`，值会被安全转义（非字母数字变成 `_`+hex，首字符是数字时补 `_`，`None`→`null`）；超长（>190 字节）自动截断并追加 MD5 后缀。

自定义命名：在模型上实现 `__subtable_name__` 类方法：

```python
class Reading(Model):
    ...
    @classmethod
    def __subtable_name__(cls, tag_values: dict) -> str:
        return "r_%s" % tag_values["device_id"].replace("-", "_")
```

也可用 `engine.create_subtable(Model, **tag_values)` 显式预建子表（通常不需要，`add()` 的 `USING TAGS` 会自动建）。

> **注意**：修改标签值集合时，会用与写入时**完全相同的算法**推导子表名，保证读写落到同一张子表。

---

## 6. 查询数据

`engine.query(...)` 有两种形态：

- 传**字符串** → 裸查询，返回 `(列名列表, 行列表)` 二元组，值保持驱动/REST 原始形态；
- 传**模型类** → 返回链式 `Query` 构建器，`.all()` 把每行映射回模型实例（自动按类型还原）。

### 链式查询基础

```python
rows = engine.query(DeviceReading) \
    .filter(device_id="d1", temperature__gt=24) \
    .order_by(DeviceReading.ts, desc=True) \
    .limit(10) \
    .all()
# rows: [DeviceReading 实例, ...]，字段已还原（ts 是 datetime）

# 查看将要执行的 SQL（调试用，不真正执行）
print(engine.query(DeviceReading).filter(device_id="d1").sql)
```

### 条件：filter / where

`filter(**kwargs)` 支持 `列名__操作符` 后缀。操作符一览：

`eq / ne / gt / ge / lt / le / like / notlike / in / notin / isnull / notnull`，另外 `between` 需要二元组：

```python
engine.query(DeviceReading).filter(
    device_id="d1",
    temperature__between=(20, 30),          # 展开为 temperature >= 20 AND <= 30
    humidity__notnull=True,                 # humidity IS NOT NULL（isnull/notnull 只看操作符出现与否）
    region__notin=["tokyo", "shanghai"],
)
```

更灵活的 `where(*conds)` 用 Field 的重载运算符组合条件（与 SQLAlchemy 习惯一致），并支持 `&`（AND）、`|`（OR）、`~`（取反）：

```python
from tdorm import Field

q = engine.query(DeviceReading)
q = q.where(
    (DeviceReading.temperature >= 30) | (DeviceReading.humidity <= 20)
).where(~(DeviceReading.online == True))
# => (temperature >= 30 OR humidity <= 20) AND NOT (online = TRUE)
# 注意：Field 本身没重载 ~，需对返回的条件对象取反
```

可用运算符：`== != < <= > >=`、`Field.in_(...)` / `.notin_(...)`、`.like()` / `.notlike()`、`.between()`、`.is_null()` / `.is_not_null()`。

### 排序 / 分页 / 选择列

```python
# 投影列：接受 Field 或列名字符串
engine.query(DeviceReading) \
    .select(DeviceReading.ts, DeviceReading.temperature) \
    .order_by("ts", desc=True) \
    .limit(10, offset=20)      # LIMIT 10 OFFSET 20
# order_by 也接受含空格的原始子句，如 "ts DESC"
```

### 聚合与时间窗口

标量聚合（返回单值，自动按列类型还原）：

```python
avg_t = engine.query(DeviceReading).filter(device_id="d1").avg(DeviceReading.temperature)
max_h = engine.query(DeviceReading).max(DeviceReading.humidity)
```

时间窗口聚合（`interval`/`sliding`/`fill` 原样透传给 `INTERVAL(...) SLIDING(...) FILL(...)`）：

```python
engine.query(DeviceReading) \
    .interval("1h", sliding="30m", fill="PREV") \
    .avg(DeviceReading.temperature)
```

可用聚合助手：

| 方法 | 语义 |
|---|---|
| `sum / avg / max / min / spread / stddev` | 常规聚合 |
| `twa / irate` | 时间加权平均 / 瞬时增长率（配合 `interval`）|
| `first / last` | 窗口内首 / 末值 |
| `percentile(col, p)` | 分位数 |
| `top(col, k=10)` / `bottom(col, k=10)` | 前/后 k 个最大值，返回值列表 |
| `diff(col)` | 逐行差值，返回 `[(ts, diff), ...]` 元组列表 |
| `agg(expr)` | 任意原始聚合表达式，如 `agg("COUNT(*)")`；返回结果集第一行第一列 |
| `count()` | `COUNT(*)` 行数 |
| `last_row()` | 取时间戳最大的一行（支持带过滤），返回实例或 None |
| `one()` | 返回第一条或 None |
| `scalars(col=None)` | 单列值列表（需恰好一列） |

### 按条件删除

```python
# TDengine 要求 DELETE 必须带条件（通常按时间范围）
engine.query(DeviceReading) \
    .where((DeviceReading.ts >= t0) & (DeviceReading.ts < t1)) \
    .delete()

# 快捷写法：engine.delete(Model, *conds, **kwargs)
engine.delete(DeviceReading, device_id="d1",
              ts__ge="2026-08-01 00:00:00", ts__lt="2026-09-01 00:00:00")
```

裸 SQL 查询作为逃生舱：

```python
columns, rows = engine.query("SELECT COUNT(*) FROM device_reading")
engine.execute("DROP TABLE IF EXISTS tmp_t")
```

---

## 7. 会话与事务

`engine.session()` 在**一条共享连接**上连续执行多个操作（native 且有连接池时复用池连接），退出自动 commit，出现异常自动 rollback：

```python
with engine.session() as s:
    s.create_database("iot", keep=30)
    s.create(DeviceReading, create_database=True)
    s.add(batch)                       # 同一连接
    rows = s.query(DeviceReading).filter(device_id="d1").all()
    s.execute("USE iot")
# 退出时自动 COMMIT
```

事务控制（native 协议）：

```python
with engine.session() as s:
    s.begin()
    try:
        s.add(batch)
        s.commit()
    except Exception:
        s.rollback()
        raise
```

> REST 模式 `engine.session()` 返回 `RestSession`：**无事务语义**（`begin/commit/rollback` 为空操作），仅复用同一 HTTP 连接，`use()` 只切换后续请求的默认库。

---

## 8. 连接池与多线程

`pool_size=N` 开启连接池，适合多线程及 `session()` 复用：

```python
engine = TDEngine(pool_size=8, pool_timeout=5.0)

with engine.session() as s:         # 取的连接用后归还（不是关闭）
    s.query(DeviceReading).all()

engine.pool.idle     # 空闲连接数
engine.pool.created  # 已创建连接数
```

- 线程安全：`acquire/release/discard/close` 均可多线程调用。
- **坏连接自愈**：执行抛异常时连接被 `discard`（丢弃而非归还），容量自动补足。
- 池满：`acquire` 阻塞等待归还，超时报 `PoolTimeoutError`。
- REST 模式无连接池（每次操作一个 HTTP 请求）。

---

## 9. REST 模式的特殊说明

`protocol="REST"` 走内置传输层（taosAdapter `/rest/sql`），**不需要 `taospy`**，适合仅开放 6041 端口 / 无法装 C 驱动的部署：

```python
engine = TDEngine(
    host="tdengine.example.com", port=6041,
    user="root", password="taosdata",
    database="your_db", protocol="REST",
    timeout=30,
    ts_style="iso",   # 平台只接受字符串时间戳时用 "iso"
)
rows = engine.query(DeviceReading).filter(device_id="d1").limit(5).all()
```

限制与坑：

1. **不支持 `bind=True`** 参数绑定与连接池。
2. **无事务语义**；`session()` 仅共享 HTTP 会话。
3. **`ts_style`**：写入侧 SQL 字面量的时间戳风格。默认 `"ms"`（epoch 毫秒数，标准 TDengine 均支持）；`"iso"` 输出 `'2026-08-01T00:00:00.000Z'` 字符串，适配只收字符串时间戳的平台。该开关是**模块级全局**（`Timestamp.sql_style`），同一进程混用不同风格的引擎需要自行协调。
4. 返回的时间戳/数值在裸查询中是 JSON 原生形态，链式查询会按列类型自动还原成 Python 对象（`ts` → datetime）。

---

## 10. 类型转换与时间戳

每个数据类型实现两个方向：

- `coerce(value)`：**写入前**把 Python 值归一化为 TDengine 规范值（如 `datetime` → 毫秒时间戳整数）；
- `from_db(value)`：**读取后**把驱动/REST 原始值还原为 Python 值（如字符串时间戳 → `datetime`）。

时间戳 `Timestamp` 的 `coerce` 接受：

```python
ts = Field(Timestamp)
# 写入时可传：
#   - 整数毫秒（epoch ms）
#   - datetime / date 对象（naive 按本机时区解释）
#   - 字符串："2026-08-26 10:00:00"、"2026-08-26T10:00:00.000+08:00"、"2026-08-26" 等
#   - 纯数字字符串按毫秒处理（REST 可能返回这种）
```

读取后 `Model.xxx.ts` 得到 `datetime`（aware，本机时区）。

---

## 11. 安全性：转义与重试

- **防注入**：所有字符串值经 `quote_literal` 转义（单引号翻倍）后才拼入 SQL；表名/列名经 `quote_ident`（命中 TDengine 关键字时用反引号包裹）。条件操作符白名单化，非法操作符直接 `ValidationError`。
- **重试只针对幂等语句**：断线时只对 `SELECT/SHOW/DESCRIBE/CREATE/DROP/TRUNCATE/USE/DELETE/ALTER` 重试一次；**INSERT 绝不重试**，避免重复写入造成脏数据。

---

## 12. 离线测试

得益于惰性导入与「纯 SQL 生成」架构，不装驱动、不连服务器就能做 SQL 级断言：

```python
from tdorm import TDEngine, ...

class MyModel(Model):
    __database__ = "iot"
    __tablename__ = "foo"
    device_id = Tag(NChar(32))
    ts = Field(Timestamp)
    value = Field(Double)

engine = TDEngine()          # 构造时不连接
q = engine.query(MyModel).filter(device_id="d1", value__gt=10).limit(3)
assert q.sql == (
    "SELECT ts, value FROM foo\n"
    "WHERE (device_id = 'd1') AND (value > 10.0)\n"   # Double coerce 10 -> 10.0
    "LIMIT 3"
)
```

项目自带测试（全部离线，不需要服务器）：

```bash
python -S -m pytest -q   # SQL 生成 / 模型 / 类型转换（本地开发环境）
```

对真实服务器的验证请使用你自己的 TDengine 连接与库（只读操作），连接参数可用环境变量
`TDENGINE_HOST / TDENGINE_PORT / TDENGINE_USER / TDENGINE_PASSWORD / TDENGINE_DATABASE` 传入。

---

## 13. 常见问题

**Q：能同时定义多个 Timestamp 列吗？**
可以，但第一个必须是时间戳主键（首列）；其余时间列按普通列处理。

**Q：`add()` 怎么保证子表不重复创建？**
`add()` 用 `CREATE TABLE IF NOT EXISTS ... USING stable TAGS(...)` 写子表；引擎还维护了 (库, 表) 建表缓存，`auto_create=True` 时不会重复执行 DDL。

**Q：修改标签的位数/长度会影响子表名吗？**
默认子表名由标签值推导。只要标签值不变，子表名不变；改表名算法（自定义 `__subtable_name__`）需谨慎，老数据会落在旧子表。

**Q：为什么 delete 强制要求条件？**
TDengine 的 `DELETE` 不允许无 `WHERE` 的全表删除；用 `truncate(Model)` 做清空。

**Q：裸查询和链式查询返回值区别？**
`engine.query("SQL")` 返回 `(列名, 行)` 原始形态；`engine.query(Model).all()` 返回模型实例并还原类型。需要原始聚合值时用裸查询，需要对象化访问用链式查询。

**Q：连接池在池满时为什么阻塞？**
池容量固定，`acquire` 在 `pool_timeout` 内等待归还，超时抛 `PoolTimeoutError`；多线程并发写建议把 `pool_size` 设成并发数。

---
