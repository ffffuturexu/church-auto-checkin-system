# Church Auto Checkin System

一个基于人脸识别技术的教堂自动签到系统，提供实时签到、人脸库管理、接待工作台和管理后台功能。

中文

English: [README.md](./README.md)

## 📋 目录

- [简介](#简介)
- [主要功能](#主要功能)
- [系统架构](#系统架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [开发进度](#开发进度)
- [项目结构](#项目结构)
- [API 概览](#api-概览)
- [维护和部署](#维护和部署)
- [许可证](#许可证)

---

## 简介

**Church Auto Checkin System** 是一套为教堂设计的智能签到解决方案。通过实时摄像头直播和 CompreFace 人脸识别引擎，自动识别与会者身份并记录签到信息，同时为接待同工和事工管理提供高效的工作台界面。

核心特性：
- ✅ **实时人脸识别签到** - 多摄像头支持，毫秒级响应
- ✅ **人脸库闭环管理** - 成员照片上传、编辑、删除与 CompreFace 同步
- ✅ **接待队列管理** - 智能处理未知人脸，支持现场补签
- ✅ **关怀中心分析** - 风险分层、分组分布、会友画像与 CSV 导出
- ✅ **签到历史与报表** - 灵活的数据查询和 CSV 导出
- ✅ **多角色工作台** - 调试工作台、接待工作台、管理后台
- ✅ **WebSocket 实时推送** - 双通道事件与视频帧同步

---

## 主要功能

### 1. 实时签到引擎
- 支持 RTSP 和 FFmpeg TCP 流媒体输入
- 多帧投票机制，提高识别准确率
- 阈值 + Margin 双重防护，减少误识别
- 自动去重，同场次仅记录一次签到
- 非主日自动忽略，主日下午 3 点后切换意语场次
- 识别链路并发处理，降低高峰时段阻塞
- 在签到成功或待复核判定前自动过滤停用或不存在的会友候选，避免误签到
- 消费者隔离单事件异常并自动重启；数据库写入在线程中串行执行，WebSocket 广播与调试帧独立处理

### 2. 人脸库管理
- 会友照片本地存储与版本管理
- 与 CompreFace 远程库自动同步
- 支持缩略图生成与按需下载，减少大图传输开销
- 支持批量修改和删除操作
- 同步失败自动重试机制

### 3. 接待工作台
- 实时展示签到动态
- 未知人脸队列（包含相似度分析）
- 支持搜索成员后关联补签
- 支持姓名、中文名、拼音、首字母和性别筛选的快速检索
- 支持带照片优先的选人场景（photo picker），并可按“已签到 / 未签到”筛选，返回有照片和无照片的符合条件成员
- 选择“已签到”时，按所选场次的签到时间倒序显示，最近签到者优先
- 支持签到/完整模式切换与“只看新人”过滤
- 现场异常抓拍归档
- 签到记录区分人脸识别、接待台搜索、待处理队列和历史回填四类业务来源

### 4. 管理后台
- 会友档案管理（新增、编辑、停用）
- 生日、分组、备注等扩展信息
- 分组下拉选择与移动端列表/报表展示优化
- 签到场次创建与编辑
- 场次归档与历史查询
- CSV 批量导出
- 新增会友支持性别、固定分组选项和拼音/外文名校验；创建成功后可自动完成当前主日场次签到

### 5. 关怀中心
- 会友关怀列表（支持状态/是否有照片/性别/分组/关键词筛选）
- 风险等级评估（低/中/高）与风险值解释
- 会友关怀画像（近月签到、连续主日缺席、趋势与近期记录）
- 关怀群组与整体报告（分组分布、参与度分布、风险分布）
- 关怀名单 CSV 导出（中文业务字段）

### 6. 系统控制
- 流媒体启动/停止
- 超参数热更新（阈值、Margin、投票数等）
- 自检诊断（数据库、运行时、WebSocket 等）
- 调试视频开关、识别标注开关与运行时重启
- 调试画面中文姓名标注已改用 CJK 字体渲染，避免出现 `????`
- 调试台超参数区将“当前参数展示”和“编辑输入”分离，并提供一键恢复默认参数
- 队列负载与 WebSocket 连接状态监控
- 进程级监控指标（CPU、内存、GPU、系统负载）
- 摄像头出帧、RTSP/TCP 链路和常见解码异常轮转日志
- 自动清理过期日志与接待记录
- 全部主要工作台支持中文/英文切换，并记住浏览器中的语言选择

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                        Web UI Layer                         │
├──────────┬──────────────┬──────────────┬────────────────────┤
│  Index   │  Reception   │  Debug       │  Admin             │
└──────────┴──────────────┴──────────────┴────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (8001)                   │
├──────────────────────────────────────────────────────────────┤
│  REST API Routers  │  WebSocket Handlers  │  Health Checks  │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                   Core Services Layer                       │
├──────────────────────────────────────────────────────────────┤
│ RuntimePipeline (摄像头→识别管道) ← 多线程异步处理           │
│ RecognitionEngine (独立识别线程池)   - 投票/去重/阈值       │
│ CameraService (摄像头采集)         - 帧队列缓冲             │
│ EventDispatcher (事件消费和落库)    - 广播+持久化            │
│ FaceLibraryService (人脸库同步)    - 本地+远端双路维护       │
└──────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│                      SQLite Database                        │
├──────────────────────────────────────────────────────────────┤
│ Members  │  AttendanceRecords  │  AttendanceEvents          │
│ FacePhotos │ UnknownFaceCases  │  RecognitionLogs           │
└──────────────────────────────────────────────────────────────┘
                          ↓
                    ┌─────────────┐
                    │ CompreFace  │ (远程人脸识别服务)
                    │ API (REST)  │
                    └─────────────┘
```

### 关键数据流

```
摄像头输入 → RuntimePipeline → RecognitionEngine
                                ↙          ↘
                     业务事件队列         最新调试帧槽
                           ↓                  ↓
                    EventDispatcher      Channel B 广播
                           ↓
                      串行数据库写入
                           ↓
                    有界广播队列 → Channel A / 接待队列
```

---

## 技术栈

| 组件 | 版本 | 用途 |
|------|------|------|
| **FastAPI** | 0.115.12 | Web 框架 |
| **Uvicorn** | 0.34.0 | ASGI 服务器 |
| **SQLAlchemy** | 2.0.39 | ORM |
| **SQLite** | 内置 | 数据库 |
| **OpenCV** | 4.11.0 | 视频处理与图像编码 |
| **Pillow** | 11.1.0 | 图像处理 |
| **Requests** | 2.32.3 | HTTP 客户端 |
| **Pytest** | 8.3.5 | 测试框架 |
| **CompreFace** | REST API | 人脸识别引擎 |

---

## 快速开始

### 前置要求

- Python 3.9+
- CompreFace 服务（自托管或云服务）
- 摄像头设备或 RTSP 流源

### 安装步骤

1. **克隆仓库**
   ```bash
   git clone https://github.com/ffffuturexu/church-auto-checkin-system.git
   cd church-auto-checkin-system
   ```

2. **创建虚拟环境**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # 或
   venv\Scripts\activate  # Windows
   ```

3. **安装依赖**
   ```bash
   pip install -r requirements.txt
   ```

4. **配置参数**

   创建 `config.ini`：
   ```ini
   [CompreFace]
   ApiKey = your-api-key
   BaseUrl = http://compreface-host:8000

   [Script]
   Threshold = 0.70
   Margin = 0.20
   DedupeSeconds = 60
   FrameSkip = 2
   VoteWindowSec = 1.5
   VoteMinSamples = 5
   VoteRatio = 0.65
   UnknownMinSimilarity = 0.65
   UnknownMinFaceSize = 64
   MaxQueueSize = 3
   PredictionCount = 5

   [DataSource]
   RTSP_URL = rtsp://camera-url
   CameraIndex = 0
   RtspTcp = true

   [GUI]
   Preview = true
   ```

5. **初始化数据库**
   ```bash
   python -c "from app.core.database import init_db; init_db()"
   ```

6. **启动服务**
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
   ```

7. **访问应用**
   - 主页: http://localhost:8001/
   - 接待工作台: http://localhost:8001/reception
   - 调试工作台: http://localhost:8001/debug
   - 管理后台: http://localhost:8001/admin

---

## 开发进度

### 月度进度概览

| 日期 | 版本号 | 主要更新内容 |
|------|------|------|
| 2026-11 | v0.7.2 | 增加摄像头状态、RTSP/TCP 连接指标和常见 OpenCV/FFmpeg 异常的轮转诊断日志。 |
| 2026-11 | v0.7.1 | 事件消费者异常隔离与自动重启、数据库与广播解耦、调试帧独立通道，以及队列和发送指标与告警。 |
| 2026-11 | v0.6.2 | 新增排班负责人演示页，支持本周排班筛选、岗位缺口查看、调整请求审核及首页入口；同步完善调试台说明与中英文界面文案。 |
| 2026-09 | v0.6.1 | 签到来源模型与 SQLite 自动迁移、停用会友识别过滤、管理后台新增会友自动签到、中文/英文界面切换及相关回归测试。 |
| 2026-08 | v0.5.5 | 接待页 photo picker 的“已签到”结果按所选场次签到时间倒序显示，并补充排序回归测试。 |
| 2026-07 | v0.5.3 | 新增每周关怀简报生产化链路：模板化周报生成与 SMTP 发送脚本、`weekly_report_logs` 日志模型、后台周报接口与管理页查看/触发能力、systemd 定时发送配置、周报测试用例。 |
| 2026-07 | v0.5.2 | 接待页补签场次修正与 photo picker 签到状态筛选增强。 |
| 2026-07 | v0.5.1 | 识别引擎健壮性更新：pending 双缓存、编码回退、finalize 时序修正、超参数约束与去重优化。 |
| 2026-06 | v0.4.5 | Pending 候选双缓存、snapshot 编码回退、finalize 时序修正与审计日志补强。 |
| 2026-06 | v0.4.4 | 识别事件模型重构：recognition.success / pending / unknown，兼容旧 check_in / unknown_face 映射。 |
| 2026-06 | v0.4.3 | 生日提醒脚本、birthday_reminder_logs 运行日志模型与 systemd 定时器。 |
| 2026-06 | v0.4.2 | 接待页启动优化、状态告警待定态与首屏自动补填。 |
| 2026-06 | v0.4.1 | 调试标注优化、中文姓名 CJK 渲染、超参数展示/编辑拆分。 |
| 2026-06 | v0.4.0 | 运维稳定性增强：Channel A/B 广播超时保护、队列负载看板与调试控制。 |
| 2026-05 | v0.3.2 | 关怀中心设计与开发、风险分层与成员画像。 |
| 2026-05 | v0.3.1 | 照片缩略图机制、报表布局优化与移动端展示优化。 |
| 2026-05 | v0.3.0 | 接待页体验与响应式优化、检索增强与语音输入。 |
| 2026-04 | v0.2.2 | 历史签到迁移入库、时区统一与统计口径修正。 |
| 2026-04 | v0.2.1 | Web UI 雏形、人脸库闭环与接待队列联动。 |
| 2026-04 | v0.2.0 | WebSocket 双通道、事件分发与基础系统看板。 |
| 2026-03 | v0.1.1 | 多线程摄像头采集、识别引擎与运行管道雏形。 |
| 2026-03 | v0.1.0 | FastAPI 基础框架、配置与数据库初始化。 |

### 当前进度摘要

- ✅ **业务闭环**: MVP 已完整覆盖签到、人脸库、接待、管理与关怀中心流程
- ✅ **实时能力**: 双通道 WebSocket 与识别事件分发已可稳定运行
- ✅ **体验优化**: 接待页、调试页与管理后台已完成多轮交互和移动端优化
- ✅ **数据支持**: 历史签到已完成迁移入库，报表与缩略图链路可用
- ✅ **照片查找排序**: 已签到会友按所选场次的最近签到时间优先展示，相关回归测试已在 `autocheckin` 环境通过
- ✅ **签到来源审计**: 历史和实时签到记录提供 `recognition`、`manual_search`、`pending_queue`、`backfill` 来源分类，Dashboard 和 CSV 同步展示
- ✅ **识别安全性**: 停用或不存在的会友不会进入自动签到或待复核流程
- ✅ **界面本地化**: 主页、接待台、管理后台和调试台支持中文/英文切换

### 📌 后续计划

**工程化完善**
- [ ] CI/CD 自动化（单元测试 + 集成测试自动运行）
- [ ] 容器化部署（Docker + Compose）
- [x] 健康检查与运行态诊断基础能力
- [ ] 健康告警机制完善

**功能扩展**
- [ ] 多摄像头支持与负载均衡
- [x] 按成员分组/时段维度统计
- [ ] 按成员分组/时段维度告警
- [ ] 人脸识别精度模型微调与 A/B 测试
- [x] 基础性能监控仪表板

**业务安全**
- [ ] 接入鉴权与角色权限（Reception / Admin / Ops）
- [ ] 数据访问控制与审计日志
- [ ] 敏感操作二次确认机制

**用户体验**
- [x] 中文/英文界面本地化基础能力
- [x] 移动端响应式设计
- [ ] 深色主题支持
- [ ] 批量导入会友功能

---

## 项目结构

```text
church-auto-checkin-system/
├── app/                     # FastAPI 后端、路由、服务与静态页面
├── scripts/                 # 数据迁移、恢复与运维脚本
├── tests/                   # 单元测试与集成测试
├── data/                    # 本地人脸库、历史数据与数据库文件
├── records/                 # 签到记录与导出文件
├── config.ini               # 配置文件（git 忽略）
├── requirements.txt         # Python 依赖
├── docs/DEVELOPMENT_PROGRESS.md
├── docs/MAINTENANCE.md      # 维护与部署指南
├── README.md                # 英文默认文档
├── README.zh-CN.md          # 本文件（中文）
└── LICENSE                  # MIT License
```

---

## API 概览

### 会友管理
- `GET /members` - 列表会友（支持分页与筛选）
- `GET /members?gender=` - 按性别筛选会友
- `POST /members` - 新增会友
- `GET /members/{member_id}` - 获取会友详情
- `PUT /members/{member_id}` - 更新会友信息
- `DELETE /members/{member_id}` - 停用会友
- `GET /members/search?q=` - 快速搜索
- `GET /members/photo-picker` - 接待/选图场景的候选会友列表

### 签到服务
- `GET /attendance/current-service` - 获取当前服务信息
- `POST /attendance/manual-checkin` - 手动签到
- `GET /attendance/history` - 查询签到历史
- `GET /attendance/history/export.csv` - 导出签到记录
- 签到历史和 CSV 返回 `source` 字段：`recognition`（人脸识别）、`manual_search`（接待台搜索）、`pending_queue`（待处理队列）、`backfill`（历史回填）；旧 SQLite 数据库启动时自动补充并回填该字段。

### 场次管理
- `GET /events` - 场次列表（含归档）
- `POST /events` - 创建新场次
- `PUT /events/{event_id}` - 编辑场次
- `POST /events/{event_id}/archive` - 归档场次

### 人脸库
- `GET /face-library/members/{member_id}/photos` - 会友照片列表
- `POST /face-library/members/{member_id}/photos` - 上传照片
- `PUT /face-library/photos/{photo_id}` - 替换照片
- `DELETE /face-library/photos/{photo_id}` - 删除照片
- `GET /face-library/photos/{photo_id}/download` - 下载原图
- `GET /face-library/photos/{photo_id}/thumbnail` - 下载照片缩略图
- `GET /face-library/members/{member_id}/thumbnail` - 下载会友主缩略图
- `POST /face-library/sync/rebuild` - 重建人脸库

### 关怀中心
- `GET /care/members` - 关怀成员列表与风险筛选
- `GET /care/members/{member_id}/profile` - 单个会友关怀画像
- `GET /care/cohorts` - 关怀分群建议
- `GET /care/report` - 关怀统计报告
- `GET /care/members/export.csv` - 关怀名单导出

### 接待队列
- `GET /reception/queue/unknown` - 未知人脸队列
- `POST /reception/queue/unknown/{case_id}/ignore` - 忽略案例
- `POST /reception/queue/unknown/{case_id}/resolve` - 关联补签
- `POST /reception/queue/unknown/clear` - 批量清理

### 系统控制
- `GET /health` - 服务健康检查
- `GET /system/self-check` - 系统自检诊断
- `GET /system/status` - 系统状态与资源指标
- `POST /system/stream/start` - 启动流媒体
- `POST /system/stream/stop` - 停止流媒体
- `PUT /system/hyperparameters` - 热更新超参数
- `PUT /system/debug-video` - 开关调试视频
- `PUT /system/debug-overlay` - 开关识别标注
- `POST /system/restart` - 重启运行时

### WebSocket
- `WS /ws/channel-a` - 实时签到事件（Channel A）
- `WS /ws/channel-b` - 调试视频帧（Channel B）

详见 FastAPI 自动文档：`/docs` 和 `/redoc`

---

## 维护和部署

### 事件处理监控

通过 `/health` 或 `/system/self-check` 检查事件管道：

- `runtime.event_queue_size` / `event_queue_max` 是业务队列深度与容量；`runtime.recognition_stats` 包含 `event_queue_high_water`、`event_drops_by_type` 和 `debug_frames_replaced`。调试帧使用独立最新帧槽，替换旧帧不代表业务事件丢失。
- `event_dispatcher` 包含处理及失败计数、重启次数、广播队列深度及高水位、按类型丢弃数，以及最近和最大处理/广播耗时（秒）。在自检接口中位于 `checks.event_dispatcher.state`。
- WebSocket 状态中的 `send_failures_by_channel` 统计发送失败或超时；自检接口中位于 `checks.websocket.state`。

队列达到 80%、事件丢弃、WebSocket 发送失败和消费者异常重启时记录警告或错误日志；需要持久告警时应接入外部日志或指标监控。计数和高水位属于进程内数据，重启后清零。

业务队列满时丢弃最旧事件，广播队列满时丢弃新通知；两者均不持久化。业务事件处理失败会记录和计数，但不会自动重试，应检查日志并核对记录。广播丢失不会回滚已提交的数据库写入，可刷新接待队列或动态记录恢复状态。正常关闭时先停止生产并排空业务事件，广播最多等待五秒，剩余通知会丢弃并记录。

### 摄像头链路日志

应用启动后，每 10 秒在仓库根目录 `logs/camera.log` 记录摄像头出帧、队列、识别状态、进程资源和摄像头 RTSP 端口的本进程 TCP 连接指标。`rtsp_tcp_requested` 表示配置要求 TCP，不代表已验证实际传输方式；`tcp.connections` 为空也不能单独证明摄像头断网。依赖本机 `ss` 和目标地址解析，无法采集时会在状态中记录错误。

在 systemd 环境下，还通过 `journalctl` 监听本进程的 OpenCV/FFmpeg 日志，将识别出的流超时、常见编码解码和 RTSP 错误写入文件。该过滤列表不是完整原始日志；无 systemd、无 journal 读取权限或命令不可用时，应检查服务日志中的采集器警告。不会记录 RTSP 地址或凭据。日志每 5 MiB 轮转，最多保留当前文件及 5 个旧文件，目录被 Git 忽略。运行用户须有仓库根目录写权限；已运行的服务需要在维护时段重启后才会启用采集。

### Systemd 服务管理
```bash
# 启动/停止服务
sudo systemctl start checkin-system
sudo systemctl stop checkin-system
sudo systemctl restart checkin-system

# 查看日志
sudo journalctl -u checkin-system -f
```

### 生日提醒（关怀中心）

本仓库新增了一个轻量化的生日提醒脚本与可选 systemd 定时器：

- 脚本：`scripts/send_birthday_reminders.py`，可配置同工收件人、SMTP、提前天数（默认当天，`BIRTHDAY_REMINDER_DAYS_AHEAD=0`）。
- 数据库：会在 `birthday_reminder_logs` 表记录每次执行（包括无生日、dry-run 与实际发送），由 ORM 模型 `BirthdayReminderLog` 管理。
- 定时：提供 `scripts/systemd/birthday_reminder.service` 与 `scripts/systemd/birthday_reminder.timer`，在系统侧设置为每天 08:00 触发；脚本仅在当天有会友生日时发送邮件。

部署提示：不要将 SMTP 凭据放入仓库，请将其放在 `/etc/default/birthday_reminder` 或系统级安全存储中。

### Nginx 反向代理
```bash
# 测试配置
sudo nginx -t

# 重新加载
sudo systemctl reload nginx
```

### 常用诊断命令
```bash
# 检查 API 端口
ss -lntp | grep 8001

# HTTP 健康检查
curl -I http://127.0.0.1:8001/health

# WebSocket 连接测试
curl -i -N -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  http://127.0.0.1:8001/ws/channel-a
```

更多维护命令见 [MAINTENANCE.md](./MAINTENANCE.md)

---

## 测试

运行测试套件：
```bash
# 所有测试
pytest tests/ -v

# 特定测试文件
pytest tests/test_api_minimal.py -v

# 包含覆盖率报告
pytest tests/ --cov=app --cov-report=html
```

测试范围：
- ✅ API 功能测试（CRUD、错误处理）
- ✅ 识别引擎逻辑测试（投票、去重、阈值）
- ✅ WebSocket 集成测试（事件推送、持久化）
- ✅ 业务流程测试（签到、补签、场次分割）

v0.6.1 新增签到来源 API 断言，以及停用会友不得进入识别成功/待复核流程的回归测试；建议在 `autocheckin` conda 环境中运行。

---

## 许可证

本项目采用 **MIT License** 开源。详见 [LICENSE](./LICENSE) 文件。

---

## 联系方式

如有任何问题或建议，欢迎通过以下方式联系：
- 提交 GitHub Issues
