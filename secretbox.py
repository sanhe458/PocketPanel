"""本地小数据量加密：HMAC-SHA256 计数器模式 + Encrypt-then-MAC。

为什么不用 AES：项目在 Termux 上是零第三方依赖的，`cryptography` 要编译，
`pycryptodome` 也得装 wheel。API Key 都只有几十字节，用 HMAC 做 PRF 生成
密钥流完全够用——这是标准的 PRF-based stream cipher，安全性取决于
`data/secret.key` 的保密性（0600），和 AES 方案同一个前提。

格式：`base64url(MAGIC | nonce(16) | tag(16) | ciphertext)`，不含明文长度。
每次加密都换随机 nonce；解密用常量时间比较 tag，篡改任意一位都会失败。
"""
import base64
import hashlib
import hmac
import os
import threading

MAGIC = b"PPB1"
NONCE = 16
TAG = 16
KEY_LEN = 32

_key_lock = threading.Lock()
_key_cache = {}


def _derive(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, label, hashlib.sha256).digest()


def load_key(path: str) -> bytes:
    """读 `secret.key`；不存在就原子创建一份 0600 的。

    缓存按路径存，测试里换目录不会串。文件比 32 字节短或读失败都直接报错，
    不静默重建——重建会让已存的 Key 全部解不开，必须让用户明确知道。
    """
    path = os.path.abspath(path)
    with _key_lock:
        hit = _key_cache.get(path)
        if hit:
            return hit
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        if not os.path.exists(path):
            raw = os.urandom(KEY_LEN)
            tmp = path + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        with open(path, "rb") as fh:
            raw = fh.read()
        if len(raw) < KEY_LEN:
            raise ValueError(f"{path} 内容异常（{len(raw)} 字节），应为 {KEY_LEN} 字节")
        raw = raw[:KEY_LEN]
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _key_cache[path] = raw
        return raw


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + counter.to_bytes(8, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def encrypt(master: bytes, plaintext: bytes) -> str:
    if not isinstance(plaintext, (bytes, bytearray)):
        plaintext = str(plaintext).encode()
    nonce = os.urandom(NONCE)
    enc_key = _derive(master, b"pocketpanel/ai/enc")
    mac_key = _derive(master, b"pocketpanel/ai/mac")
    ks = _keystream(enc_key, nonce, len(plaintext))
    ct = bytes(a ^ b for a, b in zip(plaintext, ks))
    tag = hmac.new(mac_key, MAGIC + nonce + ct, hashlib.sha256).digest()[:TAG]
    return base64.urlsafe_b64encode(MAGIC + nonce + tag + ct).decode("ascii")


def decrypt(master: bytes, token: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except Exception as e:  # noqa: BLE001
        raise ValueError("密文格式错误") from e
    if len(raw) < len(MAGIC) + NONCE + TAG or raw[:4] != MAGIC:
        raise ValueError("密文格式错误")
    nonce = raw[4:4 + NONCE]
    tag = raw[4 + NONCE:4 + NONCE + TAG]
    ct = raw[4 + NONCE + TAG:]
    mac_key = _derive(master, b"pocketpanel/ai/mac")
    want = hmac.new(mac_key, MAGIC + nonce + ct, hashlib.sha256).digest()[:TAG]
    if not hmac.compare_digest(tag, want):
        raise ValueError("密文校验失败（secret.key 可能已更换）")
    enc_key = _derive(master, b"pocketpanel/ai/enc")
    ks = _keystream(enc_key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))


def encrypt_str(master: bytes, text: str) -> str:
    return encrypt(master, (text or "").encode("utf-8"))


def decrypt_str(master: bytes, token: str) -> str:
    return decrypt(master, token).decode("utf-8")


def mask(secret: str, keep: int = 4) -> str:
    """给界面看的掩码：保留尾部 4 位，前面固定 8 个点，不泄露长度。"""
    s = secret or ""
    if not s:
        return ""
    if len(s) <= keep:
        return "•" * len(s)
    return "••••••••" + s[-keep:]
