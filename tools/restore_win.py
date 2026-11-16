import ctypes, ctypes.wintypes as wt, sys, time

user32 = ctypes.windll.user32
pid = int(sys.argv[1])

def find_win(hwnd, lparam):
    _, proc = ctypes.wintypes.DWORD(), ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc))
    if proc.value == pid and user32.IsWindowVisible(hwnd):
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if title.value:
            ctypes.windll.kernel32.CloseHandle(0)
            user32.PostMessageW(hwnd, 0x0013, 0, 0)  # not needed
            # restore if minimized then foreground
            user32.ShowWindowAsync(hwnd, 9)  # SW_RESTORE
            time.sleep(0.3)
            user32.SetForegroundWindow(hwnd)
            print("restored:", title.value)
            sys.exit(0)
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
user32.EnumWindows(WNDENUMPROC(find_win), 0)
print("window not found")
