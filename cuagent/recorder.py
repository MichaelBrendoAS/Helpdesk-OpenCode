"""Gravação de rotinas: hooks globais do Windows (ctypes) + pasta rotinas/.

Captura mouse (movimento, botões, rolagem), teclado (todas as teclas) e a
janela ativa enquanto o usuário trabalha — até ele apertar ⏹ Parar. Cada
evento recebe o instante (ms) em que ocorreu, permitindo repetir a rotina
com o mesmo ritmo depois.

Sem dependência externa: usa a API nativa `SetWindowsHookEx` (os mesmos
hooks de nível baixo que bibliotecas como pynput usam por baixo). Um thread
dedicado instala os hooks e roda um loop de mensagens — sem ele o Windows
não entrega os eventos. Cliques/teclas no PRÓPRIO programa são ignorados
(ex.: o botão ⏹ Parar não entra na gravação).
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from .config import env_path

# ------------------------------------------------------------------- Windows

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C

MOVE_MIN_PX = 2        # grava o movimento a cada N px (traçado mais fiel)
WINDOW_POLL_S = 0.5    # amostra da janela ativa entre eventos

_buttons = {
    WM_LBUTTONDOWN: ("down", "left"),
    WM_LBUTTONUP: ("up", "left"),
    WM_RBUTTONDOWN: ("down", "right"),
    WM_RBUTTONUP: ("up", "right"),
    WM_MBUTTONDOWN: ("down", "middle"),
    WM_MBUTTONUP: ("up", "middle"),
    WM_XBUTTONDOWN: ("down", "x"),
    WM_XBUTTONUP: ("up", "x"),
}


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# handles de hook são ponteiros de 64 bits: sem restype eles truncam
_user32.SetWindowsHookExW.restype = ctypes.c_void_p
_user32.SetWindowsHookExW.argtypes = [
    wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD]
_user32.CallNextHookEx.restype = ctypes.c_ssize_t
_user32.CallNextHookEx.argtypes = [
    ctypes.c_void_p, wintypes.INT, ctypes.c_size_t, ctypes.c_ssize_t]
_user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]


def _build_vk_map() -> dict[int, str]:
    """Código virtual do Windows -> nome de tecla do PyAutoGUI."""
    m: dict[int, str] = {
        0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x10: "shift",
        0x11: "ctrl", 0x12: "alt", 0x13: "pause", 0x14: "capslock",
        0x1B: "esc", 0x20: "space", 0x21: "pageup", 0x22: "pagedown",
        0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up",
        0x27: "right", 0x28: "down", 0x2C: "printscreen",
        0x2D: "insert", 0x2E: "delete", 0x5B: "win", 0x5C: "win",
        0x90: "numlock", 0x91: "scrolllock",
        0xA0: "shift", 0xA1: "shift", 0xA2: "ctrl", 0xA3: "ctrl",
        0xA4: "alt", 0xA5: "alt", 0xD1: "shift",
        0x60: "0", 0x61: "1", 0x62: "2", 0x63: "3", 0x64: "4",
        0x65: "5", 0x66: "6", 0x67: "7", 0x68: "8", 0x69: "9",
        0x6A: "*", 0x6B: "+", 0x6D: "-", 0x6E: ".", 0x6F: "/",
        0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".",
        0xBF: "/", 0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]",
        0xDE: "'",
    }
    for vk in range(0x30, 0x3A):            # 0..9
        m[vk] = chr(vk)
    for vk in range(0x41, 0x5B):            # a..z
        m[vk] = chr(vk + 0x20)
    for i, vk in enumerate(range(0x70, 0x88)):   # F1..F24
        m[vk] = f"f{i + 1}"
    return m


VK_MAP = _build_vk_map()

# ---------------------------------------------------------------- utilitários

_proc_cache: dict[int, str] = {}


def proc_name(pid: int) -> str:
    """Nome do executável (``chrome.exe``) do processo, com cache."""
    if not pid:
        return ""
    cached = _proc_cache.get(pid)
    if cached is not None:
        return cached
    name = ""
    try:
        handle = _kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if handle:
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                ok = _kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size))
                if ok:
                    name = Path(buf.value.replace("/", "\\")).name.lower()
            finally:
                _kernel32.CloseHandle(handle)
    except Exception:
        name = ""
    if len(_proc_cache) > 4096:
        _proc_cache.clear()
    _proc_cache[pid] = name
    return name


def window_text(hwnd: int) -> str:
    """Título da janela (funciona para janelas de outros processos)."""
    try:
        length = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def foreground_info() -> tuple[int, str, str]:
    """``(pid, título, executável)`` da janela em primeiro plano."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return 0, "", ""
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value, window_text(hwnd), proc_name(pid.value)


