import ctypes
import ctypes.wintypes as wintypes
import os


def get_process_creation_time_windows(pid: int) -> float | None:
    kernel32 = ctypes.windll.kernel32
    h_process = kernel32.OpenProcess(0x1000, False, pid)
    if not h_process:
        return None
    
    creation_time = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    
    success = kernel32.GetProcessTimes(
        h_process,
        ctypes.byref(creation_time),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time)
    )
    kernel32.CloseHandle(h_process)
    
    if not success:
        return None
        
    time_val = (creation_time.dwHighDateTime << 32) | creation_time.dwLowDateTime
    if time_val == 0:
        return None
        
    return (time_val - 116444736000000000) / 10000000.0

if __name__ == "__main__":
    pid = os.getpid()
    ctime = get_process_creation_time_windows(pid)
    print(f"PID: {pid}, ctime: {ctime}")
