"""Optional screenshots of synthetic Tk windows only; never the whole desktop."""

import ctypes
import os
import struct
import sys
from pathlib import Path


def save_mock_window(root, name):
    folder = os.environ.get("CARDPUTER_GUI_SCREENSHOTS")
    if not folder or sys.platform != "win32":
        return
    from ctypes import wintypes

    root.update()
    user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    for api, args, result in (
        (user.GetWindowDC, [wintypes.HWND], wintypes.HDC),
        (user.GetWindowRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
        (user.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
        (user.PrintWindow, [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
        (gdi.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
        (gdi.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
        (gdi.SelectObject, [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
        (gdi.DeleteObject, [wintypes.HANDLE], wintypes.BOOL),
        (gdi.DeleteDC, [wintypes.HDC], wintypes.BOOL),
        (
            gdi.GetDIBits,
            [
                wintypes.HDC,
                wintypes.HBITMAP,
                wintypes.UINT,
                wintypes.UINT,
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.UINT,
            ],
            ctypes.c_int,
        ),
    ):
        api.argtypes, api.restype = args, result
    hwnd = int(root.frame(), 0)
    rectangle = wintypes.RECT()
    user.GetWindowRect(hwnd, ctypes.byref(rectangle))
    width, height = rectangle.right - rectangle.left, rectangle.bottom - rectangle.top
    if not 1 <= width <= 3000 or not 1 <= height <= 2000:
        return
    dc = user.GetWindowDC(hwnd)
    memory = gdi.CreateCompatibleDC(dc)
    bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
    old = gdi.SelectObject(memory, bitmap)
    try:
        if not user.PrintWindow(hwnd, memory, 2):
            return
        size = width * height * 4
        pixels = ctypes.create_string_buffer(size)
        header = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0, size, 0, 0, 0, 0)
        info = ctypes.create_string_buffer(header)
        gdi.SelectObject(memory, old)
        if gdi.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) != height:
            return
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".bmp")).write_bytes(
            struct.pack("<2sIHHI", b"BM", 54 + size, 0, 0, 54) + header + pixels.raw
        )
    finally:
        gdi.SelectObject(memory, old)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory)
        user.ReleaseDC(hwnd, dc)
