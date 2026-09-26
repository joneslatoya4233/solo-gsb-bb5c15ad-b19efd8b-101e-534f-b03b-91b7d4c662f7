"""POST /mutations 的端到端与单元测试。运行：python -m pytest -q"""

from fastapi.testclient import TestClient

from app.main import app
from app.minimizer import build_validator

client = TestClient(app)


def gen(schema, instance, seed=None):
    payload = {"schema": schema, "instance": instance}
    if seed is not None:
        payload["seed"] = seed
    return client.post("/mutations", json=payload)


def resolve(sample, pointer):
    """按 JSON Pointer（RFC 6901）在样例中定位节点。"""
    node = sample
    if pointer == "":
        return node
    for part in pointer.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def by_keyword(mutations, keyword):
    return [m for m in mutations if m["keyword"] == keyword]


# ---- HTTP 层 ---------------------------------------------------------------


def test_invalid_schema_returns_422():
    resp = gen({"type": "not-a-type"}, 1)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_schema"


def test_invalid_sample_returns_422():
    resp = gen({"type": "integer"}, "not-an-int")
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "instance_invalid"
    assert body["first_error"]["keyword"] == "type"


def test_false_schema_sample_never_valid_returns_422():
    resp = gen(False, {"a": 1})
    assert resp.status_code == 422
    assert resp.json()["error"] == "instance_invalid"


def test_true_schema_generates_nothing():
    resp = gen(True, {"a": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mutations"] == []
    assert body["skipped"] == []


def test_bad_seed_returns_422():
    assert gen({}, 1, seed=-1).status_code == 422
    assert gen({}, 1, seed=2**31).status_code == 422


def test_missing_fields_returns_422():
    assert client.post("/mutations", json={}).status_code == 422


# ---- 各关键字的最小变异 -----------------------------------------------------


def test_required_mutation_removes_one_property():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {"id": {"type": "integer"}},
    }
    body = gen(schema, {"id": 1, "name": "abc"}).json()
    (mut,) = by_keyword(body["mutations"], "required")
    assert mut["pointer"] == ""
    assert mut["op"] == "remove"
    assert mut["instance"] == {"name": "abc"}


def test_type_mutation_replaces_with_null():
    body = gen({"type": "integer"}, 5).json()
    (mut,) = by_keyword(body["mutations"], "type")
    assert mut["pointer"] == ""
    assert mut["instance"] is None


def test_type_covering_all_types_is_skipped():
    schema = {"type": ["null", "boolean", "integer", "number", "string", "array", "object"]}
    body = gen(schema, "x").json()
    assert body["mutations"] == []
    assert by_keyword(body["skipped"], "type")


def test_min_items_truncates_to_limit_minus_one():
    body = gen({"type": "array", "minItems": 2}, [10, 20, 30]).json()
    (mut,) = by_keyword(body["mutations"], "minItems")
    assert mut["instance"] == [10]


def test_min_items_zero_is_skipped():
    body = gen({"minItems": 0}, []).json()
    assert body["mutations"] == []
    (skip,) = by_keyword(body["skipped"], "minItems")
    assert "minItems=0" in skip["reason"]


def test_max_items_extends_by_one_copy():
    schema = {"type": "array", "maxItems": 2, "items": {"type": "integer"}}
    body = gen(schema, [1, 2]).json()
    (mut,) = by_keyword(body["mutations"], "maxItems")
    assert mut["instance"] == [1, 2, 2]


def test_min_length_truncates_string():
    body = gen({"type": "string", "minLength": 3}, "abcdef").json()
    (mut,) = by_keyword(body["mutations"], "minLength")
    assert mut["instance"] == "ab"


def test_min_length_zero_is_skipped():
    body = gen({"minLength": 0}, "abc").json()
    assert by_keyword(body["skipped"], "minLength")


def test_max_length_extends_string():
    body = gen({"maxLength": 2}, "ab").json()
    (mut,) = by_keyword(body["mutations"], "maxLength")
    assert mut["instance"] == "abx"
    assert len(mut["instance"]) == 3


def test_numeric_bounds():
    body = gen({"minimum": 5}, 10).json()
    (mut,) = by_keyword(body["mutations"], "minimum")
    assert mut["instance"] == 4

    body = gen({"maximum": 5}, 1).json()
    (mut,) = by_keyword(body["mutations"], "maximum")
    assert mut["instance"] == 6


def test_exclusive_bounds_modern_draft():
    body = gen({"exclusiveMinimum": 5}, 10).json()
    (mut,) = by_keyword(body["mutations"], "exclusiveMinimum")
    assert mut["instance"] == 5

    body = gen({"exclusiveMaximum": 5}, 1).json()
    (mut,) = by_keyword(body["mutations"], "exclusiveMaximum")
    assert mut["instance"] == 5


def test_exclusive_minimum_draft4_reports_minimum_keyword():
    schema = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "minimum": 5,
        "exclusiveMinimum": True,
    }
    body = gen(schema, 10).json()
    (mut,) = by_keyword(body["mutations"], "minimum")
    assert mut["instance"] == 5


