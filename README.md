# Church Auto Checkin System

一个基于人脸识别技术的教堂自动签到系统，提供实时签到、人脸库管理、接待工作台和管理后台功能。

中文

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
- 支持带照片优先的选人场景（photo picker）
- 支持签到/完整模式切换与“只看新人”过滤
- 现场异常抓拍归档

### 4. 管理后台
- 会友档案管理（新增、编辑、停用）
- 生日、分组、备注等扩展信息
- 分组下拉选择与移动端列表/报表展示优化
- 签到场次创建与编辑
- 场次归档与历史查询
- CSV 批量导出

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
- 队列负载与 WebSocket 连接状态监控
- 进程级监控指标（CPU、内存、GPU、系统负载）
- 自动清理过期日志与接待记录

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
摄像头输入 → RuntimePipeline → RecognitionEngine → 事件队列
                                    ↓
                            EventDispatcher
                            ↙           ↘
                    WebSocket Channel A  Channel B
                    (事件推送)        (调试视频)
                    ↓                 ↓
                 落库/更新          实时显示
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

### ✅ 已完成阶段

| 阶段 | 关键内容 | 时间 |
|------|---------|------|
| **Phase A** | FastAPI 框架搭建与基础配置 | 2026-03 |
| **Phase B** | 核心数据模型与数据库初始化 | 2026-03 |
| **Phase C** | 多线程摄像头采集、识别引擎、管道 | 2026-03 |
| **Phase D** | WebSocket 双通道与事件分发系统 | 2026-04 |
| **Phase E** | Web UI（调试、接待、管理工作台） | 2026-04 |
| **Phase F** | 人脸库闭环、接待队列、业务完整性 | 2026-04 |
| **Phase G** | 最小测试套件与集成测试 | 2026-04 |
| **Phase H** | 识别链路并发化、Unknown 队列去全局锁、数据孤岛补偿 | 2026-04 |
| **Phase I** | 人脸主键映射收敛、模型简化、历史兼容迁移 | 2026-04 |
| **Phase J** | 会友信息展示、字段收敛、照片增强管理 | 2026-04 |
| **Phase K** | 历史签到迁移入库（11790+ 条历史记录） | 2026-04 |
| **Phase L** | 统计口径修正、时区统一（Europe/Rome）、筛选优化 | 2026-04 |
| **Phase M** | 识别参数调优、接待体验优化、噪声抑制 | 2026-05 |
| **Phase N** | 接待检索增强、语音输入、交互优化 | 2026-05 |
| **Phase O** | 照片选择器与接待页布局优化 | 2026-05 |
| **Phase P** | 接待页交互增强与响应式优化 | 2026-05 |
| **Phase Q** | 关怀中心设计与开发 | 2026-05 |
| **Phase R** | 照片缩略图与报表布局优化 | 2026-05 |

### 📅 当前进度（Phase R 完成）

- ✅ **业务闭环**: MVP 完整，涵盖签到、人脸库、接待、管理全流程
- ✅ **API 就绪**: 20+ 核心接口、双通道 WebSocket 实时推送
- ✅ **测试覆盖**: 400+ 行单元测试 + 386 行集成测试
- ✅ **体验优化**: 
  - 接待工作台降噪处理（每人每天去重、图片压缩）
  - 手动查找会友（拼音、首字母、语音输入）
   - 识别参数调优（阈值 0.70、Margin 0.20）
   - 照片选择器性别筛选修复与接待页响应式优化
   - 状态告警条、签到/完整模式切换与照片选择器分页
- ✅ **图片体验**: 
   - 接待页会友卡片与管理后台会友列表切换为缩略图展示
   - 照片上传、替换、删除链路自动维护缩略图文件
   - 场次与历史报表签到记录支持缩略图展示
- ✅ **关怀中心**: 
   - 关怀成员检索（含拼音/首字母匹配）
   - 风险等级中文化展示（低/中/高）与公式说明
   - 成员画像、分群与报告导出能力
- ✅ **管理后台体验**:
   - 会友分组输入支持下拉选择与手动输入
   - 移动端历史签到改为卡片式展示
- ✅ **数据支持**: 历史签到已入库（11790+ 行），完整统计数据看板
- ✅ **系统诊断**: 完整的自检诊断与日志清理机制

### 2026-06 进展

- ✅ **运维稳定性**: Channel A/B 广播增加超时保护，慢连接不会拖垮事件分发。
- ✅ **运行时监控**: 状态看板新增队列负载卡片，可直接查看帧队列与事件队列占用比。
- ✅ **调试控制**: 调试页新增运行时重启按钮、调试视频开关与识别标注开关，方便隔离调试流量。
- ✅ **链路瘦身**: `check_in` 事件出站前移除 base64 图像字段，降低 Channel A 传输成本。
- ✅ **调试标注**: 调试画面叠字改用成员名称显示，并保留相似度与分段颜色提示。

### 📌 后续计划

