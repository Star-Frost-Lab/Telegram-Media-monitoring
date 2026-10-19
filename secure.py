"""本地凭证加密：DPAPI 打底，口令当第二把锁。

为什么不用浏览器：木马以你的身份运行时浏览器 profile 一样读得到 —— Google 自己承认 DPAPI/ABE
挡不住同用户恶意程序，而且 ABE 只管 cookies、几周内就被 stealer 绕过了。官方 WebK/WebA 也不信
浏览器存储，另加了一层 passcode lock。这里做的是同一件事。

两档：
  不带口令 → CryptProtectData 绑当前 Windows 用户，重启自动解开，零操作
  带口令   → 口令经 scrypt 派生后当 DPAPI 的次要熵（pOptionalEntropy）；没有口令解不开

挡不住什么（别误会）：服务运行中、木马在你已解锁时下手 —— 它能读进程内存，也能直接调本机 API。
真正兜底的是「设置 → 设备 → 终止其他会话」。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import hashlib
import os

CRYPTPROTECT_UI_FORBIDDEN = 0x01        # headless 服务：任何情况都别弹窗
SCRYPT_N = 1 << 15                      # 32 MB 内存、约 100 ms —— 离线爆破要付这个代价
SCRYPT_MAXMEM = 64 << 20


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(_Blob), wt.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob)]
_crypt32.CryptProtectData.restype = wt.BOOL
_crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
    ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_Blob)]
_crypt32.CryptUnprotectData.restype = wt.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _blob(data):
    if not data:
        return _Blob(0, None), None
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(fn, data, entropy):
    din, _keep = _blob(data)            # 这两个 buffer 得活到调用结束，别让 GC 提前收走
    ent, _keep2 = _blob(entropy)
    out = _Blob()
    ok = fn(ctypes.byref(din), None, ctypes.byref(ent) if entropy else None,
            None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise OSError(ctypes.get_last_error(), "DPAPI 解不开（换了 Windows 账户，或口令不对）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(out.pbData)


def seal(data, entropy=None):
    return _dpapi(_crypt32.CryptProtectData, data, entropy)


def unseal(blob, entropy=None):
    return _dpapi(_crypt32.CryptUnprotectData, blob, entropy)


def scrypt_entropy(passcode, salt):
    """口令 → DPAPI 次要熵。没有它，同用户木马调 CryptUnprotectData 也解不开。"""
    return hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=SCRYPT_N, r=8, p=1,
                          dklen=32, maxmem=SCRYPT_MAXMEM)


def new_salt():
    return os.urandom(16)


def b64e(data):
    return base64.b64encode(data).decode("ascii")


def b64d(text):
    return base64.b64decode(text or "")


class Sealer:
    """一个进程一份的密封器。不传口令就只用 DPAPI，传了口令就再加一把 scrypt 锁。"""

    def __init__(self, passcode=None, salt=None):
        self._entropy = scrypt_entropy(passcode, salt) if passcode else None

    @property
    def by_passcode(self):
        return self._entropy is not None

    def seal(self, data):
        return seal(data, self._entropy)

    def unseal(self, blob):
        return unseal(blob, self._entropy)
