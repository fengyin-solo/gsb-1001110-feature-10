# 市政道路桥梁养护管理平台

覆盖道路巡查、桥隧定检、路面病害、交安设施、绿化管养、除雪防汛及养护工程管理的市政道桥全要素养护后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 路段管理 | `road_section` | 管养路段 | 路段编号、路段名称、起止桩号 |
| 日常巡查 | `patrol` | 巡查记录 | 巡查编号、巡查路段、巡查日期 |
| 路面病害 | `pavement` | 病害记录 | 病害编号、所属路段、病害类型 |
| 桥梁定检 | `bridge` | 检测记录 | 检测编号、桥梁名称、检测类型 |
| 桥梁档案 | `bridge_info` | 桥梁 | 桥梁编号、桥梁名称、桥型结构 |
| 隧道管养 | `tunnel` | 隧道 | 隧道编号、隧道名称、隧道长度 |
| 交安设施 | `traffic_facility` | 交安设施 | 设施编号、设施类型、所属路段 |
| 排水设施 | `drainage` | 排水设施 | 设施编号、设施类型、所属路段 |
| 绿化管养 | `green` | 绿化区域 | 区域编号、区域名称、植物品种 |
| 修剪灌溉计划板 | `green_plan` | 物候周期计划 | 计划任务、口径版本、绿化台账、巡查待办、车辆排班 |
| 路灯照明 | `lighting` | 路灯设施 | 灯具编号、灯具类型、功率 |
| 除雪防滑 | `winter` | 除雪作业 | 作业编号、作业路段、作业日期 |
| 防汛应急 | `flood` | 防汛记录 | 记录编号、预警级别、影响路段 |
| 边坡防护 | `slope` | 边坡 | 边坡编号、所属路段、边坡类型 |
| 伸缩缝管理 | `expansion` | 伸缩缝 | 缝编号、所属桥梁、缝类型 |
| 支座维护 | `bearing` | 桥梁支座 | 支座编号、所属桥梁、支座类型 |
| 养护工程 | `project` | 养护工程 | 工程编号、工程名称、工程类型 |
| 养护车辆 | `vehicle` | 养护车辆 | 车辆编号、车辆类型、车牌号 |
| 养护材料 | `material` | 养护材料 | 材料编号、材料名称、材料类别 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 修剪灌溉计划板（`green_plan`）

绿化管养按物候周期滚动生成未来两周的修剪/灌溉任务，生成链路为
**区域品种 × 物候口径 × 近期天气 × 巡查发现 × 作业车辆**：

- `POST /api/green-plan/generate` 集中生成；批次键（如 `GREEN-PLAN-2026-10-01`）即幂等调度键，
  重复调度只生效一次。传 `fail_after_days` 可演练发布中断：保留计划草稿、不占车、不写清单，
  重连后调 `POST /api/green-plan/batches/{id}/resume` 从「已生成截至」次日继续，最终一次事务提交。
- 演算先在暂存区完成并检测车辆冲突，全部成功才写绿化台账、巡查待办、车辆排班；
  失败整体回滚，不留重复工单；车辆仅在事务提交后被占用。
- 物候阈值（修剪/灌溉间隔、高温/暴雨门槛）按版本管理：`POST /versions/adjust` 发布新口径后，
  未开始与已调整未执行任务按新口径重算（旧任务标「已迁移」并释放车辆），
  已执行记录保留当时口径版本与阈值快照。
- 高温日灌溉改清晨时段、修剪顺延；暴雨日暂停灌溉（任务取消、不派车）、其他作业顺延，
  统一挂「应急管制」优先级。现场调整保留计划基线、实际字段另存；完成登记按**实际完成时间**
  写台账并回写区域上次修剪/浇水，同时闭环巡查待办、更新车辆排班。
- 查询接口：`/board`（按日分组的计划板）、`/batches`、`/versions`、`/weather`、
  `/ledger`、`/todos`、`/vehicle-schedules`。

