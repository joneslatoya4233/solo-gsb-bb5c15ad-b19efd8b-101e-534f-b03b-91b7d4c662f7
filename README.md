# JSON Schema 反例最小化 API

基于 **Python + FastAPI** 的确定性反例（counterexample）最小化服务。
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

整个过程不使用任何随机源，相同输入必然得到相同结果（依赖版本已锁定）。

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
│   ├── main.py          # FastAPI 应用、请求/响应模型、配置
│   └── minimizer.py     # 确定性 delta debugging 最小化内核
├── tests/test_api.py    # 单元与端到端测试
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── .env.example
└── requirements.txt
```
