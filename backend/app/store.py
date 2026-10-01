"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.seed import SEED_ROWS

# 计划板内部领域表：不进运营概览的“业务模块”清单，避免把台账/排班/口径版本表当成独立模块。
HIDDEN_TABLES = {
    "green_area",
    "green_caliber",
    "green_weather",
    "green_finding",
    "green_ledger",
    "green_vehicle_schedule",
    "green_batch",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in HIDDEN_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """事务保存点：深拷贝全部表，发布失败时整体还原，保证不留半成品工单。"""
        return deepcopy(self._tables)

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        """回滚到保存点；事务期间任何表（含台账、车辆、巡查待办）都恢复原样。"""
        self._tables = deepcopy(snapshot)

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
