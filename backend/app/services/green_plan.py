"""修剪灌溉计划板：按物候周期滚动生成未来两周任务。

生成链路：绿化区域品种 × 物候口径（版本化阈值） × 近期天气 × 巡查发现 × 作业车辆。
处置结果同步写入绿化台账、巡查待办和车辆排班清单；计划侧只保留计划基线，
现场调整另存实际字段，历史修剪一律按实际完成时间留痕。

事务口径：生成过程先在暂存区演算（含车辆占用冲突检测），全部成功才一次性提交
到各张清单；任一步失败则回滚，批次保留为草稿，已用批次键不会产生第二份工单。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

# ---- 表名 ----------------------------------------------------------------
T_VERSION = "green_phenology_version"
T_WEATHER = "green_weather"
T_BATCH = "green_plan_batch"
T_TASK = "green_plan_task"
T_LEDGER = "green_ledger"
T_TODO = "patrol_todo"
T_SCHEDULE = "vehicle_schedule"
T_DISPATCH = "green_dispatch_log"

TASK_STATUSES = ["未开始", "进行中", "已调整", "已完成", "已取消", "已迁移"]
BATCH_STATUSES = ["草稿", "已发布", "已重算", "失败"]
PRIORITY_NORMAL = "常规"
PRIORITY_EMERGENCY = "应急管制"

HORIZON_DAYS = 14

# 物候口径默认阈值：按品种类别给修剪/灌溉周期与高温暴雨门槛
DEFAULT_PROFILES: dict[str, dict[str, int]] = {
    "乔木": {"修剪间隔天": 60, "灌溉间隔天": 10, "高温阈值": 35, "暴雨阈值": 50},
    "灌木": {"修剪间隔天": 45, "灌溉间隔天": 6, "高温阈值": 35, "暴雨阈值": 50},
    "绿篱": {"修剪间隔天": 30, "灌溉间隔天": 5, "高温阈值": 35, "暴雨阈值": 50},
    "地被": {"修剪间隔天": 25, "灌溉间隔天": 4, "高温阈值": 34, "暴雨阈值": 50},
    "草坪": {"修剪间隔天": 15, "灌溉间隔天": 3, "高温阈值": 33, "暴雨阈值": 50},
}
CATEGORY_KEYWORDS = ["草坪", "地被", "绿篱", "灌木", "乔木"]
PROFILE_FIELDS = ["修剪间隔天", "灌溉间隔天", "高温阈值", "暴雨阈值"]


def today() -> date:
    return date.today()


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def iso(day: date) -> str:
    return day.isoformat()


class PlanError(RuntimeError):
    """生成或处置失败：消息直接回给前端，调用方负责不产生半成品。"""


class GenerationAborted(PlanError):
    """模拟发布中断：暂存内容转为草稿，重连后可从未生成时段继续。"""


class GreenPlanService:
    # ------------------------------------------------------------------ 查询
    def board(
        self,
        *,
        day: str | None = None,
        task_type: str | None = None,
        status: str | None = None,
        priority: str | None = None,
    ) -> dict[str, Any]:
        """计划板：任务按日期分组，附批次与统计口径。"""
        tasks = store.rows(T_TASK)
        if day:
            tasks = [row for row in tasks if row.get("计划日期") == day]
        if task_type:
            tasks = [row for row in tasks if row.get("任务类型") == task_type]
        if status:
            tasks = [row for row in tasks if row.get("status") == status]
        if priority:
            tasks = [row for row in tasks if row.get("优先级") == priority]
        tasks = sorted(tasks, key=lambda row: (str(row.get("计划日期")), str(row.get("计划时段")), int(row.get("id", 0))))

        days: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            days.setdefault(str(task.get("计划日期")), []).append(task)

        counts = {label: 0 for label in TASK_STATUSES}
        emergency = 0
        for task in store.rows(T_TASK):
            counts[task["status"]] = counts.get(task["status"], 0) + 1
            if task.get("优先级") == PRIORITY_EMERGENCY and task["status"] in ("未开始", "已调整"):
                emergency += 1
        return {
            "days": [{"日期": d, "tasks": items} for d, items in sorted(days.items())],
            "tasks": tasks,
            "total": len(tasks),
            "counts": counts,
            "emergency": emergency,
            "batches": sorted(store.rows(T_BATCH), key=lambda row: int(row.get("id", 0))),
        }

    def ledger(self, *, record_type: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_LEDGER)
        if record_type:
            rows = [row for row in rows if row.get("记录类型") == record_type]
        return sorted(rows, key=lambda row: (str(row.get("实际日期") or row.get("计划日期")), int(row.get("id", 0))), reverse=True)

    def patrol_todos(self, *, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_TODO)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        return sorted(rows, key=lambda row: (str(row.get("计划日期")), int(row.get("id", 0))))

    def vehicle_schedules(self, *, day: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_SCHEDULE)
        if day:
            rows = [row for row in rows if row.get("计划日期") == day]
        return sorted(rows, key=lambda row: (str(row.get("计划日期")), str(row.get("计划时段")), str(row.get("车辆编号"))))

    def weather_list(self) -> list[dict[str, Any]]:
        return sorted(store.rows(T_WEATHER), key=lambda row: str(row.get("日期")))

    def versions(self) -> list[dict[str, Any]]:
        return sorted(store.rows(T_VERSION), key=lambda row: int(row.get("version", 0)), reverse=True)

    # ------------------------------------------------------------ 物候口径版本
    def current_version(self) -> dict[str, Any]:
        for row in self.versions():
            if row.get("status") == "现行":
                return row
        raise PlanError("缺少现行物候口径，请先初始化口径版本")

    def adjust_thresholds(self, profiles: dict[str, Any], remark: str, effective_date: str | None = None) -> dict[str, Any]:
        """口径版本化：落新版本并重算未开始任务；已执行记录保留当时基准快照不动。"""
        clean = self._clean_profiles(profiles)
        current = self.current_version()
        if clean == current.get("profiles"):
            raise PlanError("新口径与现行口径完全一致，无需重算")

        current["status"] = "历史"
        version = self._add_row(T_VERSION, {
            "version": int(current["version"]) + 1,
            "status": "现行",
            "生效日期": effective_date or iso(today()),
            "调整说明": remark or "调整物候阈值",
            "profiles": clean,
        })
        migrated = self._replan_unstarted(version)
        return {"version": version, "migrated": migrated}

    def _clean_profiles(self, profiles: dict[str, Any]) -> dict[str, dict[str, int]]:
        """合并提交值与默认值，保证每个品种四类阈值齐全且为正整数。"""
        merged = {cat: dict(profile) for cat, profile in DEFAULT_PROFILES.items()}
        for cat, raw in (profiles or {}).items():
            if cat not in merged or not isinstance(raw, dict):
                continue
            for field in PROFILE_FIELDS:
                try:
                    value = int(raw.get(field, merged[cat][field]))
                except (TypeError, ValueError):
                    raise PlanError(f"{cat} 的「{field}」必须是整数")
                if value <= 0:
                    raise PlanError(f"{cat} 的「{field}」必须大于 0")
                merged[cat][field] = value
        return merged

    def _replan_unstarted(self, version: dict[str, Any]) -> dict[str, int]:
        """迁移未开始任务：旧计划标记迁移，再按新口径重算两周窗口。"""
        tasks = store.rows(T_TASK)
        moved = 0
        for task in tasks:
            # 仅迁移未开始/现场调整过但未执行的任务；进行中、已完成、已取消保留当时基准
            if task.get("status") not in ("未开始", "已调整"):
                continue
            task["status"] = "已迁移"
            task["pending"] = False
            task["迁移口径"] = version["version"]
            for row in store.rows(T_LEDGER):
                if row.get("关联任务") == task["任务编号"] and row.get("status") not in ("已完成",):
                    row["status"] = "已迁移"
                    row["pending"] = False
                    row["迁移说明"] = f"口径升级至 v{version['version']}，任务重算"
            for row in store.rows(T_SCHEDULE):
                if row.get("任务键") == task.get("任务键") and row.get("status") == "待执行":
                    row["status"] = "已取消"
                    row["pending"] = False
                    row["备注"] = f"口径升级至 v{version['version']}，车辆释放"
            for row in store.rows(T_TODO):
                if row.get("关联任务") == task["任务编号"] and row.get("status") == "待处置":
                    row["status"] = "已迁移"
                    row["pending"] = False
                    row["备注"] = "计划按新口径重算，待办随任务迁移"
            moved += 1

        for batch in store.rows(T_BATCH):
            if batch.get("状态") == "已发布":
                batch["状态"] = "已重算"

        result = self.generate(
            start=iso(today()),
            horizon=HORIZON_DAYS,
            version=version,
            key_suffix=f"-v{version['version']}",
        )
        result["迁移任务数"] = moved
        return result

    # -------------------------------------------------------------- 滚动生成
    def generate(
        self,
        *,
        start: str | None = None,
        horizon: int = HORIZON_DAYS,
        version: dict[str, Any] | None = None,
        key_suffix: str = "",
        fail_after_days: int | None = None,
    ) -> dict[str, Any]:
        """集中生成未来两周任务。同一批次键只生效一次（幂等调度键）。"""
        start_day = parse_date(start) or today()
        if horizon <= 0 or horizon > 60:
            raise PlanError("生成天数需在 1~60 之间")
        end_day = start_day + timedelta(days=horizon - 1)
        version = version or self.current_version()
        batch_key = f"GREEN-PLAN-{iso(start_day)}{key_suffix}"

        # 幂等：已提交批次直接回放，重复调度不再产生工单
        if self._dispatch_seen(batch_key):
            published = next((row for row in store.rows(T_BATCH) if row.get("批次键") == batch_key), None)
            return {"batch": published, "created": 0, "idempotent": True,
                    "message": f"批次 {batch_key} 已发布，重复调度只生效一次"}

        batch = next((row for row in store.rows(T_BATCH)
                      if row.get("批次键") == batch_key and row.get("状态") != "失败"), None)
        if batch is None:
            batch = self._add_row(T_BATCH, {
                "批次键": batch_key,
                "起始日期": iso(start_day),
                "结束日期": iso(end_day),
                "状态": "草稿",
                "已生成截至": None,
                "口径版本": version["version"],
                "任务数": 0,
                "备注": "发布前为计划草稿",
            })

        # 草稿续传：只演算从未生成的时段
        cursor = parse_date(batch.get("已生成截至"))
        begin = (cursor + timedelta(days=1)) if cursor else start_day
        if begin > end_day and batch.get("状态") == "草稿":
            # 时段已演算完但上次中断在提交前：直接走提交
            return self._commit_draft(batch, version)

        staging = self._load_staging(batch)
        weather = self._weather_map()
        handled_findings = self._handled_findings()

        try:
            day = begin
            offset = (begin - start_day).days
            while day <= end_day:
                self._enumerate_day(day, begin, version, weather, handled_findings, staging)
                batch["已生成截至"] = iso(day)
                if fail_after_days is not None and offset + 1 >= fail_after_days:
                    raise GenerationAborted("发布中断：演练参数在提交前打断生成")
                day += timedelta(days=1)
                offset += 1
        except GenerationAborted:
            # 保留计划草稿：暂存任务落草稿表，不写台账/待办/排班、不占车、不记调度键
            self._persist_draft(batch, staging)
            return {"batch": batch, "created": len(staging["tasks"]), "draft": True,
                    "message": f"发布中断，计划草稿保留至 {batch['已生成截至']}，重连后从未生成时段继续"}
        except PlanError:
            # 演算失败（如车辆无法落实）：本批草稿一并清掉，不落任何工单；同批次键允许修正后重试
            kept = [
                row for row in store.rows(T_TASK)
                if not (row.get("批次键") == batch["批次键"] and row.get("草稿"))
            ]
            store.replace_rows(T_TASK, kept)
            batch["状态"] = "失败"
            batch["已生成截至"] = None
            batch["任务数"] = 0
            batch["备注"] = "演算失败已回滚，未占用车辆、未生成工单；条件恢复后可用同批次键重试"
            raise

        return self._commit(batch, staging, version)

    def resume_draft(self, batch_id: int, *, fail_after_days: int | None = None) -> dict[str, Any]:
        """重连续传：从草稿批次的下一时段继续，最终一次提交。"""
        batch = store.find(T_BATCH, batch_id)
        if batch is None:
            raise PlanError(f"计划批次 {batch_id} 不存在")
        if batch.get("状态") != "草稿":
            raise PlanError(f"批次 {batch.get('批次键')} 状态为「{batch.get('状态')}」，无需续传")
        if self._dispatch_seen(batch["批次键"]):
            raise PlanError("该批次已发布，重复调度只生效一次")
        version = self._find_version(int(batch["口径版本"]))
        start_day = parse_date(batch["起始日期"]) or today()
        end_day = parse_date(batch["结束日期"])
        horizon = (end_day - start_day).days + 1
        return self.generate(start=iso(start_day), horizon=horizon, version=version,
                             fail_after_days=fail_after_days)

    def _enumerate_day(
        self,
        day: date,
        window_start: date,
        version: dict[str, Any],
        weather: dict[str, dict[str, Any]],
        handled_findings: set[str],
        staging: dict[str, list[dict[str, Any]]],
    ) -> None:
        """枚举一天内各区域的修剪/灌溉任务，并做天气应急与车辆预占。"""
        for area in store.rows("green"):
            category = self._category_of(str(area.get("植物品种", "")))
            profile = version["profiles"].get(category) or DEFAULT_PROFILES[category]
            self._enumerate_cycle(day, window_start, area, "修剪", profile, version, weather, staging, interval_field="修剪间隔天", last_field="上次修剪")
            self._enumerate_cycle(day, window_start, area, "灌溉", profile, version, weather, staging, interval_field="灌溉间隔天", last_field="上次浇水")
        self._enumerate_findings(day, version, weather, handled_findings, staging)

    def _enumerate_cycle(
        self,
        day: date,
        window_start: date,
        area: dict[str, Any],
        task_type: str,
        profile: dict[str, int],
        version: dict[str, Any],
        weather: dict[str, dict[str, Any]],
        staging: dict[str, list[dict[str, Any]]],
        *,
        interval_field: str,
        last_field: str,
    ) -> None:
        """滚动枚举：窗口首日补排逾期任务，之后每隔一个物候周期再排一次。"""
        interval = int(profile[interval_field])
        last_day = parse_date(area.get(last_field))
        # 没有历史记录的区域按「今日到期」处理，保证滚动窗口内能枚举到
        anchor = last_day or today() - timedelta(days=interval)
        due = anchor + timedelta(days=interval)
        planned_day = due if due >= window_start else window_start
        if (day - planned_day).days < 0 or (day - planned_day).days % interval != 0:
            return

        info = self._weather_decision(day, task_type, profile, weather)
        if info["cancel"]:
            self._stage_task(day, area, task_type, profile, version, staging,
                             priority=PRIORITY_EMERGENCY, slot="—", weather=info["summary"],
                             basis="物候周期", status="已取消", note=info["reason"],
                             vehicle=None)
            return
        target_day = day + timedelta(days=info["shift_days"])
        slot, vehicle = self._assign_slot_vehicle(target_day, task_type, info["slot"], staging)
        if vehicle is None:
            raise PlanError(f"{iso(target_day)} {area.get('区域名称')} 的{task_type}任务全天无可派作业车辆，本次生成已中止回滚")
        self._stage_task(target_day, area, task_type, profile, version, staging,
                         priority=info["priority"], slot=slot, weather=info["summary"],
                         basis="物候周期", vehicle=vehicle,
                         note=info["reason"] if info["priority"] == PRIORITY_EMERGENCY else "")

    def _enumerate_findings(
        self,
        day: date,
        version: dict[str, Any],
        weather: dict[str, dict[str, Any]],
        handled_findings: set[str],
        staging: dict[str, list[dict[str, Any]]],
    ) -> None:
        """巡查发现进入生成链路：未闭环且指向绿化区域的问题只派单一次。"""
        for patrol in store.rows("patrol"):
            finding_no = str(patrol.get("巡查编号", ""))
            problem = str(patrol.get("发现问题", ""))
            if patrol.get("status") in ("已完成", "已复核"):
                continue
            if finding_no in handled_findings or finding_no in staging["findings"]:
                continue
            area = self._match_area(problem)
            task_type = self._match_finding_type(problem)
            if area is None or task_type is None:
                continue
            # 巡查整改安排在窗口内首个可行日
            if day != self._first_suitable_day(day, version, area, task_type, weather):
                continue
            category = self._category_of(str(area.get("植物品种", "")))
            profile = version["profiles"].get(category) or DEFAULT_PROFILES[category]
            info = self._weather_decision(day, task_type, profile, weather)
            target_day = day + timedelta(days=info["shift_days"])
            slot, vehicle = self._assign_slot_vehicle(target_day, task_type, info["slot"], staging)
            if vehicle is None:
                raise PlanError(f"{iso(target_day)} {area.get('区域名称')} 的{task_type}任务全天无可派作业车辆，本次生成已中止回滚")
            # 同区域同类型已有周期计划：把巡查发现并入该计划（升级应急、补待办），不重复派单
            staged = next((t for t in staging["tasks"]
                           if t.get("区域编号") == area.get("区域编号")
                           and t.get("任务类型") == task_type and t.get("status") != "已取消"), None)
            if staged is not None:
                staged["计划依据"] = "物候周期+巡查发现"
                staged["来源巡查"] = finding_no
                staged["巡查问题"] = problem
                staged["优先级"] = PRIORITY_EMERGENCY
                staged["abnormal"] = True
                if info["reason"]:
                    staged["应急说明"] = info["reason"]
                staged["任务键"] = f"F-{area.get('区域编号')}-{task_type}-{staged['计划日期']}"
                staging["findings"].append(finding_no)
                return
            self._stage_task(target_day, area, task_type, profile, version, staging,
                             priority=PRIORITY_EMERGENCY, slot=slot, vehicle=vehicle,
                             weather=info["summary"], basis="巡查发现",
                             note=info["reason"] if info["priority"] == PRIORITY_EMERGENCY else "",
                             patrol_no=finding_no, problem=problem,
                             patrol_road=str(patrol.get("巡查路段", "")))
            staging["findings"].append(finding_no)

    def _first_suitable_day(self, day: date, version: dict[str, Any], area: dict[str, Any], task_type: str,
                            weather: dict[str, dict[str, Any]]) -> date:
        category = self._category_of(str(area.get("植物品种", "")))
        profile = version["profiles"].get(category) or DEFAULT_PROFILES[category]
        cursor = day
        for _ in range(HORIZON_DAYS):
            info = self._weather_decision(cursor, task_type, profile, weather)
            if not info["cancel"] and info["shift_days"] == 0:
                return cursor
            cursor += timedelta(days=1)
        return day

    def _weather_decision(
        self,
        day: date,
        task_type: str,
        profile: dict[str, int],
        weather: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """高温/暴雨应急管制口径：暴雨取消灌溉、整体顺延；高温灌溉改清晨、修剪顺延。"""
        row = weather.get(iso(day), {})
        temp = self._to_float(row.get("最高温"))
        rain = self._to_float(row.get("降水量"))
        alert = str(row.get("预警") or "")
        summary = str(row.get("天气类型") or "无预报")
        is_heat = temp is not None and temp >= float(profile["高温阈值"])
        is_storm = (rain is not None and rain >= float(profile["暴雨阈值"])) or "暴雨" in alert

        result = {"cancel": False, "shift_days": 0, "slot": "08:00-10:00" if task_type == "灌溉" else "09:00-11:00",
                  "priority": PRIORITY_NORMAL, "summary": summary, "reason": ""}

        if is_storm:
            result["summary"] = f"{summary}（降水量 {rain}mm/暴雨预警）"
            if task_type == "灌溉":
                result["cancel"] = True
                result["reason"] = "暴雨应急管制：暂停灌溉，雨后排期"
                return result
            shift = self._shift_days(day, task_type, profile, weather)
            result["shift_days"] = shift
            result["priority"] = PRIORITY_EMERGENCY
            result["reason"] = "暴雨应急管制：作业顺延"
            return result

        if is_heat:
            result["summary"] = f"{summary}（最高温 {temp}℃）"
            if task_type == "灌溉":
                result["slot"] = "06:00-08:00"
                result["priority"] = PRIORITY_EMERGENCY
                result["reason"] = "高温应急管制：灌溉调整至清晨时段"
            else:
                result["shift_days"] = self._shift_days(day, task_type, profile, weather)
                result["priority"] = PRIORITY_EMERGENCY
                result["reason"] = "高温应急管制：修剪避开高温时段顺延"
        return result

    def _shift_days(self, day: date, task_type: str, profile: dict[str, int],
                    weather: dict[str, dict[str, Any]]) -> int:
        cursor = day + timedelta(days=1)
        for offset in range(1, HORIZON_DAYS + 1):
            row = weather.get(iso(cursor), {})
            temp = self._to_float(row.get("最高温"))
            rain = self._to_float(row.get("降水量"))
            alert = str(row.get("预警") or "")
            heat = temp is not None and temp >= float(profile["高温阈值"])
            storm = (rain is not None and rain >= float(profile["暴雨阈值"])) or "暴雨" in alert
            if not heat and not storm:
                return offset
            cursor += timedelta(days=1)
        return 0  # 窗口内均为恶劣天气：保留当日并挂应急管制

    def _stage_task(
        self,
        day: date,
        area: dict[str, Any],
        task_type: str,
        profile: dict[str, int],
        version: dict[str, Any],
        staging: dict[str, list[dict[str, Any]]],
        *,
        priority: str,
        slot: str,
        weather: str,
        basis: str,
        status: str = "未开始",
        note: str = "",
        patrol_no: str | None = None,
        problem: str = "",
        patrol_road: str = "",
        vehicle: dict[str, Any] | None = None,
    ) -> None:
        task_key = f"{'F' if patrol_no else 'C'}-{area.get('区域编号')}-{task_type}-{iso(day)}"
        if any(row["任务键"] == task_key for row in staging["tasks"]):
            return
        # 日期键即幂等键：同批次/滚动重算都靠它挡重复，周期内复发任务不受影响
        if any(row.get("任务键") == task_key and row.get("status") not in ("已取消", "已迁移")
               for row in store.rows(T_TASK)):
            return
        if vehicle is None and status != "已取消":
            # 调用方应先用 _assign_slot_vehicle 落实车辆；没落实就进暂存属于程序错误，直接回滚
            raise PlanError(f"{iso(day)} {area.get('区域名称')} 的{task_type}任务未落实作业车辆，本次生成已中止回滚")

        seq = max(
            (int(str(row.get("任务编号", "GPT-0000")).split("-")[-1]) for row in store.rows(T_TASK)),
            default=0,
        )
        seq = max([seq] + [int(str(t["任务编号"]).split("-")[-1]) for t in staging["tasks"]]) + 1
        task = {
            "任务编号": f"GPT-{seq:04d}",
            "任务键": task_key,
            "批次键": staging["batch_key"],
            "区域编号": area.get("区域编号"),
            "区域名称": area.get("区域名称"),
            "植物品种": area.get("植物品种"),
            "品种类别": self._category_of(str(area.get("植物品种", ""))),
            "任务类型": task_type,
            "计划日期": iso(day),
            "计划时段": slot,
            "计划车辆编号": vehicle.get("车辆编号") if vehicle else None,
            "计划班组": area.get("管养班组"),
            "优先级": priority,
            "天气摘要": weather,
            "计划依据": basis,
            "口径版本": version["version"],
            "阈值快照": dict(profile),
            "来源巡查": patrol_no,
            "巡查问题": problem,
            "应急说明": note,
            "status": status,
            "pending": status in ("未开始", "已调整", "进行中"),
            "abnormal": priority == PRIORITY_EMERGENCY,
            "草稿": True,
        }
        staging["tasks"].append(task)

    def _assign_slot_vehicle(
        self,
        day: date,
        task_type: str,
        preferred_slot: str,
        staging: dict[str, list[dict[str, Any]]],
    ) -> tuple[str, dict[str, Any] | None]:
        """首选时段排不下时顺延到当天备选时段；全部占满才返回空（调用方负责回滚）。"""
        if task_type == "灌溉":
            fallback = ["06:00-08:00", "08:00-10:00", "16:00-18:00"]
        else:
            fallback = ["09:00-11:00", "14:00-16:00"]
        slots = [preferred_slot] + [item for item in fallback if item != preferred_slot]
        for slot in slots:
            vehicle = self._pick_vehicle(day, slot, task_type, staging)
            if vehicle is not None:
                return slot, vehicle
        return preferred_slot, None

    def _pick_vehicle(
        self,
        day: date,
        slot: str,
        task_type: str,
        staging: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        """事务提交后才能占用车辆：这里只在暂存区预占，最终随提交写入排班清单。"""
        keyword = "灌溉" if task_type == "灌溉" else "修剪"
        occupied = {
            (row.get("计划日期"), row.get("计划时段"), row.get("车辆编号"))
            for row in store.rows(T_SCHEDULE) if row.get("status") == "待执行"
        }
        occupied |= {
            (row["计划日期"], row["计划时段"], row["计划车辆编号"]) for row in staging["tasks"]
        }
        candidates = [
            row for row in store.rows("vehicle")
            if row.get("status") == "在库" and keyword in str(row.get("车辆类型", ""))
        ]
        for vehicle in candidates:
            if (iso(day), slot, vehicle.get("车辆编号")) not in occupied:
                return vehicle
        return None

    # -------------------------------------------------------------- 暂存与提交
    def _load_staging(self, batch: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """草稿续传：把上次中断时保留的草稿任务搬回暂存区。"""
        tasks = [dict(row) for row in store.rows(T_TASK)
                 if row.get("批次键") == batch["批次键"] and row.get("草稿")]
        findings = [str(row.get("来源巡查")) for row in tasks if row.get("来源巡查")]
        return {"batch_key": batch["批次键"], "tasks": tasks, "findings": findings}

    def _persist_draft(self, batch: dict[str, Any], staging: dict[str, list]) -> None:
        """中断时保留计划草稿：只落任务草稿，台账/待办/排班一律不写。"""
        existing = {row.get("任务键") for row in store.rows(T_TASK) if row.get("批次键") == batch["批次键"]}
        for task in staging["tasks"]:
            if task["任务键"] in existing:
                continue
            self._add_row(T_TASK, dict(task))
        batch["状态"] = "草稿"
        batch["任务数"] = len([t for t in staging["tasks"]])

    def _commit(self, batch: dict[str, Any], staging: dict[str, list], version: dict[str, Any]) -> dict[str, Any]:
        """一次性提交：任务转正式、写台账/待办/排班、登记调度键。失败整体回滚。"""
        snapshot = {name: len(store.rows(name)) for name in (T_TASK, T_LEDGER, T_TODO, T_SCHEDULE, T_DISPATCH)}
        try:
            ledger_links: dict[str, dict[str, Any]] = {}
            for task in staging["tasks"]:
                task["草稿"] = False
                record_type = "巡查整改" if task.get("来源巡查") else task["任务类型"]
                ledger = self._add_row(T_LEDGER, {
                    "台账编号": f"GLG-{int(task['任务编号'].split('-')[1]):04d}",
                    "记录类型": record_type,
                    "区域编号": task["区域编号"],
                    "区域名称": task["区域名称"],
                    "植物品种": task["植物品种"],
                    "计划日期": task["计划日期"],
                    "计划时段": task["计划时段"],
                    "计划班组": task["计划班组"],
                    "计划车辆编号": task["计划车辆编号"],
                    "实际日期": None,
                    "实际完成时间": None,
                    "计划口径版本": f"v{task['口径版本']}",
                    "执行口径快照": task["阈值快照"],
                    "天气摘要": task["天气摘要"],
                    "优先级": task["优先级"],
                    "关联批次": task["批次键"],
                    "关联任务": task["任务编号"],
                    "备注": task.get("应急说明", ""),
                    "status": task["status"],
                    "pending": task["pending"],
                    "abnormal": task["abnormal"],
                })
                ledger_links[task["任务编号"]] = ledger

            for task in staging["tasks"]:
                saved = next((row for row in store.rows(T_TASK)
                              if row.get("任务键") == task["任务键"] and row.get("批次键") == batch["批次键"]), None)
                if saved is not None:
                    saved.update(task)
                    saved["台账编号"] = ledger_links[task["任务编号"]]["台账编号"]
                else:
                    task["台账编号"] = ledger_links[task["任务编号"]]["台账编号"]
                    self._add_row(T_TASK, dict(task))

                if task["status"] == "已取消":
                    continue
                self._add_row(T_SCHEDULE, {
                    "排班单号": f"VSH-{len(store.rows(T_SCHEDULE)) + 1:04d}",
                    "计划日期": task["计划日期"],
                    "计划时段": task["计划时段"],
                    "车辆编号": task["计划车辆编号"],
                    "车辆类型": self._vehicle_type(task["计划车辆编号"]),
                    "车牌号": self._vehicle_plate(task["计划车辆编号"]),
                    "任务类型": task["任务类型"],
                    "区域编号": task["区域编号"],
                    "区域名称": task["区域名称"],
                    "任务键": task["任务键"],
                    "关联任务": task["任务编号"],
                    "批次键": task["批次键"],
                    "口径版本": f"v{task['口径版本']}",
                    "来源": "绿化计划",
                    "status": "待执行",
                    "pending": True,
                    "abnormal": task["优先级"] == PRIORITY_EMERGENCY,
                })
                if task.get("来源巡查"):
                    self._add_row(T_TODO, {
                        "待办编号": f"PTD-{len(store.rows(T_TODO)) + 1:04d}",
                        "来源模块": "绿化计划",
                        "巡查编号": task["来源巡查"],
                        "巡查路段": task.get("巡查问题") or task["区域名称"],
                        "发现问题": task.get("巡查问题"),
                        "处置措施": f"{task['任务类型']}作业（计划 {task['计划日期']} {task['计划时段']}）",
                        "计划日期": task["计划日期"],
                        "优先级": task["优先级"],
                        "关联任务": task["任务编号"],
                        "批次键": task["批次键"],
                        "口径版本": f"v{task['口径版本']}",
                        "status": "待处置",
                        "pending": True,
                        "abnormal": True,
                    })

            batch["状态"] = "已发布"
            batch["任务数"] = len(staging["tasks"])
            batch["备注"] = f"按 v{version['version']} 口径生成并已同步台账/待办/排班"
            self._add_row(T_DISPATCH, {"批次键": batch["批次键"], "committed_at": datetime.now().isoformat(timespec="seconds")})
        except Exception:
            # 回滚：截掉本次提交新增的所有行，车辆排班未提交即未占车，不留下重复工单
            for name, length in snapshot.items():
                rows = store.rows(name)
                del rows[length:]
            raise
        return {"batch": batch, "created": len(staging["tasks"]), "idempotent": False,
                "message": f"批次 {batch['批次键']} 已发布，生成 {len(staging['tasks'])} 项任务并同步台账、巡查待办与车辆排班"}

    def _commit_draft(self, batch: dict[str, Any], version: dict[str, Any]) -> dict[str, Any]:
        staging = self._load_staging(batch)
        return self._commit(batch, staging, version)

    # -------------------------------------------------------------- 现场处置
    def adjust_task(self, task_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """现场调整：计划基线不动，实际字段另存；必须给调整原因。"""
        task = store.find(T_TASK, task_id)
        if task is None:
            return None, f"计划任务 {task_id} 不存在"
        if task.get("status") in ("已完成", "已取消", "已迁移"):
            return None, f"任务状态为「{task.get('status')}」，不允许现场调整"
        reason = str(values.get("调整原因") or "").strip()
        if not reason:
            return None, "现场调整必须填写调整原因，计划基线需可追溯"

        actual_day = parse_date(values.get("实际日期"))
        actual_slot = str(values.get("实际时段") or task.get("计划时段")).strip()
        vehicle_code = str(values.get("实际车辆编号") or task.get("计划车辆编号") or "").strip()
        if actual_day:
            conflict = self._vehicle_conflict(actual_day, actual_slot, vehicle_code, exclude_task=task["任务编号"])
            if conflict:
                return None, f"车辆 {vehicle_code} 在 {iso(actual_day)} {actual_slot} 已有排班，现场调整未生效"
        task["实际日期"] = iso(actual_day) if actual_day else task.get("实际日期")
        task["实际时段"] = actual_slot
        task["实际车辆编号"] = vehicle_code or task.get("实际车辆编号")
        task["实际班组"] = str(values.get("实际班组") or task.get("计划班组") or "").strip()
        task["调整原因"] = reason
        task["status"] = "已调整"
        task["pending"] = True
        self._sync_ledger(task)
        return task, "现场调整已记录，计划日期与口径基线保持不变"

    def complete_task(self, task_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """完成作业：历史修剪/灌溉按实际完成时间留痕，并释放车辆、闭环待办。"""
        task = store.find(T_TASK, task_id)
        if task is None:
            return None, f"计划任务 {task_id} 不存在"
        if task.get("status") in ("已完成", "已取消", "已迁移"):
            return None, f"任务状态为「{task.get('status')}」，不能重复完成"

        actual_day = parse_date(values.get("实际日期")) or today()
        finished_at = str(values.get("实际完成时间") or datetime.now().isoformat(timespec="seconds"))
        slot = str(values.get("实际时段") or task.get("实际时段") or task.get("计划时段") or "").strip()
        vehicle_code = str(values.get("实际车辆编号") or task.get("实际车辆编号") or task.get("计划车辆编号") or "").strip()
        team = str(values.get("实际班组") or task.get("实际班组") or task.get("计划班组") or "").strip()

        if self._vehicle_conflict(actual_day, slot, vehicle_code, exclude_task=task["任务编号"]):
            # 计划车辆当天被别的任务占用时，自动改派同类型空闲车；现场点名指定才硬拦截
            if str(values.get("实际车辆编号") or "").strip():
                return None, f"车辆 {vehicle_code} 在 {iso(actual_day)} {slot} 已有其他任务，完成登记被拦下"
            alt = self._find_idle_vehicle(actual_day, slot, str(task["任务类型"]), exclude_task=task["任务编号"])
            if alt is None:
                return None, f"{iso(actual_day)} {slot} 无空闲{task['任务类型']}车辆，请改期或更换车辆后再登记完成"
            vehicle_code = str(alt["车辆编号"])

        task["status"] = "已完成"
        task["pending"] = False
        task["实际日期"] = iso(actual_day)
        task["实际时段"] = slot
        task["实际完成时间"] = finished_at
        task["实际车辆编号"] = vehicle_code
        task["实际班组"] = team
        task["完成结果"] = str(values.get("完成结果") or "现场作业完成").strip()

        # 历史修剪按实际完成时间回写绿化区域，下轮物候周期从实际完成日起算
        area = next((row for row in store.rows("green") if row.get("区域编号") == task.get("区域编号")), None)
        if area is not None:
            if task["任务类型"] == "修剪":
                area["上次修剪"] = iso(actual_day)
                area["status"] = "正常"
            elif task["任务类型"] == "灌溉":
                area["上次浇水"] = iso(actual_day)

        for row in store.rows(T_LEDGER):
            if row.get("关联任务") == task["任务编号"]:
                row["status"] = "已完成"
                row["pending"] = False
                row["实际日期"] = iso(actual_day)
                row["实际完成时间"] = finished_at
                row["实际时段"] = slot
                row["实际车辆编号"] = vehicle_code
                row["实际班组"] = team
                row["完成结果"] = task["完成结果"]
        for row in store.rows(T_SCHEDULE):
            if row.get("关联任务") == task["任务编号"] and row.get("status") != "已取消":
                row["status"] = "已完成"
                row["pending"] = False
                row["实际日期"] = iso(actual_day)
                row["实际完成时间"] = finished_at
        for row in store.rows(T_TODO):
            if row.get("关联任务") == task["任务编号"] and row.get("status") != "已迁移":
                row["status"] = "已完成"
                row["pending"] = False
                row["实际完成时间"] = finished_at
        return task, f"{task['任务类型']}作业已按实际完成时间 {finished_at} 留痕，台账/待办/排班已同步"

    def _vehicle_conflict(self, day: date, slot: str, vehicle_code: str, *, exclude_task: str) -> dict[str, Any] | None:
        for row in store.rows(T_SCHEDULE):
            if row.get("关联任务") == exclude_task:
                continue
            if row.get("status") == "待执行" and row.get("计划日期") == iso(day) \
                    and row.get("计划时段") == slot and row.get("车辆编号") == vehicle_code:
                return row
        return None

    def _find_idle_vehicle(self, day: date, slot: str, task_type: str, *, exclude_task: str) -> dict[str, Any] | None:
        keyword = "灌溉" if task_type == "灌溉" else "修剪"
        busy = {
            row.get("车辆编号") for row in store.rows(T_SCHEDULE)
            if row.get("关联任务") != exclude_task and row.get("status") == "待执行"
            and row.get("计划日期") == iso(day) and row.get("计划时段") == slot
        }
        return next((v for v in store.rows("vehicle")
                     if v.get("status") == "在库" and keyword in str(v.get("车辆类型", ""))
                     and v.get("车辆编号") not in busy), None)

    def _sync_ledger(self, task: dict[str, Any]) -> None:
        for row in store.rows(T_LEDGER):
            if row.get("关联任务") == task["任务编号"]:
                row["status"] = "已调整"
                row["实际日期"] = task.get("实际日期")
                row["实际时段"] = task.get("实际时段")
                row["实际车辆编号"] = task.get("实际车辆编号")
                row["实际班组"] = task.get("实际班组")
                row["调整原因"] = task.get("调整原因")

    # -------------------------------------------------------------- 天气维护
    def upsert_weather(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        day = parse_date(values.get("日期"))
        if day is None:
            return None, "预报日期格式应为 YYYY-MM-DD"
        try:
            temp = float(values.get("最高温"))
            rain = float(values.get("降水量", 0))
        except (TypeError, ValueError):
            return None, "最高温与降水量必须是数字"
        payload = {"日期": iso(day), "天气类型": str(values.get("天气类型") or "晴").strip(),
                   "最高温": temp, "降水量": rain, "预警": str(values.get("预警") or "").strip()}
        existing = next((row for row in store.rows(T_WEATHER) if row.get("日期") == payload["日期"]), None)
        if existing is not None:
            existing.update(payload)
            return existing, f"{payload['日期']} 天气预报已更新"
        return self._add_row(T_WEATHER, payload), f"{payload['日期']} 天气预报已录入"

    # -------------------------------------------------------------- 工具方法
    def _add_row(self, table: str, payload: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(table)
        row = {"id": max((int(item.get("id", 0)) for item in rows), default=0) + 1}
        row.update(payload)
        rows.append(row)
        return row

    def _dispatch_seen(self, batch_key: str) -> bool:
        return any(row.get("批次键") == batch_key for row in store.rows(T_DISPATCH))

    def _find_version(self, number: int) -> dict[str, Any]:
        row = next((row for row in store.rows(T_VERSION) if int(row.get("version", 0)) == number), None)
        if row is None:
            raise PlanError(f"物候口径 v{number} 不存在")
        return row

    def _weather_map(self) -> dict[str, dict[str, Any]]:
        return {str(row["日期"]): row for row in store.rows(T_WEATHER)}

    def _category_of(self, species: str) -> str:
        for keyword in CATEGORY_KEYWORDS:
            if keyword in species:
                return keyword
        return "灌木"

    def _match_area(self, text: str) -> dict[str, Any] | None:
        for area in store.rows("green"):
            code = str(area.get("区域编号") or "")
            if code and code in text:
                return area
        return None

    def _match_finding_type(self, text: str) -> str | None:
        if "修剪" in text or "徒长" in text or "枝" in text:
            return "修剪"
        if "灌溉" in text or "浇水" in text or "旱" in text or "枯黄" in text:
            return "灌溉"
        return None

    def _handled_findings(self) -> set[str]:
        """已进入未取消任务（含历史）的巡查发现不重复派单。"""
        return {
            str(row.get("来源巡查"))
            for row in store.rows(T_TASK)
            if row.get("来源巡查") and row.get("status") not in ("已迁移",)
        }

    def _vehicle_type(self, code: Any) -> str | None:
        row = next((item for item in store.rows("vehicle") if item.get("车辆编号") == code), None)
        return row.get("车辆类型") if row else None

    def _vehicle_plate(self, code: Any) -> str | None:
        row = next((item for item in store.rows("vehicle") if item.get("车辆编号") == code), None)
        return row.get("车牌号") if row else None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -------------------------------------------------------------- 演示引导
    def ensure_demo_data(self) -> None:
        """幂等播种：现行口径、两周天气（含高温/暴雨日）、绿化作业车辆与巡查发现。"""
        if not store.rows(T_VERSION):
            self._add_row(T_VERSION, {
                "version": 1, "status": "现行", "生效日期": iso(today()),
                "调整说明": "初始物候口径：按品种类别设定修剪/灌溉周期与高温暴雨阈值",
                "profiles": {cat: dict(profile) for cat, profile in DEFAULT_PROFILES.items()},
            })

        if not store.rows(T_WEATHER):
            heat_day = (today() + timedelta(days=6)).isoformat()
            storm_day = (today() + timedelta(days=9)).isoformat()
            for offset in range(HORIZON_DAYS):
                day = (today() + timedelta(days=offset)).isoformat()
                if day == heat_day:
                    payload = {"日期": day, "天气类型": "晴热", "最高温": 37, "降水量": 0, "预警": "高温橙色"}
                elif day == storm_day:
                    payload = {"日期": day, "天气类型": "暴雨", "最高温": 26, "降水量": 72, "预警": "暴雨黄色"}
                else:
                    payload = {"日期": day, "天气类型": "多云" if offset % 2 else "晴",
                               "最高温": 28 + (offset % 5), "降水量": 0, "预警": ""}
                self._add_row(T_WEATHER, payload)

        # 绿化专用作业车辆（幂等：编号存在即跳过）
        demo_vehicles = [
            ("VEHI-G01", "绿化修剪车", "苏E·GL101"),
            ("VEHI-G02", "绿化修剪车", "苏E·GL102"),
            ("VEHI-G03", "绿化灌溉车", "苏E·GL201"),
            ("VEHI-G04", "绿化灌溉车", "苏E·GL202"),
        ]
        existing_codes = {row.get("车辆编号") for row in store.rows("vehicle")}
        for code, kind, plate in demo_vehicles:
            if code in existing_codes:
                continue
            self._add_row("vehicle", {
                "车辆编号": code, "车辆类型": kind, "车牌号": plate, "所属单位": "绿化管养一班",
                "年检日期": iso(today()), "驾驶员": "示范驾驶员", "当前里程": "32000",
                "车辆状态": "在库", "status": "在库", "pending": True, "abnormal": False,
            })

        # 巡查发现：指向绿化区域的问题会进入下次滚动生成
        findings = [
            ("PATR-1001", "GREE-0002 中央分隔带", "GREE-0002 绿篱徒长严重遮挡标志，需安排修剪"),
            ("PATR-1002", "GREE-0003 互通匝道区", "GREE-0003 草坪连续干旱枯黄，需尽快灌溉浇水"),
        ]
        existing_patrol = {row.get("巡查编号") for row in store.rows("patrol")}
        for code, road, problem in findings:
            if code in existing_patrol:
                continue
            self._add_row("patrol", {
                "巡查编号": code, "巡查路段": road, "巡查日期": iso(today()),
                "巡查人员": "巡查示范员", "巡查车辆": "PATR-CAR", "发现问题": problem,
                "处置措施": "转入修剪灌溉计划板", "巡查状态": "待处置",
                "status": "巡查中", "pending": True, "abnormal": True,
            })

        # 把样例区域补成真实品种与作业历史，物候周期才有据可依
        # 偏移刻意让周期到期日落在高温日（+6）与暴雨日（+9），便于看到应急管制口径
        species_map = {
            "GREE-0001": ("香樟乔木行道树", "绿化管养一班", 50, 8),
            "GREE-0002": ("金叶女贞绿篱", "绿化管养二班", 21, 4),
            "GREE-0003": ("狗牙根草坪", "绿化管养一班", 9, 3),
        }
        for area in store.rows("green"):
            code = area.get("区域编号")
            if code not in species_map or area.get("_demo_patched"):
                continue
            species, team, prune_ago, water_ago = species_map[code]
            area["植物品种"] = species
            area["管养班组"] = team
            area["区域名称"] = {"GREE-0001": "K12+000 行道树段", "GREE-0002": "K15+300 中央分隔带", "GREE-0003": "K18+800 互通匝道区"}[code]
            area["面积"] = {"GREE-0001": "4200㎡", "GREE-0002": "1800㎡", "GREE-0003": "6500㎡"}[code]
            area["上次修剪"] = iso(today() - timedelta(days=prune_ago))
            area["上次浇水"] = iso(today() - timedelta(days=water_ago))
            area["_demo_patched"] = True


plan_service = GreenPlanService()
plan_service.ensure_demo_data()
