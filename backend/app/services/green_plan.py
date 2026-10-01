"""修剪灌溉计划板：按物候周期滚动生成未来两周任务的核心业务规则。

生成链路（系统梳理四类输入）：
    区域品种（green_area）  ──┐
    近期天气（green_weather） ─┼─► 物候周期枚举 ─► 天气应急覆盖 ─► 巡查发现补充 ─► 作业车辆试排
    巡查发现（green_finding）─┘                                   （高温/暴雨以应急管制优先）

关键约束：
- 口径版本化：物候阈值（高温/暴雨/修剪/灌溉间隔）按版本留痕；阈值调整后，仅「未开始」
  任务按新口径重算，已执行记录保留当时基准版本。
- 计划重排：迁移未开始任务（取消旧未开始工单、按新口径重算），现场已调整/已执行维持原样。
- 发布中断：先落「计划草稿」，发布在事务里提交；失败回滚不留半成品工单，重连可续发。
- 幂等调度：批次键（batch_key）唯一，重复调度同一批次只生效一次；提交成功后才占用车辆。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.store import store

# ---------- 表名 ----------
T_AREA = "green_area"
T_CALIBER = "green_caliber"
T_WEATHER = "green_weather"
T_FINDING = "green_finding"
T_LEDGER = "green_ledger"
T_SCHEDULE = "green_vehicle_schedule"
T_BATCH = "green_batch"
T_PATROL = "patrol"
T_VEHICLE = "vehicle"

PLAN_HORIZON_DAYS = 14

# 任务/工单状态
ST_DRAFT = "草稿"
ST_PENDING = "未开始"
ST_DOING = "进行中"
ST_DONE = "已完成"
ST_CANCELLED = "已取消"
# 应急管制
CTRL_HEAT = "高温管制"
CTRL_RAIN = "暴雨管制"
CTRL_NORMAL = "正常"
# 作业类型
OP_TRIM = "修剪"
OP_IRRIGATE = "灌溉"
OP_PEST = "病虫防治"

# 物候类型 → 默认作业节奏（天）。口径调整会覆盖这些值（按品种的修剪/灌溉间隔）。
# 这里作为「系统内建物候节律」，口径表里的高温/暴雨阈值与覆盖间隔共同决定最终计划。
PHENOLOGY: dict[str, dict[str, Any]] = {
    "绿篱": {"trim": 21, "irrigate": 7, "prefer": "高空修剪车"},
    "草坪": {"trim": 14, "irrigate": 6, "prefer": "绿化综合养护车"},
    "灌木": {"trim": 25, "irrigate": 7, "prefer": "绿化综合养护车"},
    "藤本": {"trim": 30, "irrigate": 8, "prefer": "高空修剪车"},
    "乔木": {"trim": 45, "irrigate": 10, "prefer": "高空修剪车"},
}
DEFAULT_PHENOLOGY = {"trim": 28, "irrigate": 7, "prefer": "绿化综合养护车"}

# 作业 → 需要的车辆类型关键字（用于在排班里挑车）
OP_VEHICLE_KIND = {
    OP_TRIM: ("高空修剪车", "绿化综合养护车"),
    OP_IRRIGATE: ("绿化喷洒车", "绿化综合养护车"),
    OP_PEST: ("绿化喷洒车", "绿化综合养护车"),
}


def _today() -> date:
    """以固定「今天」为滚动基准；可被服务参数覆盖，便于演示与测试。"""
    return date(2026, 10, 1)


def _parse(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _d(value: date) -> str:
    return value.strftime("%Y-%m-%d")


class GreenPlanService:
    # ======================================================================
    # 口径版本
    # ======================================================================
    def current_caliber(self) -> dict[str, Any]:
        rows = store.rows(T_CALIBER)
        current = next((r for r in rows if int(r.get("是否生效", 0)) == 1), None)
        return dict(current) if current else {
            "版本号": "V0", "高温阈值": 35, "暴雨阈值": 50,
        }

    def list_calibers(self) -> list[dict[str, Any]]:
        return [dict(r) for r in store.rows(T_CALIBER)]

    def adjust_caliber(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """调整物候阈值 → 生成新口径版本并置为生效；旧版本保留当时基准。

        阈值变化后调用 recalculate 才会重排未开始任务（口径变更与计划重排解耦，
        保证「重排」动作可追溯）。
        """
        try:
            heat = int(values.get("高温阈值"))
            rain = int(values.get("暴雨阈值"))
        except (TypeError, ValueError):
            return None, "高温阈值、暴雨阈值必须是整数"
        if not (20 <= heat <= 50 and 5 <= rain <= 300):
            return None, "阈值超出合理范围（高温 20-50℃、暴雨 5-300mm）"
        note = str(values.get("创建说明") or "物候阈值调整").strip()
        rows = store.rows(T_CALIBER)
        for r in rows:
            r["是否生效"] = 0
        version = f"V{_today().strftime('%Y.%m')}-{store.next_id(T_CALIBER):02d}"
        entry = {
            "id": store.next_id(T_CALIBER),
            "版本号": version,
            "是否生效": 1,
            "生效起始": _d(_today()),
            "高温阈值": heat,
            "暴雨阈值": rain,
            "创建说明": note,
        }
        rows.append(entry)
        return entry, f"已生成并生效新口径版本 {version}；请执行计划重排以迁移未开始任务"

    # ======================================================================
    # 输入链路数据（区域/天气/巡查发现）
    # ======================================================================
    def list_areas(self) -> list[dict[str, Any]]:
        return [dict(r) for r in store.rows(T_AREA)]

    def list_weather(self) -> list[dict[str, Any]]:
        return [dict(r) for r in store.rows(T_WEATHER)]

    def list_findings(self) -> list[dict[str, Any]]:
        return [dict(r) for r in store.rows(T_FINDING)]

    def _weather_index(self) -> dict[str, dict[str, Any]]:
        return {str(w["日期"]): w for w in store.rows(T_WEATHER)}

    def _control(self, day: date, weather: dict[str, Any] | None, caliber: dict[str, Any]) -> str:
        """高温或暴雨时以应急管制为准；暴雨优先级高于高温。"""
        if not weather:
            return CTRL_NORMAL
        if float(weather.get("降水量", 0) or 0) >= float(caliber["暴雨阈值"]):
            return CTRL_RAIN
        if float(weather.get("最高温", 0) or 0) >= float(caliber["高温阈值"]):
            return CTRL_HEAT
        return CTRL_NORMAL

    # ======================================================================
    # 计划枚举：物候周期 → 天气覆盖 → 巡查补充 → 车辆试排（只生成草稿，不写库）
    # ======================================================================
    def enumerate_plan(self, start: date | None = None) -> dict[str, Any]:
        """枚举未来两周任务，返回草稿结构；此步骤无副作用，发布中断后可反复重建。"""
        today = start or _today()
        end = today + timedelta(days=PLAN_HORIZON_DAYS - 1)
        caliber = self.current_caliber()
        weather_idx = self._weather_index()
        areas = store.rows(T_AREA)
        findings = store.rows(T_FINDING)

        tasks: list[dict[str, Any]] = []
        for area in areas:
            pheno = PHENOLOGY.get(str(area.get("物候类型")), DEFAULT_PHENOLOGY)
            tasks.extend(self._enumerate_area_cycle(area, pheno, today, end, weather_idx, caliber))
            tasks.extend(self._enumerate_findings(area, findings, today, end, weather_idx, caliber))

        # 同日同区域同作业去重（周期任务与巡查任务可能撞同一窗口）
        tasks = self._dedup(tasks)
        tasks.sort(key=lambda t: (t["计划日期"], t["区域编号"], t["作业类型"]))

        # 车辆试排（不改库，仅在草稿里给出建议车辆；发布提交时才真正占用）
        # 预占已发布且仍有效的排班，避免把同一车辆在同一天重复派出。
        occupancy: dict[tuple[str, str], str] = {
            (str(s.get("车辆编号")), str(s.get("日期"))): "已排班"
            for s in store.rows(T_SCHEDULE) if s.get("状态") == "已排班"
        }
        # 容量感知滚动排班：到期日车辆满员时，向后续正常作业日借车顺延，避免全挤在窗口首日。
        used_keys: set[tuple[str, str, str]] = set()
        # 紧急（巡查高优先级）与早到期的任务先派车
        tasks.sort(key=lambda t: (0 if t.get("紧急程度") == "高" else 1, t["计划日期"]))
        for t in tasks:
            self._schedule_task(t, today, end, weather_idx, caliber, occupancy, used_keys)
        tasks.sort(key=lambda t: (t["计划日期"], t["区域编号"], t["作业类型"]))

        return {
            "状态": ST_DRAFT,
            "口径版本": caliber["版本号"],
            "窗口起": _d(today),
            "窗口止": _d(end),
            "任务数": len(tasks),
            "任务": tasks,
        }

    def _enumerate_area_cycle(
        self, area, pheno, today, end, weather_idx, caliber
    ) -> list[dict[str, Any]]:
        """按物候修剪/灌溉间隔，从上次实际作业时间滚动枚举落在窗口内的周期任务。"""
        out: list[dict[str, Any]] = []
        last_trim = _parse(area.get("上次修剪日期"))
        last_irr = _parse(area.get("上次灌溉日期"))
        for op, last, interval in (
            (OP_TRIM, last_trim, int(pheno["trim"])),
            (OP_IRRIGATE, last_irr, int(pheno["irrigate"])),
        ):
            base = last or today
            due = base + timedelta(days=interval)
            while due <= end:
                if due >= today - timedelta(days=2):  # 允许把刚到期(前两天起)的任务纳入
                    day = self._resolve_window(due, today, end, weather_idx, caliber)
                    out.append(self._task(area, op, day, "周期计划", caliber, pheno,
                                          weather_idx, due_date=due))
                due = due + timedelta(days=interval)
        return out

    def _enumerate_findings(
        self, area, findings, today, end, weather_idx, caliber
    ) -> list[dict[str, Any]]:
        """巡查发现补充任务：未处理的发现按紧急程度给建议日期，并入同一窗口。"""
        out: list[dict[str, Any]] = []
        pheno = PHENOLOGY.get(str(area.get("物候类型")), DEFAULT_PHENOLOGY)
        for f in findings:
            if str(f.get("区域编号")) != str(area.get("区域编号")):
                continue
            if int(f.get("是否已处理", 0)) == 1:
                continue
            op = str(f.get("建议作业"))
            if op not in OP_VEHICLE_KIND:
                continue
            found = _parse(f.get("发现日期")) or today
            delay = 0 if f.get("紧急程度") == "高" else 2
            due = max(found + timedelta(days=delay), today)
            day = self._resolve_window(due, today, end, weather_idx, caliber)
            t = self._task(area, op, day, f"巡查发现·{f.get('发现编号')}", caliber, pheno,
                           weather_idx, due_date=due)
            t["紧急程度"] = f.get("紧急程度")
            t["发现编号"] = f.get("发现编号")
            t["发现描述"] = f.get("描述")
            out.append(t)
        return out

    def _resolve_window(self, due, today, end, weather_idx, caliber) -> date:
        """把到期日落到可行作业窗口：高温/暴雨日顺延到管制解除后的首个正常日。

        极端天气以应急管制优先级为准——不在管制窗口内强行安排作业。
        """
        day = max(due, today)
        guard = 0
        while day <= end and guard <= PLAN_HORIZON_DAYS:
            w = weather_idx.get(_d(day))
            if self._control(day, w, caliber) == CTRL_NORMAL:
                return day
            day += timedelta(days=1)
            guard += 1
        return min(day, end)  # 窗口内全是管制日时贴窗口末尾，并保留管制标记

    def _task(self, area, op, day, source, caliber, pheno, weather_idx, *, due_date) -> dict[str, Any]:
        w = weather_idx.get(_d(day))
        control = self._control(day, w, caliber)
        return {
            "区域编号": area.get("区域编号"),
            "区域名称": area.get("区域名称"),
            "所属路段": area.get("所属路段"),
            "植物品种": area.get("植物品种"),
            "班组": area.get("管养班组"),
            "作业类型": op,
            "计划日期": _d(day),
            "到期日期": _d(due_date),
            "来源": source,
            "口径版本": caliber["版本号"],
            "应急管制": control,
            "天气": (w or {}).get("天气", "—"),
            "最高温": (w or {}).get("最高温", ""),
            "降水量": (w or {}).get("降水量", ""),
            "偏好车辆": pheno.get("prefer"),
            "建议车辆": "",
            "紧急程度": "常规",
        }

    def _dedup(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str]] = set()
        out = []
        for t in tasks:
            key = (t["区域编号"], t["计划日期"], t["作业类型"])
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    # ======================================================================
    # 车辆试排（事务提交前不占用）
    # ======================================================================
    def _candidate_vehicles(self, op: str) -> list[dict[str, Any]]:
        """候选车辆：排除维修中的车辆；偏好排序在 _assign_vehicle 里按作业类型处理。"""
        return [v for v in store.rows(T_VEHICLE) if str(v.get("status")) != "维修"]

    def _assign_vehicle(
        self, op: str, day: str, occupancy: dict[tuple[str, str], str]
    ) -> str | None:
        """同日不重复派同一辆车；先派偏好车型，派满后回退其它在库可用车。"""
        usable = self._candidate_vehicles(op)
        preferred = {str(v.get("车辆编号")) for v in usable
                     if str(v.get("车辆类型")) in OP_VEHICLE_KIND.get(op, ())}
        ordered = sorted(usable, key=lambda v: 0 if str(v.get("车辆编号")) in preferred else 1)
        for v in ordered:
            code = str(v.get("车辆编号"))
            if occupancy.get((code, day)):
                continue
            occupancy[(code, day)] = "占位"
            return code
        return None

    def _schedule_task(self, task, today, end, weather_idx, caliber,
                       occupancy, used_keys) -> None:
        """容量感知滚动排班：从计划日起向后找「正常作业日 + 空闲车」。

        极端天气（高温/暴雨）日不派车；车辆当日满员则顺延到下一个可行日，
        保证窗口内车辆资源够用时每一条任务都能排到车，而不是挤爆窗口首日。
        """
        start = _parse(task["计划日期"]) or today
        day = start
        guard = 0
        while day <= end and guard <= PLAN_HORIZON_DAYS + 2:
            ds = _d(day)
            w = weather_idx.get(ds)
            if self._control(day, w, caliber) == CTRL_NORMAL:
                key = (task["区域编号"], ds, task["作业类型"])
                if key not in used_keys:
                    code = self._assign_vehicle(task["作业类型"], ds, occupancy)
                    if code:
                        occupancy[(code, ds)] = task["区域编号"]
                        used_keys.add(key)
                        task["计划日期"] = ds
                        task["应急管制"] = CTRL_NORMAL
                        task["天气"] = (w or {}).get("天气", "—")
                        task["最高温"] = (w or {}).get("最高温", "")
                        task["降水量"] = (w or {}).get("降水量", "")
                        task["建议车辆"] = code
                        return
            day += timedelta(days=1)
            guard += 1
        # 整个窗口都排不下：保留原日期并标记待派车，发布时进「待派车」清单人工介入
        task["建议车辆"] = "无可用车辆"

    # ======================================================================
    # 批次登记 / 幂等
    # ======================================================================
    def _batch_key(self, window_start: str) -> str:
        """默认批次键：按口径版本 + 两周窗口起点，天然保证「同一窗口同一口径只生成一次」。"""
        return f"{self.current_caliber()['版本号']}@{window_start}"

    def _find_batch(self, batch_key: str) -> dict[str, Any] | None:
        return next((b for b in store.rows(T_BATCH) if b.get("批次键") == batch_key), None)

    def list_batches(self) -> list[dict[str, Any]]:
        return [dict(b) for b in store.rows(T_BATCH)]

    # ======================================================================
    # 草稿：发布中断时保留
    # ======================================================================
    def save_draft(self, draft: dict[str, Any], batch_key: str | None = None) -> dict[str, Any]:
        """把草稿登记为草稿批次（不占用车辆、不写工单）；重连后可从同一批次续发。"""
        key = batch_key or self._batch_key(draft["窗口起"])
        existing = self._find_batch(key)
        if existing and existing.get("状态") == "已发布":
            return existing
        if existing:
            existing.update({"草稿": draft, "更新时间": datetime.now().isoformat(timespec="seconds")})
            return existing
        batch = {
            "id": store.next_id(T_BATCH),
            "批次键": key,
            "口径版本": draft["口径版本"],
            "窗口起": draft["窗口起"],
            "窗口止": draft["窗口止"],
            "状态": ST_DRAFT,
            "任务数": draft["任务数"],
            "草稿": draft,
            "创建时间": datetime.now().isoformat(timespec="seconds"),
            "更新时间": datetime.now().isoformat(timespec="seconds"),
        }
        store.rows(T_BATCH).append(batch)
        return batch

    # ======================================================================
    # 发布：事务提交后才占用车辆；失败整体回滚；幂等
    # ======================================================================
    def publish(self, batch_key: str | None = None, *, force_fail: bool = False) -> dict[str, Any]:
        """发布草稿批次到台账 / 巡查待办 / 车辆排班。

        - 批次键已发布 → 幂等返回，绝不重复生成工单。
        - 无批次键 → 现场枚举两周草稿后发布（首次调度）。
        - 任一步失败（或 force_fail 模拟发布中断）→ 回滚整个事务，不留半成品。
        """
        draft = None
        key = batch_key
        if key:
            batch = self._find_batch(key)
            if not batch:
                return {"ok": False, "message": f"批次 {key} 的草稿不存在，请先生成"}
            if batch.get("状态") == "已发布":
                return {"ok": True, "message": f"批次 {key} 已发布，重复调度只生效一次",
                        "幂等命中": True, "批次": self._public_batch(batch)}
            draft = batch.get("草稿")
        else:
            draft = self.enumerate_plan()
            key = self._batch_key(draft["窗口起"])
            if self._find_batch(key) and self._find_batch(key).get("状态") == "已发布":
                batch = self._find_batch(key)
                return {"ok": True, "message": f"批次 {key} 已发布，重复调度只生效一次",
                        "幂等命中": True, "批次": self._public_batch(batch)}

        snapshot = store.snapshot()  # 事务保存点
        try:
            batch = self._find_batch(key) or self.save_draft(draft, key)
            created = self._commit_publish(batch, draft, force_fail=force_fail)
        except Exception as exc:  # 发布中断：整体回滚，不留重复工单 / 不占用车辆
            store.restore(snapshot)
            return {"ok": False, "message": f"发布中断已回滚：{exc}；草稿已保留，可续发",
                    "批次键": key, "回滚": True}

        return {
            "ok": True,
            "message": f"批次 {key} 发布成功，已写入绿化台账/巡查待办/车辆排班",
            "幂等命中": False,
            "批次": self._public_batch(batch),
            "生成": created,
        }

    def _commit_publish(self, batch, draft, *, force_fail: bool) -> dict[str, int]:
        """事务体：台账工单 → 巡查待办 → 车辆排班 → 批次置已发布。任一异常向上抛出触发回滚。"""
        caliber = draft["口径版本"]
        n_ledger = n_patrol = n_schedule = 0
        for t in draft["任务"]:
            # 1) 绿化台账（计划工单）
            ledger_id = store.next_id(T_LEDGER)
            schedule_key = f"{batch['批次键']}|{t['区域编号']}|{t['计划日期']}|{t['作业类型']}"
            store.rows(T_LEDGER).append({
                "id": ledger_id,
                "台账编号": f"GL-{t['计划日期'].replace('-', '')}-{ledger_id:03d}",
                "区域编号": t["区域编号"],
                "区域名称": t["区域名称"],
                "作业类型": t["作业类型"],
                "计划日期": t["计划日期"],
                "实际完成日期": "",
                "状态": ST_PENDING,
                "口径版本": caliber,
                "来源": t["来源"],
                "班组": t["班组"],
                "车辆编号": t["建议车辆"],
                "应急管制": t["应急管制"],
                "调度键": schedule_key,
                "完成说明": "",
                "批次键": batch["批次键"],
            })
            n_ledger += 1

            # 2) 巡查待办（落到现有 patrol 表；待办状态驱动现场巡查）
            patrol_id = store.next_id(T_PATROL)
            store.rows(T_PATROL).append({
                "id": patrol_id,
                "status": "待巡查",
                "pending": True,
                "abnormal": t["应急管制"] != CTRL_NORMAL or t["紧急程度"] == "高",
                "巡查编号": f"PATR-G{patrol_id:04d}",
                "巡查路段": t["所属路段"] or t["区域名称"],
                "巡查日期": t["计划日期"],
                "巡查人员": t["班组"],
                "巡查车辆": t["建议车辆"],
                "发现问题": f"{t['区域名称']}{t['作业类型']}作业现场核查",
                "处置措施": "配合绿化计划板作业",
                "巡查状态": "待巡查",
                "来源批次": batch["批次键"],
                "调度键": schedule_key,
            })
            n_patrol += 1

            # 3) 车辆排班清单（此刻事务即将成功，才真正占用车辆）
            sched_id = store.next_id(T_SCHEDULE)
            store.rows(T_SCHEDULE).append({
                "id": sched_id,
                "排班编号": f"GS-{sched_id:04d}",
                "调度键": schedule_key,
                "批次键": batch["批次键"],
                "车辆编号": t["建议车辆"],
                "日期": t["计划日期"],
                "班次": "白班",
                "用途": f"{t['区域编号']} {t['作业类型']}",
                "状态": "已排班" if t["建议车辆"] != "无可用车辆" else "待派车",
            })
            n_schedule += 1

            if force_fail:
                raise RuntimeError("模拟发布中断（事务提交前故障）")

        batch["状态"] = "已发布"
        batch["发布时间"] = datetime.now().isoformat(timespec="seconds")
        batch["更新时间"] = batch["发布时间"]
        batch.pop("草稿", None)
        return {"台账工单": n_ledger, "巡查待办": n_patrol, "车辆排班": n_schedule}

    # ======================================================================
    # 计划重排：口径阈值调整后，迁移未开始任务（已执行保留当时基准）
    # ======================================================================
    def recalculate(self) -> dict[str, Any]:
        """按当前口径重算窗口内未开始任务：

        - 已完成 / 进行中（现场已调整）维持原计划与当时口径版本，不动；
        - 仅未开始任务取消旧工单（及其巡查待办、车辆排班），按新口径重新枚举发布；
        - 用新批次键发布，历史批次与历史记录保留。
        """
        caliber = self.current_caliber()
        # 1) 收集未开始工单覆盖的窗口（取最早计划日 ~ 最晚计划日），用于重算同范围
        pending = [r for r in store.rows(T_LEDGER)
                   if r.get("状态") == ST_PENDING and r.get("批次键")]
        if not pending:
            return {"ok": True, "message": "没有需要迁移的未开始任务", "迁移工单数": 0}
        dates = sorted(_parse(r["计划日期"]) for r in pending if _parse(r.get("计划日期")))
        window_start = dates[0]

        snapshot = store.snapshot()
        try:
            migrated = self._migrate_pending(pending, window_start, caliber)
        except Exception as exc:
            store.restore(snapshot)
            return {"ok": False, "message": f"计划重排失败已回滚：{exc}"}

        return {
            "ok": True,
            "message": f"已按新口径 {caliber['版本号']} 重排未开始任务，已执行记录保留原基准",
            "迁移工单数": migrated["取消"],
            "新批次键": migrated["批次键"],
            "重新生成": migrated["生成"],
        }

    def _migrate_pending(self, pending, window_start, caliber) -> dict[str, Any]:
        # 1) 取消未开始台账工单，并回收其巡查待办与车辆排班
        old_keys = {str(r.get("调度键")) for r in pending}
        for r in pending:
            r["状态"] = ST_CANCELLED
            r["取消说明"] = f"口径调整为{caliber['版本号']}，未开始任务迁移重排"
        for p in store.rows(T_PATROL):
            if str(p.get("调度键")) in old_keys and str(p.get("status")) == "待巡查":
                p["status"] = "已取消"
                p["pending"] = False
                p["巡查状态"] = "已取消"
        for s in store.rows(T_SCHEDULE):
            if str(s.get("调度键")) in old_keys and str(s.get("状态")) in ("已排班", "待派车"):
                s["状态"] = "已释放"

        # 2) 按新口径重算同窗口两周草稿并发布（事务内）
        draft = self.enumerate_plan(start=window_start)
        key = f"{caliber['版本号']}@{draft['窗口起']}#re{store.next_id(T_BATCH)}"
        batch = self.save_draft(draft, key)
        created = self._commit_publish(batch, draft, force_fail=False)
        return {"取消": len(pending), "批次键": key, "生成": created}

    # ======================================================================
    # 现场完成：历史修剪按实际完成时间留痕；现场调整维持此前计划
    # ======================================================================
    def complete_task(self, ledger_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """现场完成一条台账工单：以「实际完成日期」留痕（默认今天），并联动收车、关闭待办。

        计划重排只迁移未开始任务，所以现场已执行/进行中的记录不会被改写。
        """
        ledger = store.find(T_LEDGER, ledger_id)
        if ledger is None:
            return None, f"台账工单 {ledger_id} 不存在"
        if ledger.get("状态") == ST_DONE:
            return None, "该工单已完成，不能重复填报"
        if ledger.get("状态") == ST_CANCELLED:
            return None, "该工单已随口径调整取消，不能再填报"
        actual = _parse(values.get("实际完成日期")) or _today()
        note = str(values.get("完成说明") or "").strip() or "现场按计划完成"

        ledger["状态"] = ST_DONE
        ledger["实际完成日期"] = _d(actual)
        ledger["完成说明"] = note
        # 历史修剪按实际完成时间回写区域最近作业，作为下一周期滚动基准
        area = next((a for a in store.rows(T_AREA) if a.get("区域编号") == ledger.get("区域编号")), None)
        if area:
            if ledger.get("作业类型") == OP_TRIM:
                area["上次修剪日期"] = _d(actual)
            elif ledger.get("作业类型") == OP_IRRIGATE:
                area["上次灌溉日期"] = _d(actual)
        # 车辆收车、排班完成
        for s in store.rows(T_SCHEDULE):
            if str(s.get("调度键")) == str(ledger.get("调度键")):
                s["状态"] = "已完成"
        # 关联巡查待办关闭
        for p in store.rows(T_PATROL):
            if str(p.get("调度键")) == str(ledger.get("调度键")) and p.get("status") == "待巡查":
                p["status"] = "已完成"
                p["pending"] = False
                p["巡查状态"] = "已完成"
        return ledger, f"已按实际完成时间 {_d(actual)} 留痕"

    def list_ledger(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(T_LEDGER)
        if status:
            rows = [r for r in rows if r.get("状态") == status]
        return [dict(r) for r in rows]

    def list_schedule(self) -> list[dict[str, Any]]:
        return [dict(r) for r in store.rows(T_SCHEDULE)]

    def board_summary(self) -> dict[str, Any]:
        """计划板看板汇总：任务/应急/车辆/批次口径状态一览。"""
        ledger = store.rows(T_LEDGER)
        sched = store.rows(T_SCHEDULE)
        caliber = self.current_caliber()
        return {
            "口径版本": caliber["版本号"],
            "窗口": f"{_d(_today())} ~ {_d(_today() + timedelta(days=PLAN_HORIZON_DAYS - 1))}",
            "未开始": sum(1 for r in ledger if r.get("状态") == ST_PENDING),
            "进行中": sum(1 for r in ledger if r.get("状态") == ST_DOING),
            "已完成": sum(1 for r in ledger if r.get("状态") == ST_DONE),
            "已取消": sum(1 for r in ledger if r.get("状态") == ST_CANCELLED),
            "高温/暴雨管制": sum(1 for r in ledger if r.get("应急管制") in (CTRL_HEAT, CTRL_RAIN)),
            "排班占用": sum(1 for s in sched if s.get("状态") == "已排班"),
            "待派车": sum(1 for s in sched if s.get("状态") == "待派车"),
            "已发布批次": sum(1 for b in store.rows(T_BATCH) if b.get("状态") == "已发布"),
            "草稿批次": sum(1 for b in store.rows(T_BATCH) if b.get("状态") == ST_DRAFT),
        }

    def _public_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in batch.items() if k != "草稿"}
        return out


service = GreenPlanService()
