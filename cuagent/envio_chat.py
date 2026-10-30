"""Manda a resposta pro chat do cliente: foca a janela, cola e dá Enter.

Fluxo SEM IA (instantâneo e sem gastar cota):

1. :class:`ClienteTracker` — thread que lembra a ÚLTIMA janela ativa que
   NÃO é a nossa (o "chat do cliente" que o atendente estava usando);
2. :func:`send_text` — traz essa janela pra frente, cola o texto pelo
   clipboard (Ctrl+V — Unicode com acentos, onde ``pyautogui.write``
   falharia), aperta Enter e devolve o clipboard ao dono.

Clipboard: o texto anterior só é devolvido se existir; se não havia
texto (ex.: imagem), a mensagem enviada fica como está — nunca apagamos
às cegas o que não sabemos restaurar.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

import pyautogui

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_RESTORE = 9

# handles/ponteiros em 64 bits: sem restype o ctypes trunca e quebra
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [ctypes.c_uint, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def _pid_of(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    """Título da janela ("" se inexistente/sem título)."""
    if not hwnd:
        return ""
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def foreign_foreground(my_pid: int) -> int:
    """HWND da janela ativa, mas só se NÃO for deste processo (senão 0)."""
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return 0
    if _pid_of(hwnd) == my_pid:
        return 0
    if not window_title(hwnd):        # sem título = desktop/shell demais
        return 0
    return hwnd


class ClienteTracker:
    """Lembra a última janela ATIVA que não é a nossa — o chat do cliente.

    Polling leve (a cada 0,4 s) num thread daemon: quando o atendente
    clica em 📨 Mandar pro cliente, o destino já está guardado aqui (a
    janela do chat — a nossa é ignorada pelo pid).
    """

    def __init__(self, interval: float = 0.4) -> None:
        self.hwnd: int = 0
        self._interval = interval
        self._pid = os.getpid()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                hwnd = foreign_foreground(self._pid)
                if hwnd:
                    self.hwnd = hwnd
            except Exception:
                pass

    def alive(self) -> bool:
        h = self.hwnd
        return bool(h and user32.IsWindow(h) and user32.IsWindowVisible(h))

    def title(self) -> str:
        return window_title(self.hwnd) if self.alive() else ""

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ clipboard

def _open_clipboard(tent: int = 8, pausa: float = 0.1) -> bool:
    """OpenClipboard com tentativas — outro app pode estar com ele aberto."""
    for _ in range(max(1, int(tent))):
        if user32.OpenClipboard(None):
            return True
        time.sleep(pausa)
    return False


def save_clipboard() -> str | None:
    """Texto atual do clipboard (None se não houver CF_UNICODETEXT)."""
    if not _open_clipboard(tent=3):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """Escreve ``text`` no clipboard (Unicode). Devolve se conseguiu."""
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(str(text))
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, ctypes.sizeof(buf))
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, ctypes.addressof(buf), ctypes.sizeof(buf))
        kernel32.GlobalUnlock(handle)
        ok = bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
        if not ok:
            kernel32.GlobalFree(handle)  # só em falha: no sucesso é do sistema
        return ok
    finally:
        user32.CloseClipboard()


# ------------------------------------------- digitação sem clipboard (fallback)

_INPUT_KEYBOARD = 0
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
_VK_RETURN = 0x0D


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT,
                             ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _ev_ki(wVk: int, wScan: int, flags: int) -> _INPUT:
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.u.ki.wVk = wVk
    inp.u.ki.wScan = wScan
    inp.u.ki.dwFlags = flags
    inp.u.ki.dwExtraInfo = 0
    return inp


def type_unicode(text: str) -> bool:
    """Digita ``text`` com SendInput Unicode — NÃO toca no clipboard.

    Caminho de segurança quando o clipboard está ocupado por outro
    programa. ``\\n`` vira Enter de verdade (VK_RETURN), não LF solto.
    """
    try:
        eventos: list[_INPUT] = []
        for ch in str(text):
            if ch == "\n":
                eventos.append(_ev_ki(_VK_RETURN, 0, 0))
                eventos.append(_ev_ki(_VK_RETURN, 0, KEYEVENTF_KEYUP))
                continue
            if ch == "\r":
                continue
            eventos.append(_ev_ki(0, ord(ch), KEYEVENTF_UNICODE))
            eventos.append(_ev_ki(0, ord(ch),
                                  KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        if not eventos:
            return True
        arr = (_INPUT * len(eventos))(*eventos)
        enviado = user32.SendInput(len(eventos), arr,
                                   ctypes.sizeof(_INPUT))
        return int(enviado) == len(eventos)
    except Exception:
        return False


# ---------------------------------------------------------------------- envio

def focus_window(hwnd: int, timeout: float = 1.5) -> bool:
    """Traz a janela pra frente (restaura se estava minimizada)."""
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)
    user32.SetForegroundWindow(hwnd)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.1)
    return int(user32.GetForegroundWindow() or 0) == hwnd


def send_text(hwnd: int, text: str) -> tuple[bool, str]:
    """Foca a janela, cola o texto (Ctrl+V) e aperta Enter.

    Devolve ``(ok, mensagem)`` — a mensagem cita o destino ou o motivo.
    Clipboard devolvido ao dono logo após o Enter (~0,7 s de janela).
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False, "a janela do chat não existe mais (fechou?)"
    if not user32.IsWindowVisible(hwnd):
        return False, "a janela do chat está escondida"
    titulo = window_title(hwnd)
    if not focus_window(hwnd):
        return False, f"não consegui trazer «{titulo}» pra frente"

    salvo = save_clipboard()
    if set_clipboard_text(text):
        time.sleep(0.15)
        try:
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.35)
            pyautogui.press("enter")
            time.sleep(0.2)
        except Exception as exc:        # FailSafe do pyautogui inclusive
            return False, f"falha ao digitar: {exc}"
        finally:
            if salvo is not None:
                set_clipboard_text(salvo)   # devolve o que o usuário tinha
        return True, f"resposta digitada em «{titulo}» (Ctrl+V + Enter)"

    # clipboard travado por outro programa: digita SEM passar por ele
    if not type_unicode(text):
        return False, ("clipboard ocupado por outro app e a digitação "
                       "direta falhou — feche o gerenciador de área de "
                       "transferência e tente de novo")
    time.sleep(0.2)
    try:
        pyautogui.press("enter")
        time.sleep(0.2)
    except Exception as exc:
        return False, f"falha ao digitar: {exc}"
    return True, (f"resposta digitada em «{titulo}» "
                  "(digitação direta — clipboard ocupado)")
