# JSON Schema 反例最小化 API

基于 **Python + FastAPI** 的确定性反例（counterexample）工具服务，提供两个接口：

- **`POST /minimize`**：给定 JSON Schema 和一个**未通过校验**的实例，做确定性
  delta debugging 最小化；
- **`POST /mutations`**：给定 JSON Schema 和一个**合法**样例，确定性生成覆盖
  各约束关键字的**最小变异实例**（每个变异都是只改动一个位置的真实反例）。

## `POST /minimize`（反例最小化）

给定一份 JSON Schema 和一个**未通过校验**的 JSON 实例，服务会：

1. **锁定首个校验错误的关键字**（keyword，如 `required` / `type` / `minItems`）；
2. 用**确定性 delta debugging** 反复尝试：
   - 坍缩：同类型空值（`{}` / `[]` / `""`）与 `null`；
   - 简化标量：`false`、`0`、`±1`、按 2 次幂减半、字符串截到单字符；
   - 删除对象字段块（经典 Zeller ddmin，作用于键的有序序列）；
   - 删除数组连续区间块、删除字符串字符块（同一个 ddmin 内核）；
   - 递归处理剩余子节点，外层迭代到整棵树到达不动点；
3. **只保留仍触发同一关键字的候选**；如果候选变成合法实例、或首个错误换成了
   别的关键字，则丢弃该候选；
4. 返回最小实例、完整评估轨迹，以及是否因评估预算提前停止。

## `POST /mutations`（最小变异生成）

给定一份 JSON Schema 和一个**通过校验**的合法样例，服务会：

1. 随 schema 递归遍历样例（`properties` / `items` / `prefixItems` /
   `additionalProperties` / `allOf`），定位样例实际触及的约束；
2. 对每个约束生成**单点最小变异**（只改动样例的一个位置）：
   - `required`：逐个删除必填属性；
   - `type`：替换为允许类型之外的取值（候选阶梯 `null → "x" → [] → {} → true → 0`）；
   - `enum` / `const`：替换为枚举/常量之外的取值；
   - `minimum` / `maximum`：取刚好越界的值（整数 `bound ∓ 1`，浮点 `nextafter`）；
   - `exclusiveMinimum` / `exclusiveMaximum`：取边界值本身（draft 6+ 数值形式；
     draft 3/4 的布尔修饰符语义并入 `minimum` / `maximum`）；
   - `minLength` / `minItems`：截断到 `limit - 1`；
   - `maxLength` / `maxItems`：恰好扩充到 `limit + 1`；
3. **每个变异都用校验器实测**：只有首个校验错误的关键字与预期一致才返回，
   否则列入 `skipped`（例如被书写顺序更靠前的其他关键字遮蔽）；
4. **同一变异实例去重**（规范化 JSON 相同只保留首次出现）；
5. 每个变异返回 **JSON Pointer**（改动位置，RFC 6901）、**预期触发关键字**
   和**可复现 seed**（第 i 个变异的 seed = 请求 `seed` + i）。

两个接口都**不使用任何随机源**，相同输入必然得到相同输出（依赖版本已锁定）。

---

## 一条命令启动

需要 Docker（含 Compose 插件）：

```bash
docker compose up --build -d
```

启动后服务监听：

- **默认访问地址**：`http://localhost:8000`
- 健康检查：`GET http://localhost:8000/healthz`
- 交互式 API 文档（Swagger UI）：`http://localhost:8000/docs`

停止：`docker compose down`；查看日志：`docker compose logs -f`。

---

## 必要配置

所有配置均通过环境变量提供，均有默认值，**零配置即可启动**：

| 环境变量                   | 默认值    | 说明                                                       |
| -------------------------- | --------- | ---------------------------------------------------------- |
| `HOST`                     | `0.0.0.0` | 容器内监听地址（一般无需修改）                             |
| `PORT`                     | `8000`    | 容器内监听端口；宿主机映射端口由 Compose 的 `PORT` 控制    |
| `DEFAULT_MAX_EVALUATIONS`  | `1000`    | 请求未显式指定 `max_evaluations` 时使用的默认评估次数      |
| `HARD_MAX_EVALUATIONS`     | `100000`  | 单次请求允许的评估次数硬上限（超过返回 422）               |

