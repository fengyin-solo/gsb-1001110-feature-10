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
| 修剪灌溉计划板 | `green_plan` | 计划工单/口径版本/天气/发现/排班 | 区域品种、口径版本、批次键、调度键 |
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

在绿化管养基础上扩展的「按物候周期滚动生成」计划板，把四类输入串成一条生成链路：

```text
区域品种(green_area) ─┐
近期天气(green_weather)─┼─► 物候周期枚举 ─► 高温/暴雨应急覆盖 ─► 巡查发现补充 ─► 作业车辆容量试排
巡查发现(green_finding)┘                                   （窗口顺延、同车同日不重派）
```

- **滚动两周**：以「今天」为基准，按品种物候间隔（修剪/灌溉周期）从最近一次实际作业
  滚动枚举未来 14 天任务；巡查发现按紧急程度补充任务。
- **应急优先**：达到当前口径的高温/暴雨阈值即触发应急管制，作业顺延到管制解除后的首个
  正常作业日，绝不在管制日强行安排。
- **口径版本化**：物候阈值调整生成新生效版本（`green_caliber`），历史版本保留；
  「计划重排」只迁移**未开始**任务（旧工单打已取消、释放车辆与待办、按新口径重新生成），
  **已执行记录保留当时基准版本**，历史修剪按**实际完成时间**留痕。
- **三处同步**：发布结果写入绿化台账（`green_ledger`）、巡查待办（`patrol`）、
  车辆排班清单（`green_vehicle_schedule`）。
- **草稿续发**：发布中断时保留计划草稿（`green_batch` 草稿批次），重连后从未生成时段继续。
- **幂等调度**：按「口径版本@窗口起点」生成批次键；重复调度同一批次只生效一次，不产生重复工单。
- **事务提交**：发布在事务里先建快照，台账→待办→排班全部成功才提交并占用车辆；
  任一步失败整体回滚，不留半成品工单（可用 `force_fail` 模拟中断验证）。

接口前缀 `/api/green/plan`：`GET /draft`（枚举，不落库）、`POST /draft`（保留草稿）、
`POST /publish`（事务发布/幂等/续发）、`POST /recalculate`（口径重排）、
`GET|POST /calibers`（口径版本）、`GET /ledger`、`POST /ledger/{id}/complete`（现场完成留痕）、
`GET /schedule`、`GET /batches`。前端页面在「修剪灌溉计划板」菜单（`/green_plan`）。
