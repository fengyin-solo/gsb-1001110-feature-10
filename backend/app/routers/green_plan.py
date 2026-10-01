"""修剪灌溉计划板接口。

围绕「区域品种 / 近期天气 / 巡查发现 / 作业车辆」生成链路，提供：
枚举两周草稿、草稿保存（发布中断可续）、事务发布、口径版本调整、计划重排、
现场完成留痕，以及台账 / 排班 / 批次只读清单。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import (
    ActionResult,
    CaliberAdjustPayload,
    CompleteTaskPayload,
    PublishPayload,
)
from app.services.green_plan import service

router = APIRouter(prefix="/api/green/plan", tags=["绿化修剪灌溉计划板"])


@router.get("")
def board() -> dict[str, Any]:
    """计划板看板：口径版本、窗口、各状态任务量、管制与车辆占用一览。"""
    return {
        "summary": service.board_summary(),
        "links": {
            "枚举草稿": "GET /api/green/plan/draft",
            "发布": "POST /api/green/plan/publish",
            "口径重排": "POST /api/green/plan/recalculate",
        },
    }


@router.get("/inputs")
def inputs() -> dict[str, Any]:
    """生成链路输入：区域品种、近期天气、巡查发现、可派车辆。"""
    return {
        "areas": service.list_areas(),
        "weather": service.list_weather(),
        "findings": service.list_findings(),
        "caliber": service.current_caliber(),
    }


@router.get("/draft")
def preview_draft() -> dict[str, Any]:
    """实时枚举未来两周任务草稿（不落库）；发布中断后可据此重建续发。"""
    return service.enumerate_plan()


@router.post("/draft")
def save_draft(payload: PublishPayload) -> ActionResult:
    """保留计划草稿：把当前两周枚举结果登记为草稿批次，发布中断重连后可续发。"""
    draft = service.enumerate_plan()
    batch = service.save_draft(draft, payload.批次键)
    return ActionResult(ok=True, message=f"计划草稿已保留（{batch['批次键']}），可随时发布", entry=batch)


@router.post("/publish")
def publish(payload: PublishPayload) -> dict[str, Any]:
    """事务发布：提交成功后才占用车辆；失败整体回滚；重复批次键幂等只生效一次。"""
    return service.publish(payload.批次键, force_fail=payload.force_fail)


@router.post("/recalculate")
def recalculate() -> dict[str, Any]:
    """物候阈值调整后重排：迁移未开始任务，已执行记录保留当时基准。"""
    return service.recalculate()


@router.get("/calibers")
def calibers() -> dict[str, Any]:
    """口径版本清单：历史版本与当前生效版本。"""
    return {"current": service.current_caliber(), "items": service.list_calibers()}


@router.post("/calibers", response_model=ActionResult)
def adjust_caliber(payload: CaliberAdjustPayload) -> ActionResult:
    """调整物候阈值，生成新生效口径版本；随后可调 recalculate 迁移未开始任务。"""
    entry, message = service.adjust_caliber(payload.model_dump())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/ledger")
def ledger(status: str | None = Query(default=None, description="未开始/进行中/已完成/已取消")) -> dict[str, Any]:
    """绿化台账（含历史留痕与本次生成的计划工单）。"""
    items = service.list_ledger(status)
    return {"total": len(items), "items": items}


@router.post("/ledger/{ledger_id}/complete", response_model=ActionResult)
def complete_task(ledger_id: int, payload: CompleteTaskPayload) -> ActionResult:
    """现场完成填报：按实际完成时间留痕，联动收车并关闭巡查待办。"""
    entry, message = service.complete_task(ledger_id, payload.model_dump())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/schedule")
def schedule() -> dict[str, Any]:
    """车辆排班清单：每个已发布工单对应一条占用记录。"""
    items = service.list_schedule()
    return {"total": len(items), "items": items}


@router.get("/batches")
def batches() -> dict[str, Any]:
    """发布批次登记：调度键状态可追溯，用于核对幂等只生效一次。"""
    return {"total": len(service.list_batches()), "items": service.list_batches()}
