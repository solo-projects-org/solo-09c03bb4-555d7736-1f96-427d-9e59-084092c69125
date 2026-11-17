"""脱敏快照生成与发布。

流程与保证：
- 仅针对 status='ready' 的恢复任务，从其独立只读恢复实例流式读取全部样品；
- 每行只写四个字段：样品号匿名标识、站号匿名标识、pH、入库时间；
  原号、数据库行号（samples.id）不查询、不输出，文件名只含快照 id；
- 同一原号跨快照稳定（anonymizer 持久化密钥），样品号/站号域分隔不共用标识；
- 先写临时文件并逐行 fsync，全部读完且校验行数后原子 rename 才置 published，
  生成期间任务被取代/停止或读取失败 → 删除临时文件、标记 failed，绝不发布半份；
- 发布后的文件只增不改：不依赖恢复实例与源库，任务被取代、源库变化、
  服务重启都不影响已发布内容。
"""
import hashlib
import json
import os
import threading
from datetime import datetime

from . import anonymizer, config, recoverer, state

# 同一时刻只允许一个快照生成（与恢复任务一样串行，避免对单个恢复实例争抢）
_gen_lock = threading.Lock()


class BusyError(RuntimeError):
    """已有快照正在生成。"""


# ---------------------------------------------------------------- 工具

def file_path(file_name: str) -> str:
    return os.path.join(config.SNAPSHOT_DIR, file_name)


def _tmp_path(snapshot_id: int) -> str:
    return os.path.join(config.SNAPSHOT_DIR, f".snapshot-{snapshot_id}.jsonl.tmp")


def _job_still_ready(job_id: int) -> bool:
    """生成期间的守护条件：来源任务必须仍是 ready。

    被新任务取代 → superseded；被 DELETE 停止 → stopped；
    失败/取消等任何偏离 ready 的状态都立即中止本次发布。
    """
    job = state.one_row("SELECT status FROM jobs WHERE id=?", (job_id,))
    return bool(job and job["status"] == "ready")


def _record(row: dict) -> dict:
    """把一行样品转为脱敏记录：只含匿名标识、pH、入库时间。"""
    created = row["created_at"]
    if isinstance(created, datetime):
        created_text = created.isoformat()
    else:
        created_text = str(created)
    # 源列为 numeric(4,2)，规整为两位小数，避免 float8 的二进制尾数噪声
    return {
        "anon_sample_id": anonymizer.sample_id(row["sample_no"]),
        "anon_station_id": anonymizer.station_id(row["station_no"]),
        "ph": round(float(row["ph"]), 2),
        "created_at": created_text,
    }


# ---------------------------------------------------------------- 发起

def start_snapshot(job_id: int) -> int:
    """登记一条 generating 快照并启动后台线程，返回快照 id。"""
    if not _gen_lock.acquire(blocking=False):
        raise BusyError("已有脱敏快照正在生成，请稍后再试")
    try:
        busy = state.one_row(
            "SELECT id FROM snapshots WHERE status='generating' LIMIT 1")
        if busy:
            raise BusyError(f"已有脱敏快照 #{busy['id']} 正在生成")
        sid = state.execute(
            "INSERT INTO snapshots (job_id, checkpoint_name, status, detail,"
            " created_at) VALUES (?,?,?,?,?)",
            (job_id, _checkpoint_of(job_id), "generating",
             "排队等待生成", state.now()),
        )
    finally:
        _gen_lock.release()
    threading.Thread(target=_run_safely, args=(sid, job_id),
                     name=f"snapshot-{sid}", daemon=True).start()
    return sid


def _checkpoint_of(job_id: int) -> str:
    job = state.one_row("SELECT checkpoint_name FROM jobs WHERE id=?", (job_id,))
    if not job:
        raise RuntimeError("恢复任务不存在")
    return job["checkpoint_name"]


def _run_safely(snapshot_id: int, job_id: int) -> None:
    try:
        generate(snapshot_id, job_id)
    except Exception as e:  # noqa: BLE001 - 兜底：任何意外也不得留下半份
        _fail(snapshot_id, f"快照生成异常: {e}")


# ---------------------------------------------------------------- 生成

