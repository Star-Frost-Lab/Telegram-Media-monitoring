"""一次性验证：把服务进程真的挂起几秒，看它醒来后有没有在日志里认出「我被挂起过」。

系统进待机（Modern Standby）时 Windows 就是这么对待桌面程序的，所以这个测试
等价于「待机发生了一次」。用法：python -X utf8 _tests/_suspend_test.py <pid> [秒数]
"""

import ctypes
import sys
import time
from ctypes import wintypes

PROCESS_SUSPEND_RESUME = 0x0800

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

pid = int(sys.argv[1])
secs = float(sys.argv[2]) if len(sys.argv) > 2 else 10

h = k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
if not h:
    raise SystemExit(f"OpenProcess 失败，错误码 {ctypes.get_last_error()}")
try:
    print(f"挂起 PID {pid}，{secs:.0f} 秒…", flush=True)
    ntdll.NtSuspendProcess(h)
    time.sleep(secs)
    ntdll.NtResumeProcess(h)
finally:
    k32.CloseHandle(h)
print("已恢复", flush=True)
