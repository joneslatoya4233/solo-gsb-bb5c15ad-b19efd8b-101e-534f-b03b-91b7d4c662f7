"""FastAPI 入口：JSON Schema 反例最小化服务。"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from jsonschema.exceptions import SchemaError

from .minimizer import Minimizer, build_validator
from .mutator import generate_mutations

# ---- 配置（全部可通过环境变量覆盖）----------------------------------------

DEFAULT_MAX_EVALUATIONS = int(os.getenv("DEFAULT_MAX_EVALUATIONS", "1000"))
HARD_MAX_EVALUATIONS = int(os.getenv("HARD_MAX_EVALUATIONS", "100000"))

app = FastAPI(
    title="JSON Schema Counterexample Minimizer",
    description=(
        "对未通过 JSON Schema 校验的实例做确定性 delta debugging 最小化，"
        "锁定首个校验错误的关键字，只保留仍触发同一关键字的候选；"
        "也可以给定合法样例，确定性生成覆盖各约束关键字的最小变异实例。"
    ),
    version="1.0.0",
)


class SchemaBoundRequest(BaseModel):
    # JSON Schema 本身可以是 object 或 boolean（true/false schema）。
    # 字段名避开 BaseModel.schema，JSON 键通过 alias 保持为 "schema"。
    schema_: Annotated[
        Any,
        Field(
            alias="schema",
            title="JSON Schema",
            description="JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
        ),
    ]


class MinimizeRequest(SchemaBoundRequest):
    instance: Any = Field(
        ...,
        title="未通过校验的 JSON 实例",
    )
    max_evaluations: int | None = Field(
        default=None,
        ge=1,
        le=HARD_MAX_EVALUATIONS,
        description=(
            "最大校验评估次数（含对原实例的首次校验）；"
            f"默认 {DEFAULT_MAX_EVALUATIONS}，硬上限 {HARD_MAX_EVALUATIONS}。"
        ),
    )


class MutationsRequest(SchemaBoundRequest):
    instance: Any = Field(
        ...,
        title="通过校验的合法 JSON 样例（变异基线）",
    )
    seed: int = Field(
        default=0,
        ge=0,
        le=2_147_483_647,
        description=(
            "seed 基数：第 i 个变异的 seed = seed + i。生成全程无随机源，"
            "相同 schema + instance + seed 重放必然得到相同响应。"
        ),
    )


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/minimize")
def minimize(req: MinimizeRequest) -> JSONResponse:
    max_evaluations = (
        req.max_evaluations
        if req.max_evaluations is not None
        else DEFAULT_MAX_EVALUATIONS
    )

    # schema 自身不合法：返回 422。
    try:
        validator = build_validator(req.schema_)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "message": exc.message,
                "path": list(exc.absolute_path),
            },
        )

    minimizer = Minimizer(validator, req.instance, max_evaluations)
    # 第 1 次评估同时负责"实例确实非法"的确认与目标关键字锁定
    # （lock 内部已设置 target_keyword，含 false schema 的 None 关键字）。
    first_error = minimizer.lock()
    if first_error is None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "instance_valid",
                "message": "提供的实例已通过 schema 校验，无需最小化；本接口只接受反例。",
            },
        )

    result = minimizer.minimize()
    return JSONResponse(status_code=200, content=result.to_dict())


@app.post("/mutations")
def mutations(req: MutationsRequest) -> JSONResponse:
    # schema 自身不合法：返回 422。
    try:
        validator = build_validator(req.schema_)
    except SchemaError as exc:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_schema",
                "message": exc.message,
                "path": list(exc.absolute_path),
            },
        )

    # 样例必须合法：变异以合法样例为基线，非法样例请改用 /minimize。
    first_error = next(iter(validator.iter_errors(req.instance)), None)
    if first_error is not None:
        return JSONResponse(
            status_code=422,
            content={
                "error": "instance_invalid",
                "message": "样例未通过 schema 校验；本接口需要合法样例作为变异基线，非法实例请使用 /minimize。",
                "first_error": {
                    "keyword": first_error.validator,
                    "path": list(first_error.absolute_path),
                    "message": first_error.message,
                },
            },
        )

    return JSONResponse(
        status_code=200,
        content=generate_mutations(validator, req.instance, req.seed),
    )