def generate(snapshot_id: int, job_id: int) -> None:
    os.makedirs(config.SNAPSHOT_DIR, exist_ok=True)
    tmp = _tmp_path(snapshot_id)
    # 清理可能的历史残留
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass

    # 开跑前再次确认任务仍 ready（防止请求受理后、线程启动前被取代）
    if not _job_still_ready(job_id):
        _fail(snapshot_id, "生成前恢复任务已被取代或停止，快照未发布")
        return

    state.execute(
        "UPDATE snapshots SET detail=? WHERE id=?",
        ("正在从只读恢复实例读取全部样品并脱敏", snapshot_id),
    )
    row_count = 0
    digest = hashlib.sha256()
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for row in recoverer.iter_all_samples(config.SNAPSHOT_FETCH_BATCH):
                # 每批次之间检查取代/停止：发现偏离立即中止，半份文件随后删除
                if row_count % config.SNAPSHOT_FETCH_BATCH == 0 \
                        and not _job_still_ready(job_id):
                    raise RuntimeError("生成期间恢复任务被取代或停止")
                line = json.dumps(_record(row), ensure_ascii=False,
                                   separators=(",", ":"))
                payload = line + "\n"
                f.write(payload)
                digest.update(payload.encode("utf-8"))
                row_count += 1
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001 - 读取失败/被取代：绝不发布半份
        _cleanup_tmp(tmp)
        _fail(snapshot_id, f"读取恢复实例失败或任务已失效，快照未发布: {e}")
        return

    # 读完到发布之间仍可能被取代：终检，不通过则丢弃整份成品
    if not _job_still_ready(job_id):
        _cleanup_tmp(tmp)
        _fail(snapshot_id, "发布前恢复任务被取代或停止，快照未发布")
        return

    # 交叉校验：与独立 count(*) 不一致说明游标读取被静默截断，拒绝发布
    try:
        total = recoverer.count_all_samples()
    except Exception as e:  # noqa: BLE001
        _cleanup_tmp(tmp)
        _fail(snapshot_id, f"发布前行数复核读取失败，快照未发布: {e}")
        return
    if total != row_count:
        _cleanup_tmp(tmp)
        _fail(snapshot_id,
              f"行数不一致：流式读取 {row_count} 行，库内 count(*) 为 {total}，拒绝发布")
        return

    file_name = f"snapshot-{snapshot_id}.jsonl"
    final = file_path(file_name)
    size = os.path.getsize(tmp)
    # 原子改名 + 元数据同事务落库后才是 published；客户端只能看到终态
    os.rename(tmp, final)
    state.execute(
        "UPDATE snapshots SET status='published', file_name=?, row_count=?,"
        " file_size=?, sha256=?, detail=?, error=NULL, published_at=?"
        " WHERE id=?",
        (file_name, row_count, size, digest.hexdigest(),
         f"已发布：{row_count} 行脱敏记录", state.now(), snapshot_id),
    )


def _fail(snapshot_id: int, message: str) -> None:
    _cleanup_tmp(_tmp_path(snapshot_id))
    state.execute(
        "UPDATE snapshots SET status='failed', error=?, detail=? WHERE id=?",
        (message[-2000:], "生成失败，未发布任何文件", snapshot_id),
    )


def _cleanup_tmp(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 重启续接

def resume_after_restart() -> None:
    """API 重启后的快照状态修复。

    - 重启前未发布完的 generating 快照：线程已随进程消失，标记 failed
      并清理临时文件（绝不把半成品当成品）；
    - 已 published 的快照：文件在持久化卷上，逐一核对仍在且哈希不变，
      缺失则如实标记 failed；发布内容本身不受任何源端变化影响。
    """
    for s in state.all_rows(
        "SELECT * FROM snapshots WHERE status='generating'"
    ):
        _cleanup_tmp(_tmp_path(s["id"]))
        state.execute(
            "UPDATE snapshots SET status='failed',"
            " error='API 重启中断了生成，未发布（可重新发起）',"
            " detail='生成失败，未发布任何文件' WHERE id=?", (s["id"],),
        )
    for s in state.all_rows(
        "SELECT * FROM snapshots WHERE status='published'"
    ):
        path = file_path(s["file_name"])
        if not os.path.isfile(path):
            state.execute(
                "UPDATE snapshots SET status='failed',"
                " error='已发布快照文件在存储中缺失' WHERE id=?", (s["id"],),
            )
            continue
        with open(path, "rb") as f:
            actual = hashlib.sha256(f.read()).hexdigest()
        if actual != s["sha256"]:
            state.execute(
                "UPDATE snapshots SET status='failed',"
                " error='快照文件哈希与发布时不一致，拒绝下载' WHERE id=?",
                (s["id"],),
            )
