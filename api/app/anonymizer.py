"""稳定匿名标识：HMAC-SHA256（域分隔）+ 持久化随机密钥。

设计要点：
- 密钥在首次使用时生成并写入持久化卷上的密钥文件（0600），
  服务重启、重复发布快照都复用同一密钥，因此同一原号跨快照标识稳定；
- 样品号与站号即使文本相同也绝不共用标识：HMAC 输入带字段域前缀，
  两个域使用不同前缀，无法推出两者同文；
- 只输出不可逆的伪随机摘要，原号不进入任何响应、文件名与日志。
"""
import hashlib
import hmac
import os
import secrets
import threading

from . import config

# 字段域分隔前缀：两个字段命名空间互相独立
_DOMAIN_SAMPLE = b"v1:sample_no:"
_DOMAIN_STATION = b"v1:station_no:"

_lock = threading.Lock()
_key: bytes | None = None


def _load_or_create_key() -> bytes:
    """读取持久化密钥；不存在则生成 32 字节随机密钥并原子落盘。"""
    global _key
    if _key is not None:
        return _key
    with _lock:
        if _key is not None:
            return _key
        path = config.ANON_KEY_FILE
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            if len(data) < 32:
                raise RuntimeError(
                    f"匿名密钥文件 {path} 长度不足，拒绝以弱密钥生成标识")
            _key = data
            return _key
        # 先写临时文件再原子改名，保证任何时刻读到的都是完整密钥
        new_key = secrets.token_bytes(32)
        tmp = f"{path}.tmp-{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, new_key)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
        os.chmod(path, 0o600)
        _key = new_key
        return _key


def _digest(domain: bytes, raw: str) -> str:
    key = _load_or_create_key()
    mac = hmac.new(key, domain + raw.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def sample_id(sample_no: str) -> str:
    """样品号的稳定匿名标识。"""
    return _digest(_DOMAIN_SAMPLE, sample_no)


def station_id(station_no: str) -> str:
    """站号的稳定匿名标识（与样品号命名空间独立）。"""
    return _digest(_DOMAIN_STATION, station_no)