# --------------------------------------------------------------- gravador


class RoutineRecorder:
    """Captura global de mouse/teclado + janela ativa até ⏹ Parar."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._t0 = time.perf_counter()
        self._thread: threading.Thread | None = None
        self._sampler: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()
        self._hook_error = ""
        self._tid = 0
        self._hh_mouse: int | None = None
        self._hh_kbd: int | None = None
        self._mouse_cb = None          # referências p/ não sofrerem GC
        self._kbd_cb = None
        self._last_move: tuple[int, int] | None = None
        self._last_win: tuple[int, str] | None = None
        self._own = {os.getpid(), os.getppid()}   # janelas do próprio app

    @property
    def recording(self) -> bool:
        return self._running

    def stats(self) -> tuple[int, float]:
        """``(número de eventos, ms desde o início da gravação)``."""
        with self._lock:
            total = len(self.events)
        ms = (time.perf_counter() - self._t0) * 1000.0 if self._running else 0.0
        return total, ms

    # ----------------------------------------------------------- controle

    def start(self) -> None:
        """Instala os hooks globais e começa a gravar."""
        if self._running:
            raise RuntimeError("já existe uma gravação em andamento.")
        self.events = []
        self._last_move = None
        self._last_win = None
        self._hook_error = ""
        self._stop_evt.clear()
        self._ready_evt.clear()
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(
            target=self._hook_thread, name="rotina-hooks", daemon=True)
        self._thread.start()
        if not self._ready_evt.wait(3.0):
            self._shutdown()
            raise RuntimeError("tempo esgotado ao instalar os hooks de gravação.")
        if self._hook_error:
            err = self._hook_error
            self._shutdown()
            raise RuntimeError(err)
        self._running = True
        self._check_window_now()      # janela em que o usuário começou
        self._sampler = threading.Thread(
            target=self._sample_loop, name="rotina-janelas", daemon=True)
        self._sampler.start()

    def stop(self) -> dict:
        """Encerra a gravação e devolve a rotina montada."""
        if not self._running and self._thread is None:
            return self.build()
        self._running = False
        routine = self.build()
        self._shutdown()
        return routine

    def _shutdown(self) -> None:
        """Encerra o thread dos hooks (WM_QUIT) e o sampler de janelas."""
        self._stop_evt.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and self._tid:
            for _ in range(40):   # espera a fila de mensagens da thread existir
                if _user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0):
                    break
                time.sleep(0.05)
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self._tid = 0
        sampler = self._sampler
        if sampler is not None and sampler.is_alive():
            sampler.join(timeout=1.5)
        self._sampler = None

    def build(self) -> dict:
        """Monta o dicionário da rotina com os eventos gravados."""
        with self._lock:
            events = sorted((dict(e) for e in self.events),
                            key=lambda e: e.get("t", 0))
        duration = events[-1].get("t", 0) if events else 0
        now = datetime.now()
        try:
            w = int(_user32.GetSystemMetrics(0))
            h = int(_user32.GetSystemMetrics(1))
        except Exception:
            w, h = 0, 0
        try:   # área de TODOS os monitores (x negativo = monitor à esquerda)
            virtual = [int(_user32.GetSystemMetrics(i)) for i in (76, 77, 78, 79)]
        except Exception:
            virtual = None
        return {
            "id": f"r_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}",
            "kind": "rotina",
            "name": f"Rotina {now.strftime('%H:%M')}",
            "description": "",
            "ai": "pendente",
            "created": now.isoformat(timespec="seconds"),
            "screen": [w, h],
            "virtual": virtual,
            "duration_ms": int(round(duration)),
            "events": events,
        }

    # ------------------------------------------------------ thread dos hooks

    def _hook_thread(self) -> None:
        try:
            self._tid = _kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            # PeekMessage cria a fila de mensagens (PostThreadMessage exige)
            _user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 0)
            cb_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t,
                ctypes.c_ssize_t)
            self._mouse_cb = cb_type(self._on_mouse)
            self._kbd_cb = cb_type(self._on_key)
            self._hh_mouse = _user32.SetWindowsHookExW(
                WH_MOUSE_LL, ctypes.cast(self._mouse_cb, ctypes.c_void_p),
                None, 0)
            self._hh_kbd = _user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, ctypes.cast(self._kbd_cb, ctypes.c_void_p),
                None, 0)
            if not self._hh_mouse or not self._hh_kbd:
                self._hook_error = ("falha ao instalar os hooks globais de "
                                    "gravação (SetWindowsHookEx).")
            self._ready_evt.set()
            if self._hook_error:
                return
            # loop de mensagens: sem ele o Windows não entrega os hooks
            while True:
                r = _user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
                if r in (0, -1):        # WM_QUIT ou erro
                    break
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:        # pragma: no cover — defesa
            self._hook_error = f"erro nos hooks de gravação: {exc}"
            self._ready_evt.set()
        finally:
            if self._hh_mouse:
                _user32.UnhookWindowsHookEx(self._hh_mouse)
                self._hh_mouse = None
            if self._hh_kbd:
                _user32.UnhookWindowsHookEx(self._hh_kbd)
                self._hh_kbd = None
            self._mouse_cb = self._kbd_cb = None

    def _sample_loop(self) -> None:
        """Registra trocas de janela mesmo quando o usuário só está lendo."""
        while not self._stop_evt.wait(WINDOW_POLL_S):
            try:
                self._check_window_now()
            except Exception:
                pass

    # ------------------------------------------------------------ callbacks

    def _on_mouse(self, n_code, w_param, l_param):
        try:
            if n_code >= 0:
                data = ctypes.cast(
                    l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                self._handle_mouse(int(w_param), data)
        except Exception:
            pass
        try:
            return _user32.CallNextHookEx(
                self._hh_mouse, int(n_code), int(w_param), int(l_param))
        except Exception:
            return 0

    def _on_key(self, n_code, w_param, l_param):
        try:
            if n_code >= 0:
                data = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                self._handle_key(int(w_param), int(data.vkCode))
        except Exception:
            pass
        try:
            return _user32.CallNextHookEx(
                self._hh_kbd, int(n_code), int(w_param), int(l_param))
        except Exception:
            return 0

    def _handle_mouse(self, msg: int, data) -> None:
        if msg in _buttons:
            if not self._check_window_now():
                return                    # cliques no próprio programa: ignora
            state, button = _buttons[msg]
            if button == "x":
                which = (data.mouseData >> 16) & 0xFFFF
                button = "x1" if which == 1 else "x2"
            self.rec_button(state, button, data.pt.x, data.pt.y)
        elif msg == WM_MOUSEWHEEL:
            if not self._check_window_now():
                return
            delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
            self.rec_wheel(data.pt.x, data.pt.y, delta)
        elif msg == WM_MOUSEMOVE:
            if not self._check_window_now():
                return
            self.rec_move(data.pt.x, data.pt.y)

    def _handle_key(self, msg: int, vk: int) -> None:
        key = VK_MAP.get(vk)
        if key is None:
            return                        # tecla fora do mapa: não grava
        if not self._check_window_now():
            return                        # digitou no próprio programa: ignora
        state = "down" if msg in (WM_KEYDOWN, WM_SYSKEYDOWN) else "up"
        self.rec_key(state, key)

    # ------------------------------------------------ ingestão de eventos

    def _check_window_now(self) -> bool:
        """Registra a janela ativa se mudou. False = é o próprio programa."""
        pid, title, proc = foreground_info()
        if pid in self._own:
            return False
        if (pid, title) != self._last_win:
            self._last_win = (pid, title)
            self._push({"type": "window", "title": title, "proc": proc})
        return True

    def _push(self, ev: dict) -> None:
        ev["t"] = round((time.perf_counter() - self._t0) * 1000.0, 1)
        with self._lock:
            self.events.append(ev)

    def rec_move(self, x: int, y: int) -> None:
        x, y = int(x), int(y)
        last = self._last_move
        if last is not None and (x - last[0]) ** 2 + (y - last[1]) ** 2 \
                < MOVE_MIN_PX ** 2:
            return                        # abaixo do limiar: não grava
        self._last_move = (x, y)
        self._push({"type": "move", "x": x, "y": y})

    def rec_button(self, state: str, button: str, x: int, y: int) -> None:
        self._push({"type": state, "button": button,
                    "x": int(x), "y": int(y)})

    def rec_wheel(self, x: int, y: int, delta: int) -> None:
        if not delta:
            return
        amount = delta // 120 if abs(delta) >= 120 else 0
        if amount == 0:
            amount = 1 if delta > 0 else -1
        self._push({"type": "wheel", "x": int(x), "y": int(y),
                    "amount": int(amount)})

    def rec_key(self, state: str, key: str) -> None:
        if not key:
            return
        self._push({"type": "key", "state": state, "key": key})


# ------------------------------------------------------------ armazenamento


def routines_dir() -> Path:
    """Pasta das rotinas gravadas (ao lado do .env/.exe)."""
    return env_path().parent / "rotinas"


def save_routine(routine: dict) -> Path:
    folder = routines_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{Path(str(routine['id'])).name}.json"
    path.write_text(
        json.dumps(routine, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def list_routines() -> list[dict]:
    """Todas as rotinas salvas, mais recentes primeiro (ignora corrompidas)."""
    folder = routines_dir()
    if not folder.exists():
        return []
    out: list[dict] = []
    for path in folder.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    out.sort(key=lambda r: str(r.get("created", "")), reverse=True)
    return out


def load_routine(routine_id: str) -> dict:
    path = routines_dir() / f"{Path(str(routine_id)).name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"rotina inválida: {routine_id}")
    return data


def delete_routine(routine_id: str) -> bool:
    path = routines_dir() / f"{Path(str(routine_id)).name}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ------------------------------------------------- rotina citada no chat

_NUM_WORDS = {
    "uma": 1, "duas": 2, "dois": 2, "tres": 3, "quatro": 4, "cinco": 5,
    "seis": 6, "sete": 7, "oito": 8, "nove": 9, "dez": 10, "quinze": 15,
    "vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50,
    "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
    "cem": 100, "duzentos": 200, "trezentos": 300, "quatrocentos": 400,
    "quinhentos": 500, "novecentos": 900, "mil": 1000,
}


def norm_text(text: str) -> str:
    """Minúsculas, sem acentos, só letras/números — base para casar nomes."""
    ascii_ = unicodedata.normalize("NFKD", str(text)).encode(
        "ascii", "ignore").decode()
    flat = re.sub(r"[^a-z0-9]+", " ", ascii_.lower())
    return re.sub(r"\s+", " ", flat).strip()


def repetition_count(text: str) -> int:
    """Quantas vezes o usuário pediu no texto (padrão 1, teto 999).

    Entende "5 vezes", "5x", "5 repetições", "vezes: 5" e por extenso
    ("duas vezes"). O "x" exige não vir seguido de dígito — assim
    "janela 800x600" não vira contagem nenhuma.
    """
    t = norm_text(text)
    m = re.search(r"(\d+)\s*(vezes?|repet\w*|rodadas?|voltas?)", t)
    if m:
        return max(1, min(999, int(m.group(1))))
    m = re.search(r"(\d+)\s*x(?!\d)", t)
    if m:
        return max(1, min(999, int(m.group(1))))
    m = re.search(
        r"(vezes?|repet\w*|rodadas?|voltas?)\s*[:=]?\s*(\d+)", t)
    if m:
        return max(1, min(999, int(m.group(2))))
    m = re.search(
        r"\b(" + "|".join(_NUM_WORDS) + r")\s*(vezes?|x\b|repet\w*)", t)
    if m:
        return max(1, min(999, _NUM_WORDS[m.group(1)]))
    return 1


def match_routine(text: str) -> dict | None:
    """O pedido cita o NOME de uma rotina salva? => dá para rodar sem IA.

    Devolve ``{"routine": {...}, "times": n}`` ou ``None`` (aí é tarefa
    comum e segue para o agente). O nome mais LONGO vence quando o texto
    cita mais de um; exige nome de3+ caracteres casando nas fronteiras
    de palavra ("rotina x" casa, "rotinax" não).
    """
    padded = f" {norm_text(text)} "
    best: dict | None = None
    best_len = 0
    for routine in list_routines():
        name = norm_text(routine.get("name") or "")
        if len(name) < 3 or len(name) <= best_len:
            continue
        if f" {name} " in padded:
            best = routine
            best_len = len(name)
    if best is None:
        return None
    return {"routine": best, "times": repetition_count(text)}
