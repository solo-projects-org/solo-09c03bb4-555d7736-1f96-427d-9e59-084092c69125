# 水质数仓单实例 PITR 恢复服务

面向"值班员误删样品后，要取回删前核对时点数据、同时保留生产库后续写入"的场景。
基于 **FastAPI + PostgreSQL 16 + Docker Compose**：

- 生产源库可写样品库（样品号 / 站号 / pH），开启 **WAL 持续归档**；
- API 自动制作一份 **物理基准备份**（`pg_basebackup`，源库不停写）；
- 可随时登记 **命名核对点**（`pg_create_restore_point`）；
- 按核对点 **PITR 恢复到独立只读实例** 并查询样品；
- 恢复结果只含核对点前已提交的数据，**跨点后才提交的事务不会出现**；
- 查询只打恢复实例，**绝不用当前源库冒充恢复结果**；
- WAL 未归档齐备时任务显示等待、不开放目标查询；
- 失败或 API 重启后可按原核对点重试；一次只恢复一项；全程不改写生产库；
- **脱敏快照**：从已就绪恢复任务的只读库导出全部样品为可下载 JSONL，
  每行仅含样品号/站号各自的稳定匿名标识、pH 与入库时间；
  原号与数据库行号不出现在文件、文件名和任何响应中；
  同一原号跨快照标识不变，两个字段域分隔、同文也不共用标识；
  匿名密钥持久化，重启后继续生效；任务被取代或读取失败绝不发布半份快照，
  发布后的内容不受源库变化、任务取代或服务重启影响。

## 快速开始

```bash
docker compose up -d --build        # 启动源库 + API（首次自动做基准备份）
python3 scripts/demo.py             # 端到端演示：更正 / 误删 / 跨点事务 + 恢复校验
```

Swagger 文档：<http://localhost:8000/docs>

演示脚本依次验证：

| 场景 | 操作 | 恢复校验 |
| --- | --- | --- |
| 更正 | `UPDATE` 修改 S-2026-0002 的 pH | cp1 中仍是旧值 6.95，cp3 中是新值 7.55 |
| 误删 | `DELETE` 删除 S-2026-0003 | cp1 中查得到，cp3 中已删除 |
| 跨点事务 | 事务在 cp3 前 `INSERT`、cp3 后才 `COMMIT` | cp3 恢复结果中**不含**该行 |
| 源库不受打扰 | 恢复进行期间向源库写入新样品 | 写入成功；恢复实例中也看不到该行（证明没拿源库冒充） |

## 架构

```
                 ┌──────────────────────────── compose ───────────────────────────┐
                 │                                                                  │
  写入/变更 ───▶ │  db (PostgreSQL 16, 可写)                                        │
                 │    archive_mode=on ──WAL 段(原子改名)──▶ [wal_archive 卷]         │
                 │                                          │ (只读挂载)             │
                 │  api (FastAPI + PG16 工具)               ▼                       │
                 │    ① pg_basebackup ──▶ [/backups/base]   核对 WAL 段齐备          │
                 │    ② pg_create_restore_point(核对点) ──▶ 源库（仅 WAL 元数据）    │
                 │    ③ 拷贝备份 + recovery.signal ──▶ [/recoveries/job-N]           │
                 │       restore_command 从归档重放 ─▶ pause@核对点 ─▶ 校验 ─▶ 提升  │
                 │    ④ 只读查询 ◀── 恢复实例 127.0.0.1:55433（独立数据目录）        │
                 │                                                                  │
                 └──────────────────────────────────────────────────────────────────┘
```

## 关键机制与需求对照

