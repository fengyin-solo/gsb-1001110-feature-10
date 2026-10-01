"""修剪灌溉计划板接口。

滚动生成（幂等批次键）、草稿断点续传、口径版本调整与重算、现场调整与完成留痕，
生成结果一次性同步绿化台账、巡查待办和车辆排班清单。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.green_plan import (
    HORIZON_DAYS,
    PROFILE_FIELDS,
    GreenPlanService,
    PlanError,
)

router = APIRouter(prefix="/api/green-plan", tags=["修剪灌溉计划板"])

service = GreenPlanService()


@router.get("/board")
def get_board(
    day: str | None = Query(default=None, description="按计划日期过滤"),
    task_type: str | None = Query(default=None, description="修剪 / 灌溉"),
    status: str | None = None,
    priority: str | None = Query(default=None, description="常规 / 应急管制"),
) -> dict[str, Any]:
    """计划板视图：任务按日期分组，并带状态统计、应急量与批次清单。"""
    return service.board(day=day, task_type=task_type, status=status, priority=priority)


@router.post("/generate", response_model=ActionResult)
def generate_plan(payload: EntryPayload) -> ActionResult:
    """集中生成未来两周任务。

    同一批次键只生效一次；fail_after_days 用于演练发布中断——保留草稿、
    不写台账/待办/排班、不占用车辆，重连后调用续传接口从未生成时段继续。
    """
    values = payload.values or {}
    try:
        horizon = int(values.get("horizon", HORIZON_DAYS))
        fail_after = values.get("fail_after_days")
        fail_after_days = int(fail_after) if fail_after not in (None, "") else None
        result = service.generate(
            start=str(values.get("start") or "") or None,
            horizon=horizon,
            fail_after_days=fail_after_days,
        )
    except (PlanError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ok = not result.get("draft")
    return ActionResult(ok=ok, message=result["message"], entry=result.get("batch"))


@router.post("/batches/{batch_id}/resume", response_model=ActionResult)
def resume_batch(batch_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """重连续传：从草稿批次的下一时段继续演算，成功后一次提交全部清单。"""
    values = (payload.values if payload else None) or {}
    fail_after = values.get("fail_after_days")
    fail_after_days = int(fail_after) if fail_after not in (None, "") else None
    try:
        result = service.resume_draft(batch_id, fail_after_days=fail_after_days)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return ActionResult(ok=not result.get("draft"), message=result["message"], entry=result.get("batch"))


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    return {"items": service.board()["batches"]}


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    rows = service.versions()
    return {"items": rows, "current": service.current_version()}


@router.post("/versions/adjust", response_model=ActionResult)
def adjust_version(payload: EntryPayload) -> ActionResult:
    """物候阈值调整：落新版本；未开始任务按新口径重算，已执行记录保留当时基准。"""
    values = payload.values or {}
    profiles = values.get("profiles")
    if not isinstance(profiles, dict):
        return ActionResult(ok=False, message="请按品种类别提交 profiles 阈值对象")
    try:
        result = service.adjust_thresholds(
            profiles,
            remark=str(values.get("remark") or "").strip(),
            effective_date=str(values.get("effective_date") or "") or None,
        )
    except PlanError as exc:
        return ActionResult(ok=False, message=str(exc))
    migrated = result["migrated"]
    return ActionResult(
        ok=True,
        message=(
            f"已发布 v{result['version']['version']} 新口径；"
            f"迁移未开始任务 {migrated.get('迁移任务数', 0)} 项，"
            f"新口径生成任务 {migrated.get('created', 0)} 项；已执行记录保留当时基准"
        ),
        entry={"version": result["version"], "migrated": migrated},
    )


@router.get("/threshold-fields")
def threshold_fields() -> dict[str, Any]:
    return {"fields": PROFILE_FIELDS}


@router.get("/weather")
def list_weather() -> dict[str, Any]:
    return {"items": service.weather_list()}


@router.post("/weather", response_model=ActionResult)
def upsert_weather(payload: EntryPayload) -> ActionResult:
    """维护近期天气预报：高温/暴雨日会触发生成时的应急管制口径。"""
    row, message = service.upsert_weather(payload.values or {})
    if row is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=row)


@router.get("/ledger")
def list_ledger(record_type: str | None = None) -> dict[str, Any]:
    """绿化台账：计划与执行同页对照，历史修剪按实际完成时间留痕。"""
    return {"items": service.ledger(record_type=record_type)}


@router.get("/todos")
def list_todos(status: str | None = None) -> dict[str, Any]:
    """巡查待办：计划处置结果同步推入，可按状态过滤。"""
    return {"items": service.patrol_todos(status=status)}


@router.get("/vehicle-schedules")
def list_schedules(day: str | None = None) -> dict[str, Any]:
    """车辆排班清单：仅事务提交后的任务才占用车辆。"""
    return {"items": service.vehicle_schedules(day=day)}


@router.post("/tasks/{task_id}/adjust", response_model=ActionResult)
def adjust_task(task_id: int, payload: EntryPayload) -> ActionResult:
    """现场调整：维持计划基线（日期/时段/车辆只读留痕），实际字段与原因另存。"""
    task, message = service.adjust_task(task_id, payload.values or {})
    if task is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=task)


@router.post("/tasks/{task_id}/complete", response_model=ActionResult)
def complete_task(task_id: int, payload: EntryPayload) -> ActionResult:
    """完成作业：按实际完成时间写入台账并回写区域上次修剪/浇水，同步释放车辆、闭环待办。"""
    task, message = service.complete_task(task_id, payload.values or {})
    if task is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=task)
