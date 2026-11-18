# 多源数据采集与RPA自动化平台

面向网页抓取、API接入、增量采集、RPA流程编排、数据校验与采集监控的数据工程平台。

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
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 数据源配置 | `datasource` | 数据源 | 源端编号、连接类型、接入地址 |
| 网页采集规则 | `crawler` | 采集规则 | 规则编号、目标网址、页面选择器 |
| API接入管理 | `apiclient` | API客户端 | 客户端编号、接口名称、请求方法 |
| 增量采集 | `incremental` | 增量游标 | 游标编号、来源标识、增量字段 |
| RPA任务编排 | `robot_task` | RPA任务 | 任务编号、任务名称、页面定位器 |
| 数据接入通道 | `ingestion` | 接入通道 | 通道编号、通道名称、输入格式 |
| 格式解析器 | `parser` | 解析配置 | 解析编号、输入格式、输出格式 |
| 采集结果校验 | `validator` | 校验规则 | 规则编号、校验字段、校验类型 |
| 采集去重 | `dedup` | 去重策略 | 策略编号、输入来源、去重键 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联任务、调度周期 |
| 采集监控 | `monitor` | 监控面板 | 面板编号、采集源总数、今日采集量 |
| 采集日志 | `log_viewer` | 日志条目 | 日志编号、关联任务、日志级别 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、凭证类型、关联数据源 |
| 采集转换管道 | `transform` | 转换节点 | 节点编号、输入来源、输出目标 |
| 数据流转编排 | `flow` | 流转管道 | 管道编号、管道名称、起始节点 |
| 数据血缘追踪 | `lineage` | 血缘关系 | 关系编号、上游节点、下游节点 |
| 采集报告 | `report` | 周期采集报告 | 报告编号、统计周期、总采集量、成功量、失败量、平均耗秒 |

## 周期采集报告

采集报告不再用随机指标填充成功量、失败量、平均耗时，而是按统计周期汇总采集记录：

- **唯一数据源**：`_collect_records`（采集记录）与 `_failure_events`（失败事件），
  报告里每一条失败都能回到具体的采集记录或事件编号。
- **统计周期**：支持 `2026-W45`（ISO 周）、`2026-11`（整月）、`2026-11-09`（单日）、
  `2026-11-09~2026-11-15`（日期段）。周期边界按半开区间处理，同一条记录只落一个周期。
- **可复核快照**：同一周期反复生成、重新生成、隔天再打开，成功量/失败量/平均耗时都不会变；
  来源数据没变化时保持原版本，变化时追加修订版本。
- **竞态**：同一 `(统计周期, 采集源)` 只允许一条报告，并发创建只有一个成功，后到请求收到冲突提示。
- **失败事件归属**：
  - `event_time`：事件时间决定修订哪个原周期，迟到事件按发生时间回改原报告；
  - `report_time`：上报（报告生成）时间决定进入下一周期，原周期的记录整体迁出，避免重复计数。
  - 两种口径都保证「成功量 + 失败量 + 未结算量 + 迁移出记录数 = 周期内采集记录数」。
- **未归因失败**：没有对应采集记录的失败事件单独计入失败量，并标记异常，绝不会算作成功。
- **历史可查**：每次修订都把上一版摘要、失败明细、核对依据留档，
  `GET /api/report/{id}/versions/{version}` 可回查修复前的原依据。
- **采集监控**：消费周期报告并持久化（`_monitor_reports`），把修订版报告的失败增量同步到
  趋势（`_monitor_trends`，一个报告版本一个趋势点），核对差异回写报告并进入待办工作台
  （`_workbench_todos`）。重复消费同一版本不会重复生成趋势点或待办。

### 相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/report` | 周期报告列表（可按统计周期筛选） |
| POST | `/api/report/period` | 按统计周期生成报告（同周期重复/并发创建返回冲突） |
| POST | `/api/report/{id}/actions` | 重新生成（来源未变化保持原版本） |
| GET | `/api/report/{id}` | 周期总量、失败明细、异常与修订记录 |
| GET | `/api/report/{id}/versions/{version}` | 回查历史版本摘要与当时的失败明细 |
| GET/POST | `/api/report/events` | 失败事件清单与登记（按归属策略触发报告修订） |
| POST | `/api/report/{id}/sync` | 采集监控回写核对结果 |
| POST | `/api/monitor/reports/consume` | 消费周期报告、同步趋势、生成待办 |
| GET | `/api/monitor/trend` | 报告版本一一对应的监控趋势 |
| GET | `/api/monitor/reports` | 已持久化的报告消费结果 |
| GET/POST | `/api/monitor/todos`、`/api/monitor/todos/{id}/actions` | 待办工作台 |


## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
