# APS 生产排程业务系统

这是一个可从算法项目中整体拷贝出去、独立部署运行的业务系统。系统自身不导入父项目的任何 Python 模块，通过 HTTP 与 Scheduling Engine Robot 算法服务交互。

详细手册：

- [业务系统部署手册](docs/业务系统部署手册.md)
- [业务系统使用手册](docs/业务系统用户手册.md)

## 已实现的业务能力

- 用户登录与 `admin / planner / approver / viewer` 角色控制；
- 管理员创建、停用、修改角色和重置用户密码；
- 订单、工序、设备、人员、资源组、工厂日历的统一主数据管理；
- 订单、设备、人员、资源组按 `[{}, {}]` 数组批量导入和批量导出；
- 完整 `data_snapshot` 导入、导出、校验和任务快照固化；
- 静态全量、动态滚动、局部微调三种排程任务；
- `task_id` 唯一约束、失败记录和原任务重试；
- 算法健康检查、能力发现和结构化失败处理；
- 算法结果自动形成草稿计划版本；
- 版本审批、驳回、版本差异对比和甘特图查看；
- 审批后发布，自动替代同工艺旧版本并回写订单工序；
- 操作审计日志；
- SQLite 持久化、内置 Web 前端、Docker 部署。

## 系统结构

```text
business_system/
├── business_app/
│   ├── main.py              # API、权限和 Web 入口
│   ├── services.py          # 快照、任务、版本、审批发布业务逻辑
│   ├── algorithm_client.py  # 算法 HTTP 适配器
│   ├── database.py          # SQLite 表结构与初始化
│   ├── security.py          # 密码哈希和签名会话
│   └── static/              # 无需构建的 Web 管理界面
├── seed/demo_snapshot.json  # 首次启动演示数据
├── tests/                   # 自动化业务闭环测试
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── start.ps1 / start.sh
```

## 本地启动

先启动算法服务（在原算法项目根目录）：

```powershell
uvicorn algorithm_service.api:app --host 0.0.0.0 --port 8000
```

再启动本业务系统：

```powershell
cd business_system
.\start.ps1
```

Windows 下也可以直接双击：

```text
启动业务系统.bat
```

该文件默认执行重启，并自动检查虚拟环境、依赖、算法服务连接、8080 端口和业务系统健康状态。

也可以手工启动：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn business_app.main:app --host 0.0.0.0 --port 8080
```

访问 <http://127.0.0.1:8080>，初始账号为：

- 用户名：`admin`
- 密码：`admin123`

首次登录后请通过 `PUT /api/auth/password` 修改密码，并在生产环境设置随机 `SESSION_SECRET`。

## Docker 启动

算法服务在宿主机 `8000` 端口运行时：

```powershell
docker compose up --build -d
```

如算法服务在其他地址：

```powershell
$env:ALGORITHM_BASE_URL="http://algorithm-host:8000"
$env:SESSION_SECRET="替换为足够长的随机字符串"
docker compose up --build -d
```

业务数据库保存在 Docker 命名卷 `aps_business_data` 中。

## 独立拷贝部署

只需复制整个 `business_system` 目录。复制后的系统没有任何 `../` 文件引用。配置算法地址后即可运行：

```powershell
$env:ALGORITHM_BASE_URL="http://10.0.0.20:8000"
python -m uvicorn business_app.main:app --host 0.0.0.0 --port 8080
```

主要环境变量见 `.env.example`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_PATH` | `./data/business.db` | SQLite 数据文件 |
| `ALGORITHM_BASE_URL` | `http://127.0.0.1:8000` | 算法服务根地址 |
| `ALGORITHM_TIMEOUT_SECONDS` | `600` | 单次算法 HTTP 超时 |
| `FACTORY_CODE` | `FACTORY01` | 业务任务编号中的工厂编码 |
| `SESSION_SECRET` | 开发默认值 | 会话签名密钥，生产必须修改 |

## 业务流程

1. 在“主数据中心”维护或导入同一业务版本的订单、资源和日历。
2. 在“排程任务”选择工艺类型、模式、派工规则和算法参数。
3. 业务系统固化本次完整快照，生成唯一 `task_id` 并调用算法服务。
4. 成功结果自动形成 `DRAFT` 计划版本；失败任务可保留原 `task_id` 重试。
5. 审批人查看 KPI、甘特图和明细，执行通过或驳回。
6. `APPROVED` 版本发布后成为当前生产计划，旧发布版本转为 `SUPERSEDED`。
7. 发布结果回写工序计划时间、设备、人员、版本号和计划锁。

## API 概览

- `POST /api/auth/login`：登录；
- `GET /api/dashboard`：运营总览；
- `GET/POST/PUT /api/users`：用户和角色维护；
- `GET/PUT/DELETE /api/master-data/{type}`：主数据；
- `GET /api/master-data/snapshot`：导出算法快照；
- `POST /api/master-data/import`：导入快照；
- `POST /api/tasks`：提交排程任务；
- `POST /api/tasks/{task_id}/retry`：失败重试；
- `GET /api/versions`：计划版本；
- `POST /api/versions/{id}/review`：审批或驳回；
- `POST /api/versions/{id}/publish`：发布和回写；
- `GET /api/versions/compare/{left}/{right}`：版本差异；
- `POST /api/schedule-callbacks`：预留算法异步回调入口；
- `GET /docs`：完整 OpenAPI 文档。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖登录与演示数据初始化、主数据修订、排程任务、结果版本、审批、发布和工序回写。

## 生产化说明

当前版本适合单工厂或中小规模部署。SQLite 已启用 WAL，可支持常规并发读取。若要多实例横向扩容，建议保持 API 与业务服务逻辑不变，将持久层迁移到 PostgreSQL，并把后台任务执行迁移到 RabbitMQ/Celery；算法协议无需改变。异步回调入口应部署在内网或由 API 网关增加来源鉴权。