def test_enum_mutation_picks_value_outside_enum():
    body = gen({"enum": ["a", "b"]}, "a").json()
    (mut,) = by_keyword(body["mutations"], "enum")
    assert mut["instance"] is None


def test_const_mutation():
    body = gen({"const": "fixed"}, "fixed").json()
    (mut,) = by_keyword(body["mutations"], "const")
    assert mut["instance"] is None


# ---- 遍历、指针与嵌套 -------------------------------------------------------


def test_nested_pointers_and_properties_walk():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "minItems": 2},
            "name": {"type": "string", "minLength": 2},
        },
    }
    sample = {"tags": [1, 2, 3], "name": "abcd"}
    body = gen(schema, sample).json()

    (tags_mut,) = by_keyword(body["mutations"], "minItems")
    assert tags_mut["pointer"] == "/tags"
    assert tags_mut["instance"] == {"tags": [1], "name": "abcd"}

    (name_mut,) = by_keyword(body["mutations"], "minLength")
    assert name_mut["pointer"] == "/name"
    assert name_mut["instance"] == {"tags": [1, 2, 3], "name": "a"}


def test_items_walk_generates_per_element_mutations():
    body = gen({"items": {"type": "integer"}}, [1, 2, 3]).json()
    muts = by_keyword(body["mutations"], "type")
    assert [m["pointer"] for m in muts] == ["/0", "/1", "/2"]
    assert muts[0]["instance"] == [None, 2, 3]


def test_allof_constraints_are_covered():
    body = gen({"allOf": [{"minLength": 3}]}, "abcdef").json()
    (mut,) = by_keyword(body["mutations"], "minLength")
    assert mut["instance"] == "ab"


def test_every_pointer_resolves_in_original_sample():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "tags": {"type": "array", "minItems": 1},
        },
    }
    sample = {"id": 5, "tags": ["a"], "extra": True}
    body = gen(schema, sample).json()
    assert body["mutations"], "应至少生成一个变异"
    for mut in body["mutations"]:
        resolve(sample, mut["pointer"])  # 不抛异常即可解析


# ---- 去重 / skipped / 实测保证 ----------------------------------------------


def test_identical_instances_are_deduplicated():
    # type 与 enum 在同一路径都会把实例变异为 null，只保留先处理的 type。
    body = gen({"type": "integer", "enum": [1, 2, 3]}, 1).json()
    assert len(body["mutations"]) == 1
    assert body["mutations"][0]["keyword"] == "type"
    assert body["summary"]["deduplicated"] == 1


def test_shadowed_keyword_goes_to_skipped():
    # minProperties 书写在前：删除 a 后首错是 minProperties 而非 required。
    schema = {"minProperties": 1, "required": ["a"]}
    body = gen(schema, {"a": 1}).json()
    assert body["mutations"] == []
    (skip,) = by_keyword(body["skipped"], "required")
    assert "minProperties" in skip["reason"]


def test_vacuous_keyword_goes_to_skipped():
    # 样例是数值，minLength 在此空真；不做跨类型合成。
    body = gen({"minLength": 3}, 42).json()
    assert body["mutations"] == []
    (skip,) = by_keyword(body["skipped"], "minLength")
    assert "不是字符串" in skip["reason"]


def test_every_mutation_actually_triggers_its_keyword():
    schema = {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "integer", "minimum": 1, "maximum": 100},
            "name": {"type": "string", "minLength": 2, "maxLength": 5},
            "tags": {"type": "array", "minItems": 1, "maxItems": 3},
            "role": {"enum": ["admin", "user"]},
            "level": {"exclusiveMinimum": 0, "exclusiveMaximum": 10},
        },
    }
    sample = {"id": 5, "name": "abc", "tags": ["x"], "role": "user", "level": 5}
    body = gen(schema, sample).json()
    assert body["skipped"] == []
    covered = {m["keyword"] for m in body["mutations"]}
    assert {
        "required",
        "type",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "enum",
        "exclusiveMinimum",
        "exclusiveMaximum",
    } <= covered

    validator = build_validator(schema)
    for mut in body["mutations"]:
        errors = list(validator.iter_errors(mut["instance"]))
        assert errors, f"变异实例必须非法：{mut}"
        assert errors[0].validator == mut["keyword"]


# ---- seed 与确定性 -----------------------------------------------------------


def test_seed_is_base_plus_index_and_reproducible():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}}}
    sample = {"a": 1, "b": "x"}

    first = gen(schema, sample, seed=100).json()
    second = gen(schema, sample, seed=100).json()
    assert first == second  # 相同请求重放，响应逐字节相同
    assert first["seed"] == 100
    assert [m["seed"] for m in first["mutations"]] == [100 + i for i in range(len(first["mutations"]))]

    shifted = gen(schema, sample, seed=200).json()
    assert [m["instance"] for m in shifted["mutations"]] == [m["instance"] for m in first["mutations"]]
    assert [m["seed"] for m in shifted["mutations"]] == [m["seed"] + 100 for m in first["mutations"]]


def test_default_seed_is_zero():
    body = gen({"type": "integer"}, 5).json()
    assert body["seed"] == 0
    assert [m["seed"] for m in body["mutations"]] == list(range(len(body["mutations"])))
