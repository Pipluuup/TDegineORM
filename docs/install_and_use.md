# tdorm 安装与使用指南（外部项目消费）

> Repository: https://github.com/Pipluuup/TDegineORM
> 适用版本：0.5.0（master）；目标项目建议使用 Python 3.8+ 的虚拟环境。

本文面向「把 tdorm 作为第三方包装进你的项目」的场景。仓库已公开，默认
**HTTPS 即可直接安装，无需 GitHub 账号**。

---

## 1. 安装

### 方式 A：从 Git 仓库安装（推荐）

```bash
pip install "tdorm @ git+https://github.com/Pipluuup/TDegineORM.git"
# 等效写法：
pip install git+https://github.com/Pipluuup/TDegineORM.git
```

- 拉取当前 `master` 最新代码；
- pip 读取仓库内 `pyproject.toml` 自动构建，并安装运行时依赖 `taospy>=2.8.0`；
- 之后升级到最新：`pip install -U "tdorm @ git+https://github.com/Pipluuup/TDegineORM.git"`。

### 固定版本 / 锁定 commit

用 `@` 指定 tag 或 commit（推荐锁 tag 或 commit SHA，保证可复现）：

```bash
pip install "tdorm @ git+https://github.com/Pipluuup/TDegineORM.git@<tag-or-commit>"
# 例： pip install "tdorm @ git+https://github.com/Pipluuup/TDegineORM.git@<9eacb50>"
```

> 当前尚未打 tag；发布 tag（如 `v0.5.0`）后可用 `@v0.5.0` 固定。

### 方式 B：SSH（私有仓库 / 无法走 HTTPS 时）

```bash
pip install "tdorm @ git+ssh://git@github.com/Pipluuup/TDegineORM.git"
```

需要本机已配置可访问该仓库的 SSH key。仓库当前为公开，一般用方式 A 即可。

### 方式 C：本地路径（源码 / 离线）

```bash
pip install -e /path/to/TDengineORM      # editable，改代码即时生效
```

## 2. 写入 requirements.txt

其他项目里固定依赖，格式如下（可用 `@tag-or-commit` 锁定版本）：

```text
tdorm @ git+https://github.com/Pipluuup/TDegineORM.git
```

## 3. 验证安装

```bash
python -c "import tdorm; print(tdorm.__version__)"   # 应输出 0.5.0
```

`tdorm` **零依赖导入**：未装 `taospy` 也能 import 并做模型定义 / SQL 生成，
只有真正建立 native 连接时才要求驱动。

## 4. 最小使用示例

### 4.1 不需要服务器：声明模型 + 验证 SQL

```python
from tdorm import TDEngine, Model, Field, Tag, Timestamp, Double, NChar

class DeviceReading(Model):
    __database__ = "demo"
    __tablename__ = "device_reading"

    device_id = Tag(NChar(32))
    region    = Tag(NChar(16))

    ts          = Field(Timestamp)   # 时间戳主键（第一列）
    temperature = Field(Double)
    humidity    = Field(Double)

engine = TDEngine()  # 构造不建连
q = engine.query(DeviceReading).filter(device_id="d1", temperature__gt=24).limit(10)
print(q.sql)         # 直接打印生成的 SELECT，可用于离线断言
```

### 4.2 连接服务器：建库 → 建表 → 写入 → 查询

```python
engine = TDEngine(host="localhost", port=6030, user="root",
                  password="taosdata", database="demo", protocol="native")

with engine:
    engine.create_database(keep=365, days=10)
    engine.create(DeviceReading)

    engine.add(DeviceReading(ts="2026-08-27 10:00:00", temperature=25.5,
                             humidity=60.0, device_id="d1", region="beijing"))

    rows = engine.query(DeviceReading) \
        .filter(device_id="d1", temperature__gt=24) \
        .order_by(DeviceReading.ts, desc=True) \
        .limit(100) \
        .all()
    for r in rows:
        print(r.ts, r.temperature)   # ts 已还原为 datetime
```

只开放 REST 端口（6041）的部署改一行参数即可，无需 `taospy`：

```python
engine = TDEngine(host="your-tdengine-host", port=6041, user="root",
                  password="taosdata", database="your_db", protocol="REST")
```

## 5. 常见问题

- **连接时报 `DriverError: 未安装 taospy 驱动`**：`pip install taospy` 后再连。
- **`Timestamp data out of range`（部分平台只接受字符串时间戳）**：建引擎时设
  `ts_style="iso"`，并以服务器 `SELECT NOW()` 为写入时间基准。
- **REST 下 `bind=True` 报错**：参数绑定仅支持 native 协议。
- **过滤报 `不支持的操作符 'gte'`**：tdorm 用 `ge`/`le`（如 `ts__ge`、`ts__le`）。
- **pip 无法访问 github.com（网络受限）**：用方式 C 本地路径安装。
- **`DELETE` 报必须带条件**：TDengine 不允许无 WHERE 的删除，先 `where(...)` 限定，
  需要清空用 `truncate(Model)`。

## 6. 更多 API

查询/写入/连接池/会话等完整说明见 [`docs/usage.md`](usage.md)。