| 需求 | 实现 |
| --- | --- |
| 物理基准备份 + 持续归档 | `pg_basebackup -Fp -X stream`（自洽含备份期 WAL）；源库 `archive_command` 先写临时文件再原子 `mv`，API 只会看到完整 WAL 段 |
| 登记命名核对点 | `POST /api/checkpoints` → `pg_create_restore_point` + `pg_switch_wal`（促使该段立即归档） |
| 只含核对点前已提交数据 | `recovery_target_name` + `recovery_target_action=pause`，重放精确停在核对点；跨点后提交的事务不重放 |
| 不用源库冒充恢复 | 查询连接的是 `/recoveries/job-N` 下独立实例（端口 55433），响应中标注实例与 as-of LSN |
| WAL 未齐备显示等待 | 核对点 `waiting_wal` 状态；任务逐段核对"备份起点→核对点"全部 WAL 段，缺段时停留 `waiting_wal` 并列出缺失段 |
| 不把半库报成功 | pause 后校验 `pg_last_wal_replay_lsn() >= 核对点 LSN` 才 promote；再查 `samples`  sanity + `default_transaction_read_only=on` 才置 `ready`；任何异常 → `failed` + 日志 |
| 失败/重启可重试 | 任务状态落 SQLite；`POST /api/recoveries/{id}/retry` 按原点重试；API 重启时进行中任务自动重新排队、ready 实例自动拉起 |
| 一次只恢复一项 | 有进行中任务时 `POST /api/recoveries` 返回 409；新任务就绪前会把旧 ready 实例停掉并标记 `superseded` |
| 源库可写、不被改写 | 基准备份/恢复点均为在线只读操作；API 对源库不执行任何 DML；归档卷对 API 只读挂载 |
| 脱敏快照只含匿名标识 | 全量扫描只 `SELECT sample_no, station_no, ph, created_at`（主键行号 `id` 不取）；每行仅输出 `anon_sample_id`/`anon_station_id`/`ph`/`created_at`；文件名只含快照 id，原号不入 SQLite、不进日志与响应 |
| 同号跨快照稳定、字段不共用标识 | HMAC-SHA256 带字段域前缀（`v1:sample_no:` / `v1:station_no:`），两字段同文也产生不同标识；32 字节随机密钥首用时生成并持久化（`/state/anon.key`，0600），重启复用 |
| 不发布半份快照 | 先写隐藏临时文件并 `fsync`，读完全部样品且终检任务仍 `ready` 后才原子 `rename` 并同库置 `published`；生成期间任务变 `superseded`/`stopped` 或读取抛错 → 删临时文件、置 `failed`；同一时刻只允许一个快照在生成 |
| 发布后不可变 | 成品只写一次、放在独立持久卷 `/snapshots`，下载不再连接恢复实例/源库；任务被取代、源库后续变化、API 重启均不影响文件；重启时校验已发布文件仍存在且 SHA-256 与发布时一致，异常如实置 `failed` |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（源库连通性、已归档 WAL 数） |
| POST | `/api/basebackup?force=` | 制作物理基准备份（默认启动时自动做一次） |
| GET | `/api/basebackup` | 备份状态（起始 LSN、大小、历史） |
| POST | `/api/checkpoints` | 登记命名核对点 `{"name": "cp1"}` |
| GET | `/api/checkpoints` `/api/checkpoints/{name}` | 核对点状态（`waiting_wal`/`ready`/`expired`） |
| POST | `/api/recoveries` | 按核对点发起恢复 `{"checkpoint": "cp1"}`（一次一项） |
| GET | `/api/recoveries` `/api/recoveries/{id}` | 任务状态查询 |
| POST | `/api/recoveries/{id}/retry` | 失败/取消/被取代后按原点重试 |
| DELETE | `/api/recoveries/{id}` | 取消进行中任务 / 停止已就绪实例 |
| GET | `/api/recoveries/{id}/samples` | 查询恢复实例样品（`station_no`/`sample_no` 过滤，仅 `ready` 后开放） |
| POST | `/api/recoveries/{id}/query` | 只读 SQL（演示用，限单条 SELECT，连接强制只读兜底） |
| POST | `/api/recoveries/{id}/snapshots` | 对**已就绪**任务生成脱敏 JSONL 快照（后台异步，202 受理） |
| GET | `/api/snapshots` `/api/snapshots/{id}` | 快照列表/状态（可按 `job_id` 过滤；元数据不含原号） |
| GET | `/api/snapshots/{id}/download` | 下载已发布快照（`application/x-ndjson`，文件名只含快照 id） |

### 恢复任务状态机

```
queued → waiting_wal → preparing → recovering → verifying → ready
           (等待WAL归档齐备)                            ↘ failed（可 retry）
ready --(新任务就绪前)--> superseded        ready --(DELETE)--> stopped
进行中 --(DELETE)--> cancelled             API 重启：进行中→重新排队；ready→自动拉起
```

## 手动操作示例（curl）

```bash
# 1. 等基准备份完成
curl -s localhost:8000/api/basebackup | jq .latest_done

# 2. 登记核对点（等 status 变 ready，通常几秒）
curl -s -X POST localhost:8000/api/checkpoints -H 'content-type: application/json' -d '{"name":"cp1"}'
curl -s localhost:8000/api/checkpoints/cp1

# 3. 源库继续变更（更正、误删……）
docker compose exec db psql -U postgres -d water -c "DELETE FROM samples WHERE sample_no='S-2026-0003'"

# 4. 发起恢复并等 ready
curl -s -X POST localhost:8000/api/recoveries -H 'content-type: application/json' -d '{"checkpoint":"cp1"}'
curl -s localhost:8000/api/recoveries/1

# 5. 查询删前时点（S-2026-0003 回来了）
curl -s 'localhost:8000/api/recoveries/1/samples?station_no=ST-02'

# 6. 生成脱敏快照并下载（原号与行号不会出现）
curl -s -X POST localhost:8000/api/recoveries/1/snapshots        # 202，后台生成
curl -s localhost:8000/api/snapshots/1                            # 轮询至 published
curl -s -OJ localhost:8000/api/snapshots/1/download               # 下载 snapshot-1.jsonl
# 每行形如：
# {"anon_sample_id":"32a7…","anon_station_id":"a680…","ph":7.11,"created_at":"2026-11-17T10:00:01+00:00"}
```