**工程化完善**
- [ ] CI/CD 自动化（单元测试 + 集成测试自动运行）
- [ ] 容器化部署（Docker + Compose）
- [ ] 健康检查与告警机制完善

**功能扩展**
- [ ] 多摄像头支持与负载均衡
- [ ] 按成员分组/时段维度的统计与告警
- [ ] 人脸识别精度模型微调与 A/B 测试
- [ ] 性能监控仪表板

**业务安全**
- [ ] 接入鉴权与角色权限（Reception / Admin / Ops）
- [ ] 数据访问控制与审计日志
- [ ] 敏感操作二次确认机制

**用户体验**
- [ ] 国际化支持（英文、繁体中文）
- [ ] 移动端响应式设计
- [ ] 深色主题支持
- [ ] 批量导入会友功能

---

## 项目结构

```
church-auto-checkin-system/
├── app/
│   ├── api.py                      # CompreFace 客户端
│   ├── config.py                   # 全局配置
│   ├── main.py                     # FastAPI 应用入口 + 生命周期
│   ├── core/
│   │   ├── config.py               # 配置加载
│   │   ├── database.py             # SQLAlchemy 设置与会话
│   │   ├── process_metrics.py      # 进程与GPU监控指标
│   │   ├── websocket_manager.py    # WebSocket 管理器
│   │   ├── service_event.py        # 事件定义
│   │   └── time_utils.py           # 时间工具函数
│   ├── models/
│   │   └── models.py               # SQLAlchemy ORM 模型
│   ├── schemas/
│   │   ├── member.py               # 会友 Pydantic Schema
│   │   ├── attendance.py           # 签到 Schema
│   │   ├── care.py                 # 关怀中心 Schema
│   │   ├── event.py                # 场次 Schema
│   │   ├── face_library.py         # 人脸库 Schema
│   │   └── ...                     # 其他 Schema
│   ├── routers/
│   │   ├── index.py                # 主页与 WebSocket 入口
│   │   ├── members.py              # 会友 CRUD API
│   │   ├── attendance.py           # 签到与历史 API
│   │   ├── care.py                 # 关怀中心 API
│   │   ├── events.py               # 场次管理 API
│   │   ├── face_library.py         # 人脸库 API
│   │   ├── reception_queue.py      # 接待队列 API
│   │   ├── debug.py                # 调试工作台路由
│   │   ├── admin.py                # 管理后台路由
│   │   ├── health.py               # 健康检查
│   │   ├── system.py               # 系统控制 API
│   │   ├── websocket.py            # WebSocket 路由
│   │   └── ...
│   ├── services/
│   │   ├── camera_service.py       # 摄像头采集线程
│   │   ├── recognition_engine.py   # 识别引擎（投票、去重）
│   │   ├── runtime_pipeline.py     # 摄像头→识别 管道
│   │   ├── event_dispatcher.py     # 事件消费与分发
│   │   ├── face_library_service.py # 人脸库同步
│   │   ├── cleanup_service.py      # 日志清理任务
│   │   └── ...
│   ├── static/
│   │   ├── index.html              # 主页 UI
│   │   ├── reception.html          # 接待工作台
│   │   ├── debug.html              # 调试工作台
│   │   └── admin.html              # 管理后台
│   └── __init__.py
├── scripts/
│   ├── migrate_*.py                # 数据迁移脚本
│   ├── recover_*.py                # 数据恢复脚本
│   └── ...
├── tests/
│   ├── conftest.py                 # Pytest 配置与 Fixtures
│   ├── test_api_minimal.py         # 最小 API 测试
│   ├── test_recognition_engine_logic.py  # 识别引擎单元测试
│   ├── test_event_dispatcher_websocket_integration.py  # 集成测试
│   └── ...
├── data/
│   ├── face_gallery/               # 本地人脸库存储
│   ├── history/                    # 历史数据与备份
│   ├── backups/                    # 数据库备份
│   └── checkin.db                  # SQLite 数据库文件
├── records/
│   └── camera_attendance.csv       # 签到记录 CSV
├── config.ini                      # 配置文件（git 忽略）
├── requirements.txt                # Python 依赖
├── docs/
│   └── DEVELOPMENT_PROGRESS.md     # 开发进度记录
├── MAINTENANCE.md                  # 维护与部署指南
├── README.md                       # 本文件
└── LICENSE                         # MIT License
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

### Systemd 服务管理
```bash
# 启动/停止服务
sudo systemctl start checkin-system
sudo systemctl stop checkin-system
sudo systemctl restart checkin-system

# 查看日志
sudo journalctl -u checkin-system -f
```

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

---

## 许可证

本项目采用 **MIT License** 开源。详见 [LICENSE](./LICENSE) 文件。

---

## 联系方式

如有任何问题或建议，欢迎通过以下方式联系：
- 提交 GitHub Issues

---

**最后更新**: 2026-06-06  
**维护者**: Weilai Xu@Rugiada
**仓库**: [github.com/ffffuturexu/church-auto-checkin-system](https://github.com/ffffuturexu/church-auto-checkin-system)