可复制 `.env.example` 为 `.env` 后按需修改，例如改用宿主机 9000 端口：

```bash
cp .env.example .env
echo "PORT=9000" >> .env
docker compose up --build -d   # 然后访问 http://localhost:9000
```

---

## API

### `POST /minimize`

请求体：

| 字段              | 类型             | 必填 | 说明                                                                 |
| ----------------- | ---------------- | ---- | -------------------------------------------------------------------- |
| `schema`          | object / boolean | 是   | JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）    |
| `instance`        | 任意 JSON        | 是   | 未通过该校验的 JSON 实例                                             |
| `max_evaluations` | integer          | 否   | 最大校验评估次数（**含对原实例的首次校验**），默认/上限见上表，≥ 1   |

`schema` 中的 `$schema` 用于自动识别 draft；缺省按 Draft 2020-12 处理。

#### 响应 `200`

| 字段                 | 说明                                                                 |
| -------------------- | -------------------------------------------------------------------- |
| `minimal_instance`   | 最小化后的实例                                                       |
| `original_instance`  | 原始实例（原样回显）                                                 |
| `target_keyword`     | 锁定的首个错误关键字；布尔 `false` schema 下为 `null`                |
| `evaluations`        | 实际消耗的评估次数                                                   |
| `stopped_by_budget`  | `true` 表示在到达不动点前因预算耗尽提前停止                          |
| `trace[]`            | 每一次评估的轨迹（见下）                                             |

`trace[]` 条目字段：

| 字段                | 说明                                                                 |
| ------------------- | -------------------------------------------------------------------- |
| `index`             | 评估序号，从 1 开始（第 1 条恒为 `lock`）                            |
| `op`                | 操作类型：`lock` / `collapse` / `scalar` / `ddmin-object` / `ddmin-array` |
| `path`              | 被修改节点的 JSON Pointer（根为 `""`）                               |
| `detail`            | 人类可读的操作描述（删了哪些字段/区间、标量如何替换等）              |
| `kept`              | 该候选是否仍触发锁定关键字并被接受为新的当前实例                     |
| `triggered_keyword` | 候选触发的首个错误关键字；候选合法时为 `null`                        |
| `error_path`        | 错误对象所在位置的 JSON Pointer                                      |
| `message`           | 校验器给出的错误信息                                                 |

#### 错误响应（均为 `422`）

- 原实例**已经合法**：

  ```json
  { "error": "instance_valid", "message": "提供的实例已通过 schema 校验，无需最小化；本接口只接受反例。" }
  ```

- schema 自身不合法：`{ "error": "invalid_schema", "message": "...", "path": [...] }`
- 请求体缺少必填字段、`max_evaluations` 越界等：FastAPI/Pydantic 标准 422 结构。

---

### `POST /mutations`

请求体：

| 字段       | 类型             | 必填 | 说明                                                                  |
| ---------- | ---------------- | ---- | --------------------------------------------------------------------- |
| `schema`   | object / boolean | 是   | JSON Schema（支持 Draft 3/4/6/7/2019-09/2020-12，含布尔 schema）     |
| `instance` | 任意 JSON        | 是   | **通过该校验**的合法样例（变异基线）                                  |
| `seed`     | integer          | 否   | seed 基数，默认 `0`，范围 `0 .. 2^31-1`；第 i 个变异的 seed = seed + i |

#### 响应 `200`

| 字段        | 说明                                                                 |
| ----------- | -------------------------------------------------------------------- |
| `seed`      | 本次请求使用的 seed 基数（原样回显）                                 |
| `mutations` | 通过实测的最小变异列表（生成顺序固定，见下）                         |
| `skipped`   | 无法构造（或被遮蔽）的关键字及原因                                   |
| `summary`   | 计数：`mutations` / `skipped` / `deduplicated`（被去重丢弃的数量）   |

`mutations[]` 条目字段：

