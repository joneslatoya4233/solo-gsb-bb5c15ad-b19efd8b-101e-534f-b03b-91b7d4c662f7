"""确定性最小变异生成器（POST /mutations 的内核）。

给定 JSON Schema 与一个 *合法* 样例，随 schema 递归遍历样例，对样例
实际触及的约束关键字生成 **单点最小变异**：每个变异只改动样例的一个
位置，且保证改动后的实例首个校验错误的关键字与预期一致。

覆盖关键字：
``required`` / ``type`` / ``enum`` / ``const`` /
``minimum`` / ``maximum`` / ``exclusiveMinimum`` / ``exclusiveMaximum`` /
``minLength`` / ``maxLength`` / ``minItems`` / ``maxItems``。

确定性保证：
- 关键字按固定顺序处理，与子 schema 在 schema 对象中的书写顺序无关；
- 替换候选取自固定阶梯（null → 字符串 → 数组 → 对象 → 布尔 → 数值）；
- 每个变异都用校验器实测，首错关键字与预期一致才会返回，否则记入
  ``skipped``（例如被书写顺序更靠前的其他关键字遮蔽）；
- 同一变异实例（规范化 JSON 相同）只保留首次出现；
- 不使用任何随机源：第 i 个变异的 ``seed`` = 请求 ``seed`` + i，
  相同请求重放必然得到逐字节相同的响应。

不在推导范围内（保持单点最小语义）：``$ref``、``patternProperties``、
``anyOf`` / ``oneOf`` / ``not``、旧版 ``additionalItems``；对样例节点
类型不适用的空真约束（如对数值节点上的 ``minLength``）不做跨类型合成，
直接记入 ``skipped``。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft3Validator, Draft4Validator

from .minimizer import pointer_from_path, set_at

try:  # 与校验器保持完全一致的 JSON 相等性（1.0 == 1、true != 1）。
    from jsonschema._keywords import equal as _json_equal
except ImportError:  # pragma: no cover - 依赖已锁定，仅作防御

    def _json_equal(one: Any, two: Any) -> bool:
        if isinstance(one, bool) or isinstance(two, bool):
            return type(one) is type(two) and one == two
        return bool(one == two)


# 类型变异候选阶梯：null 是最典型的"越界"取值，其余按固定顺序兜底。
_TYPE_CANDIDATES: list[Any] = [None, "x", [], {}, True, 0]
# 枚举/常量变异候选阶梯：类型分散，尽量避免与枚举成员碰撞。
_VALUE_LADDER: list[Any] = [None, "x", [], {}, True, False, 0, 1]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_types(value: Any) -> set[str]:
    """值在 JSON Schema 语义下所属的类型集合（1.0 同时是 integer/number）。"""
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"integer", "number"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    return {"object"}


def _brief(value: Any, limit: int = 40) -> str:
    shown = json.dumps(value, ensure_ascii=False)
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


@dataclass
class Mutation:
    """一个通过实测的最小变异。"""

    index: int
    seed: int
    pointer: str
    keyword: str
    op: str
    detail: str
    instance: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "seed": self.seed,
            "pointer": self.pointer,
            "keyword": self.keyword,
            "op": self.op,
            "detail": self.detail,
            "instance": self.instance,
        }


@dataclass
class Skip:
    """一个无法构造（或被遮蔽）的关键字。"""

    pointer: str
    keyword: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"pointer": self.pointer, "keyword": self.keyword, "reason": self.reason}


class MutationGenerator:
    """对单个 (schema, 合法样例) 对生成最小变异集。"""

    def __init__(self, validator, instance: Any, base_seed: int = 0):
        self.validator = validator
        self.instance = instance
        self.base_seed = base_seed
        self.mutations: list[Mutation] = []
        self.skipped: list[Skip] = []
        self.deduplicated = 0
        self._seen: set[str] = set()
        self._skip_seen: set[tuple] = set()
        # draft-3/4 的 exclusiveMinimum/exclusiveMaximum 是布尔修饰符，
        # 语义并入 minimum/maximum 处理（其报错关键字也是 minimum/maximum）。
        self._legacy_bounds = validator.__class__ in (Draft3Validator, Draft4Validator)
        # 固定处理顺序：与 schema 书写顺序无关，保证输出确定。
        self._handlers = {
            "required": self._mutate_required,
            "type": self._mutate_type,
            "enum": self._mutate_enum,
            "const": self._mutate_const,
            "minimum": self._mutate_minimum,
            "maximum": self._mutate_maximum,
            "exclusiveMinimum": self._mutate_exclusive_minimum,
            "exclusiveMaximum": self._mutate_exclusive_maximum,
            "minLength": self._mutate_min_length,
            "maxLength": self._mutate_max_length,
            "minItems": self._mutate_min_items,
            "maxItems": self._mutate_max_items,
        }

    # ---- 主入口 -----------------------------------------------------------

    def generate(self) -> dict[str, Any]:
        self._walk((), self.validator.schema, self.instance)
        return {
            "seed": self.base_seed,
            "mutations": [m.to_dict() for m in self.mutations],
            "skipped": [s.to_dict() for s in self.skipped],
            "summary": {
                "mutations": len(self.mutations),
                "skipped": len(self.skipped),
                "deduplicated": self.deduplicated,
            },
        }

    # ---- 遍历 -------------------------------------------------------------

    def _walk(self, path: tuple, subschema: Any, node: Any) -> None:
        if not isinstance(subschema, dict):
            # 布尔 schema：true 无约束；false 下样例不可能合法（入口已 422）。
            return
        for keyword, handler in self._handlers.items():
            if keyword in subschema:
                handler(path, subschema, node)

        if isinstance(node, dict):
            properties = subschema.get("properties")
            if isinstance(properties, dict):
                for key, sub in properties.items():
                    if key in node:
                        self._walk(path + (key,), sub, node[key])
            additional = subschema.get("additionalProperties")
            if isinstance(additional, dict):
                for key, value in node.items():
                    if not isinstance(properties, dict) or key not in properties:
                        self._walk(path + (key,), additional, value)
        elif isinstance(node, list):
            start = 0
            prefix = subschema.get("prefixItems")
            items = subschema.get("items")
            if isinstance(prefix, list):
                for i, sub in enumerate(prefix[: len(node)]):
                    self._walk(path + (i,), sub, node[i])
                start = len(prefix)
            elif isinstance(items, list):  # draft ≤7 的元组校验
                for i, sub in enumerate(items[: len(node)]):
                    self._walk(path + (i,), sub, node[i])
                start = len(items)
                items = None  # 旧版 additionalItems 不做推导
            if isinstance(items, dict):
                for i in range(start, len(node)):
                    self._walk(path + (i,), items, node[i])

        # allOf 各分支与当前层等价适用，递归进去收集约束。
        all_of = subschema.get("allOf")
        if isinstance(all_of, list):
            for sub in all_of:
                self._walk(path, sub, node)

    # ---- 变异登记 -----------------------------------------------------------

    def _try(self, path: tuple, keyword: str, op: str, detail: str, mutated_node: Any) -> None:
        """实测一个候选变异：通过去重与首错关键字校验才会被登记。"""
        mutated = set_at(self.instance, path, mutated_node)
        canonical = json.dumps(mutated, sort_keys=True, separators=(",", ":"))
        if canonical in self._seen:
            # 同一实例去重：保留首次生成者（关键字处理顺序固定，结果确定）。
            self.deduplicated += 1
            return
        err = next(iter(self.validator.iter_errors(mutated)), None)
        if err is None:
            self._skip(path, keyword, "变异后实例仍然合法（约束可能被其他关键字遮蔽）")
            return
        if err.validator != keyword:
            self._skip(
                path,
                keyword,
                f"变异首错关键字为 {err.validator!r}，与预期 {keyword!r} 不一致",
            )
            return
        self._seen.add(canonical)
        index = len(self.mutations)
        self.mutations.append(
            Mutation(
                index=index,
                seed=self.base_seed + index,
                pointer=pointer_from_path(path),
                keyword=keyword,
                op=op,
                detail=detail,
                instance=mutated,
            )
        )

    def _skip(self, path: tuple, keyword: str, reason: str) -> None:
        entry = Skip(pointer_from_path(path), keyword, reason)
        key = (entry.pointer, entry.keyword, entry.reason)
        if key not in self._skip_seen:
            self._skip_seen.add(key)
            self.skipped.append(entry)

    # ---- 各关键字的最小变异 --------------------------------------------------

    def _mutate_required(self, path: tuple, subschema: dict, node: Any) -> None:
        required = subschema["required"]
        if not isinstance(required, list):
            return  # draft-3 的 required 是属性级布尔值，不在本接口处理范围
        if not isinstance(node, dict):
            self._skip(path, "required", "样例该位置不是对象，required 在此为空真约束")
            return
        if not required:
            self._skip(path, "required", "required 为空数组，无可删除的必填属性")
            return
        for key in required:
            if key not in node:
                continue  # 样例合法前提下不会发生；防御性跳过
            mutated = {k: v for k, v in node.items() if k != key}
            self._try(path, "required", "remove", f"删除必填属性 {key!r}", mutated)

    def _mutate_type(self, path: tuple, subschema: dict, node: Any) -> None:
        declared = subschema["type"]
        if isinstance(declared, str):
            allowed = {declared}
        elif isinstance(declared, list):
            allowed = {t for t in declared if isinstance(t, str)}
        else:
            allowed = set()
        if not allowed:
            return  # 无法解析的 type 声明（如 draft-3 内嵌 schema），不推导
        for cand in _TYPE_CANDIDATES:
            if _json_types(cand) & allowed:
                continue
            detail = f"替换为 {_brief(cand)}（越出允许类型 {sorted(allowed)}）"
            self._try(path, "type", "replace", detail, cand)
            return
        self._skip(path, "type", "type 已覆盖全部 JSON 类型，无法构造类型违规")

    def _mutate_enum(self, path: tuple, subschema: dict, node: Any) -> None:
        enums = subschema["enum"]
        if not isinstance(enums, list) or not enums:
            return  # 空 enum 下样例不可能合法（入口已 422）
        for cand in _VALUE_LADDER:
            if all(not _json_equal(cand, each) for each in enums):
                self._try(path, "enum", "replace", f"替换为枚举外取值 {_brief(cand)}", cand)
                return
        self._skip(path, "enum", "候选阶梯全部被枚举成员覆盖")

    def _mutate_const(self, path: tuple, subschema: dict, node: Any) -> None:
        const = subschema["const"]
        for cand in _VALUE_LADDER:
            if not _json_equal(cand, const):
                self._try(path, "const", "replace", f"替换为 const 之外的取值 {_brief(cand)}", cand)
                return
        self._skip(path, "const", "候选阶梯全部被 const 覆盖")

    def _mutate_minimum(self, path: tuple, subschema: dict, node: Any) -> None:
        bound = subschema["minimum"]
        if not _is_number(node) or not _is_number(bound):
            if not _is_number(node):
                self._skip(path, "minimum", "样例该位置不是数值，minimum 在此为空真约束")
            return
        if self._legacy_bounds and subschema.get("exclusiveMinimum") is True:
            # draft-3/4：等于边界即违规，且报错关键字归为 minimum。
            self._try(path, "minimum", "nudge", f"取边界值 {_brief(bound)}（违反 exclusiveMinimum）", bound)
            return
        mutated = bound - 1 if isinstance(bound, int) else math.nextafter(bound, -math.inf)
        self._try(path, "minimum", "nudge", f"取 {_brief(mutated)}（< minimum {_brief(bound)}）", mutated)

    def _mutate_maximum(self, path: tuple, subschema: dict, node: Any) -> None:
        bound = subschema["maximum"]
        if not _is_number(node) or not _is_number(bound):
            if not _is_number(node):
                self._skip(path, "maximum", "样例该位置不是数值，maximum 在此为空真约束")
            return
        if self._legacy_bounds and subschema.get("exclusiveMaximum") is True:
            self._try(path, "maximum", "nudge", f"取边界值 {_brief(bound)}（违反 exclusiveMaximum）", bound)
            return
        mutated = bound + 1 if isinstance(bound, int) else math.nextafter(bound, math.inf)
        self._try(path, "maximum", "nudge", f"取 {_brief(mutated)}（> maximum {_brief(bound)}）", mutated)

    def _mutate_exclusive_minimum(self, path: tuple, subschema: dict, node: Any) -> None:
        if self._legacy_bounds:
            return  # 布尔修饰符语义已并入 minimum 处理
        bound = subschema["exclusiveMinimum"]
        if not _is_number(node) or not _is_number(bound):
            if not _is_number(node):
                self._skip(path, "exclusiveMinimum", "样例该位置不是数值，exclusiveMinimum 在此为空真约束")
            return
        self._try(
            path,
            "exclusiveMinimum",
            "nudge",
            f"取边界值 {_brief(bound)}（不满足 > exclusiveMinimum）",
            bound,
        )

    def _mutate_exclusive_maximum(self, path: tuple, subschema: dict, node: Any) -> None:
        if self._legacy_bounds:
            return  # 布尔修饰符语义已并入 maximum 处理
        bound = subschema["exclusiveMaximum"]
        if not _is_number(node) or not _is_number(bound):
            if not _is_number(node):
                self._skip(path, "exclusiveMaximum", "样例该位置不是数值，exclusiveMaximum 在此为空真约束")
            return
        self._try(
            path,
            "exclusiveMaximum",
            "nudge",
            f"取边界值 {_brief(bound)}（不满足 < exclusiveMaximum）",
            bound,
        )

    def _mutate_min_length(self, path: tuple, subschema: dict, node: Any) -> None:
        limit = subschema["minLength"]
        if not isinstance(node, str):
            self._skip(path, "minLength", "样例该位置不是字符串，minLength 在此为空真约束")
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            return
        if limit == 0:
            self._skip(path, "minLength", "minLength=0，无法构造更短的违规字符串")
            return
        mutated = node[: limit - 1]
        self._try(path, "minLength", "truncate", f"截断到长度 {limit - 1}（< minLength {limit}）", mutated)

    def _mutate_max_length(self, path: tuple, subschema: dict, node: Any) -> None:
        limit = subschema["maxLength"]
        if not isinstance(node, str):
            self._skip(path, "maxLength", "样例该位置不是字符串，maxLength 在此为空真约束")
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            return
        mutated = node + "x" * (limit + 1 - len(node))
        self._try(path, "maxLength", "extend", f"加长到长度 {limit + 1}（> maxLength {limit}）", mutated)

    def _mutate_min_items(self, path: tuple, subschema: dict, node: Any) -> None:
        limit = subschema["minItems"]
        if not isinstance(node, list):
            self._skip(path, "minItems", "样例该位置不是数组，minItems 在此为空真约束")
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            return
        if limit == 0:
            self._skip(path, "minItems", "minItems=0，无法构造更短的违规数组")
            return
        mutated = node[: limit - 1]
        self._try(path, "minItems", "truncate", f"截断到 {limit - 1} 个元素（< minItems {limit}）", mutated)

    def _mutate_max_items(self, path: tuple, subschema: dict, node: Any) -> None:
        limit = subschema["maxItems"]
        if not isinstance(node, list):
            self._skip(path, "maxItems", "样例该位置不是数组，maxItems 在此为空真约束")
            return
        if not isinstance(limit, int) or isinstance(limit, bool):
            return
        # 复制末元素填充（末元素必然满足 items 约束），恰好超界一个。
        filler = node[-1] if node else None
        mutated = node + [filler] * (limit + 1 - len(node))
        self._try(path, "maxItems", "extend", f"扩充到 {limit + 1} 个元素（> maxItems {limit}）", mutated)


def generate_mutations(validator, instance: Any, base_seed: int = 0) -> dict[str, Any]:
    """生成最小变异响应体（``validator`` 需由 ``build_validator`` 构造）。"""
    return MutationGenerator(validator, instance, base_seed).generate()
