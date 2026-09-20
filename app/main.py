"""FastAPI 入口：JSON Schema 反例最小化服务。"""

from __future__ import annotations

import os
from typing import Annotated, Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from jsonschema.exceptions import SchemaError

from .minimizer import Minimizer, build_validator

# ---- 配置（全部可通过环境变量覆盖）----------------------------------------

DEFAULT_MAX_EVALUATIONS = int(os.getenv("DEFAULT_MAX_EVALUATIONS", "1000"))
HARD_MAX_EVALUATIONS = int(os.getenv("HARD_MAX_EVALUATIONS", "100000"))

app = FastAPI(
    title="JSON Schema Counterexample Minimizer",
    description=(
        "对未通过 JSON Schema 校验的实例做确定性 delta debugging 最小化，"
        "锁定首个校验错误的关键字，只保留仍触发同一关键字的候选。"
    ),
    version="1.0.0",
)


class MinimizeRequest(BaseModel):
    # JSON Schema 本身可以是 object 或 boolean（true/false schema）。
    # 字段名避开 BaseModel.schema，JSON 键通过 alias 保持为 "schema"。
    schema_: Annotated[
        Any,
        Field(
            alias="schema",
            title="JSON Schema",
            description="待针对校验的 JSON Schema，支持 draft 3/4/6/7/2019-09/2020-12。",
        ),
    ]
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