| 字段       | 说明                                                                       |
| ---------- | -------------------------------------------------------------------------- |
| `index`    | 变异序号，从 0 开始                                                        |
| `seed`     | 可复现 seed：`请求 seed + index`；相同请求重放必然得到逐字节相同的响应     |
| `pointer`  | 改动位置的 JSON Pointer（根为 `""`）；`required` 指向被删属性所在的对象    |
| `keyword`  | 预期触发（且实测确认）的首错关键字                                         |
| `op`       | 操作类型：`remove` / `replace` / `truncate` / `extend` / `nudge`           |
| `detail`   | 人类可读的操作描述                                                         |
| `instance` | 变异后的完整实例（保证非法，且首错关键字为 `keyword`）                     |

`skipped[]` 条目字段：`pointer` / `keyword` / `reason`。典型原因：

- 空真约束：样例该位置类型不适用（如数值节点上的 `minLength`），不做跨类型合成；
- 约束不可违反：`minLength=0` / `minItems=0` / `required=[]` / `type` 覆盖全部类型；
- 关键字被遮蔽：变异后首错是书写顺序更靠前的其他关键字（如 `minProperties` 先于 `required`）。

不做变异推导的构造：`$ref`、`patternProperties`、`anyOf` / `oneOf` / `not`、
旧版 `additionalItems`（保持"单点最小变异"语义，避免产生误导性反例）。

#### 错误响应（均为 `422`）

- 样例**未通过校验**（本接口需要合法基线，非法实例请改用 `/minimize`）：

  ```json
  {
    "error": "instance_invalid",
    "message": "样例未通过 schema 校验；本接口需要合法样例作为变异基线，非法实例请使用 /minimize。",
    "first_error": { "keyword": "type", "path": [], "message": "'oops' is not of type 'integer'" }
  }
  ```

- schema 自身不合法：`{ "error": "invalid_schema", "message": "...", "path": [...] }`
- 请求体缺少必填字段、`seed` 越界等：FastAPI/Pydantic 标准 422 结构。

---

## 使用示例

```bash
curl -s -X POST http://localhost:8000/minimize \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {"type":"object","required":["id"],"properties":{"id":{"type":"integer"}}},
    "instance": {"tags": ["aaaaaaaaaa", "bbbbbbbbbb"], "extra": {"deep": [1,2,3,4]}, "name": "hello-world"},
    "max_evaluations": 500
  }'
```

响应（节选）：空对象仍缺 `id`，故最小反例为 `{}`，全程仅 3 次评估：

```json
{
  "minimal_instance": {},
  "target_keyword": "required",
  "evaluations": 3,
  "stopped_by_budget": false,
  "trace": [
    {"index": 1, "op": "lock", "detail": "validate original instance", "kept": false, "triggered_keyword": "required"},
    {"index": 2, "op": "collapse", "detail": "collapse object(3) -> {}", "kept": true,  "triggered_keyword": "required"},
    {"index": 3, "op": "collapse", "detail": "collapse object(0) -> null", "kept": false, "triggered_keyword": "type"}
  ]
}
```

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/minimize",
    json={
        "schema": {"type": "array", "minItems": 3},
        "instance": [10, 20],          # 非法：只有 2 个元素
        "max_evaluations": 1000,
    },
)
print(r.status_code, r.json()["minimal_instance"])  # 200 []
```

### `POST /mutations` 示例

```bash
curl -s -X POST http://localhost:8000/mutations \
  -H 'Content-Type: application/json' \
  -d '{
    "schema": {
      "type": "object",
      "required": ["id"],
      "properties": {
        "id":   {"type": "integer", "minimum": 1},
        "name": {"type": "string", "minLength": 2},
        "tags": {"type": "array", "minItems": 1},
        "role": {"enum": ["admin", "user"]}
      }
    },
    "instance": {"id": 5, "name": "abc", "tags": ["x"], "role": "user"},
    "seed": 7
  }'