### 脱敏快照状态机

```
POST /api/recoveries/{id}/snapshots（仅任务 ready 受理，否则 409/404）
generating ──全部样品读取成功 + 终检任务仍 ready + 原子改名──▶ published（可下载）
     └──── 读取失败 / 任务被取代或停止 / API 重启中断 ────▶ failed（临时文件已清理，无半份）
重启：generating → failed；published → 校验文件与 SHA-256，通过则继续可下载
```

## 限制与说明

- 演示环境口令为 `postgres/postgres`（scram-sha-256，复制连接已在 `db/init/02_hba.sh` 放行），生产使用请更换口令并收敛 pg_hba；
- 归档卷只增不减，生产需自行配置 WAL 保留/清理策略（如 `pg_archivecleanup` + 备份周期）；
- 重做基准备份（`force=true`）会使早于新备份起点的核对点过期（`expired`）；
- 恢复实例监听容器内 55433，仅 API 经本机 socket 访问，不对外暴露；
- `/query` 为演示便利，真正兜底是连接级 `default_transaction_read_only=on`，任何写操作都会被数据库拒绝；
- 匿名密钥存于 `api_state` 卷的 `/state/anon.key`（0600）：丢失该文件会使重新生成的标识全部变化，备份卷时须一并妥善保管；密钥仅用于不可逆 HMAC，无法从标识反推原号；
- 已发布快照存于独立 `snapshots` 卷，只增不删（当前未提供删除接口）；快照全量读取走服务端命名游标分批，内存占用与表大小无关；
- 快照仅可由 `ready` 任务发起；任务一旦被取代（`superseded`）或停止（`stopped`），进行中的快照立即失败，已 `published` 的快照仍可正常下载。

## 已验证项

### 脱敏快照（本次新增，本地 Python 3.11 + 同版本 FastAPI/psycopg 实测）

当前环境无 Docker/PostgreSQL，恢复实例读取以替身模拟，其余（SQLite 状态、
原子发布、匿名化、FastAPI 路由、真实 uvicorn 进程与重启）均走真实代码路径：

- 18 项 pytest 全部 PASS：密钥持久化（0600）、同号跨快照/重启标识不变、
  样品号与站号同文不共用标识、密钥不同则标识不同；
- 成品 JSONL 与所有 API 响应经泄露扫描：无原样品号/站号、无 `samples.id` 行号、
  无 `sample_no`/`station_no` 字段名；文件名仅含快照 id；
- 生成途中任务被 `superseded`/`stopped`、读完全部行后发布前被取代、
  读取中途抛错三种场景均置 `failed` 且临时/成品文件零残留，绝不发布半份；
- 同一时刻第二个生成请求返回 409；非 `ready` 任务发起返回 409，未知任务 404，
  未发布快照下载返回 409；
- 真实 uvicorn 进程端到端：发起→轮询→下载→第二份快照逐字节一致；
  杀掉进程重启后已发布文件逐字节不变、中断的 `generating` 自动转 `failed`、
  重启后新快照因密钥持久化仍与重启前逐字节一致；已发布快照在任务被取代后仍可下载；
- Swagger `/docs` 与 OpenAPI 正常包含全部快照路由，`compose.yaml` YAML 校验通过。

### PITR 主流程（PostgreSQL 16.10 容器实测，沿用未改动）

- 冷启动 → 自动基准备份 → `scripts/demo.py` 全部 10 项断言 PASS（更正/误删/跨点事务/恢复期间源库可写/不拿源库冒充）；
- 归档滞后时核对点与任务停留 `waiting_wal`、目标查询返回 409，归档恢复后任务自动续跑到 `ready`；
- 损坏 WAL 段 → 任务如实 `failed` 并附日志，修复后 `retry` 按原点恢复成功；
- API 优雅重启后 `ready` 实例自动拉起可查；API 被 SIGKILL 于恢复中途（遗留孤儿恢复进程）→ 重启后自动清理并按原点重试至 `ready`；
- 并发第二个恢复请求返回 409；新任务就绪时旧实例被 `superseded`；
- 恢复实例直连写入被数据库拒绝（read-only transaction），`/query` 非 SELECT 与多语句被 400 拦截；
- 强制重做基准备份后旧核对点自动 `expired`，恢复过期核对点返回 409。