```

响应（节选）：9 个变异，每个都标注了改动位置、预期关键字与可复现 seed——

```json
{
  "seed": 7,
  "mutations": [
    {"index": 0, "seed": 7,  "pointer": "",      "keyword": "required",  "op": "remove",
     "detail": "删除必填属性 'id'",
     "instance": {"name": "abc", "tags": ["x"], "role": "user"}},
    {"index": 3, "seed": 10, "pointer": "/id",   "keyword": "minimum",   "op": "nudge",
     "detail": "取 0（< minimum 1）",
     "instance": {"id": 0, "name": "abc", "tags": ["x"], "role": "user"}},
    {"index": 5, "seed": 12, "pointer": "/name", "keyword": "minLength", "op": "truncate",
     "detail": "截断到长度 1（< minLength 2）",
     "instance": {"id": 5, "name": "a", "tags": ["x"], "role": "user"}},
    {"index": 7, "seed": 14, "pointer": "/tags", "keyword": "minItems",  "op": "truncate",
     "detail": "截断到 0 个元素（< minItems 1）",
     "instance": {"id": 5, "name": "abc", "tags": [], "role": "user"}}
  ],
  "skipped": [],
  "summary": {"mutations": 9, "skipped": 0, "deduplicated": 0}
}
```

Python 调用：

```python
import httpx

r = httpx.post(
    "http://localhost:8000/mutations",
    json={
        "schema": {"type": "string", "minLength": 3},
        "instance": "abcdef",          # 合法样例
    },
)
for m in r.json()["mutations"]:
    print(m["seed"], m["pointer"] or "/", m["keyword"], repr(m["instance"]))
# 0 / type None
# 1 / minLength 'ab'
```

---

## 算法说明（确定性保证）

1. **锁定**：首次校验取 `jsonschema` 校验器产出的第一个错误，记录其
   `validator`（即关键字）作为唯一"有趣性"判据。
2. **遍历策略**：对每个节点依次执行「坍缩 → 标量阶梯 → 本层 ddmin → 递归子节点」。
   任何被接受的修改都严格减小实例的良基复杂度（容器元素数 / 标量量级），
   因此外层不动点迭代必然终止，不会出现 `1 ↔ -1` 之类的振荡。
3. **ddmin**：对象按键的插入序、数组/字符串按位置切成尽量等长的连续块，
   先逐块删除，再尝试只保留单个块，粒度 `n` 按经典算法
   `2 → … → 元素数` 自适应增减。
4. **预算**：每执行一次完整 JSON 校验计 1 次评估（含锁定）。开始第
   `max_evaluations + 1` 次评估前立即停止并把 `stopped_by_budget` 置为 `true`，
   已接受的缩减全部保留。

注意：这里的"最小"是 **1-最小（1-minimal）** 意义上的最小——算法保证在其
操作算子下再删任何一个单元都会丢失目标关键字，但不保证是全局基数最小。

### 变异生成的确定性（`/mutations`）

1. **固定顺序**：关键字按 `required → type → enum → const → minimum → maximum →
   exclusiveMinimum → exclusiveMaximum → minLength → maxLength → minItems →
   maxItems` 的固定顺序处理，与 schema 书写顺序无关；子节点按
   `properties → additionalProperties → items/prefixItems → allOf` 递归。
2. **最小性**：每个变异与样例的编辑距离为 1（删一个属性 / 改一个节点的值 /
   截断或扩充到刚好越界），不存在更小的同关键字反例变异。
3. **实测保证**：每个变异都经校验器完整校验，首错关键字与预期一致才返回；
   同一实例按规范化 JSON 去重，只保留首次出现。
4. **seed**：生成全程无随机源，seed 是可复现标识而非随机输入——
   相同 `schema + instance + seed` 重放必然得到逐字节相同的响应。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

运行测试：

```bash
pip install pytest httpx
python -m pytest -q
```

---

## 项目结构

```
.
├── app/
│   ├── main.py              # FastAPI 应用、请求/响应模型、配置
│   ├── minimizer.py         # 确定性 delta debugging 最小化内核
│   └── mutator.py           # 确定性最小变异生成内核
├── tests/
│   ├── test_api.py          # /minimize 单元与端到端测试
│   └── test_mutations.py    # /mutations 单元与端到端测试
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── requirements.txt
```
