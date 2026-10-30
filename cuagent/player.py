"""Repetição de rotinas gravadas (mesmo ritmo, com escala de tela).

O replay respeita o tempo relativo de cada evento, escala as coordenadas
quando a resolução mudou desde a gravação, foca as janelas certas (pelo
executável/título gravados) e sempre solta teclas e botões do mouse ao
parar. Paradas de emergência: ``stop_event`` (botão ■ Parar) e o failsafe
do PyAutoGUI (mouse no canto superior esquerdo).
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from difflib import SequenceMatcher

import pyautogui

from .recorder import foreground_info, proc_name, window_text

CHUNK_S = 0.01        # fatia de espera — com timer de 1 ms o ritmo fica exato
BUTTONS_OK = ("left", "middle", "right")

try:  # resolução de 1 ms nos timers do Windows (pacing milimétrico)
    _winmm = ctypes.WinDLL("winmm")
    _winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
    _winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
except Exception:  # pragma: no cover — só Windows usa winmm
    _winmm = None


class _Parada(Exception):
    """stop_event foi sinalizado durante a espera."""


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _virtual() -> list[int] | None:
    """[x, y, largura, altura] da área de todos os monitores (Windows)."""
    try:
        return [int(ctypes.windll.user32.GetSystemMetrics(i))
                for i in (76, 77, 78, 79)]
    except Exception:
        return None


def scale_xy(x: int, y: int, rec_screen, cur_screen) -> tuple[int, int]:
    """Converte a coordenada gravada para a resolução atual (e limita à tela).

    ``cur_screen`` com 4 valores = área virtual idêntica à da gravação:
    mantém a coordenada (vale para monitores com x/y negativos) e só limita
    aos limites da área de todos os monitores.
    """
    if len(cur_screen) == 4:
        vx, vy, vw, vh = cur_screen
        return (_clamp(int(x), vx, vx + vw - 1), _clamp(int(y), vy, vy + vh - 1))
    cw, ch = cur_screen
    rw, rh = (rec_screen or [0, 0])[:2]
    if rw and rh and (rw, rh) != (cw, ch):
        x = x * cw / rw
        y = y * ch / rh
    return (_clamp(round(x), 0, max(0, cw - 1)),
            _clamp(round(y), 0, max(0, ch - 1)))


# ------------------------------------------------------------ foco de janela


def find_window(title: str, proc: str) -> int | None:
    """Acha a janela mais parecida com a gravada (título e/ou executável)."""
    user32 = ctypes.windll.user32
    own = os.getpid()
    best_hwnd = 0
    best_score = 0.0
    title_l = (title or "").strip().lower()
    proc_l = (proc or "").strip().lower()

    enum_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        nonlocal best_hwnd, best_score
        try:
            if not user32.IsWindowVisible(hwnd):
                return 1
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own:
                return 1                 # nunca foca o próprio programa
            text = window_text(hwnd)
            if not text:
                return 1
            score = 0.0
            tl = text.lower()
            if proc_l and proc_name(pid.value) == proc_l:
                score += 3.0
            if title_l:
                if tl == title_l:
                    score += 6.0
                elif title_l in tl or tl in title_l:
                    score += 4.0
                else:
                    score += SequenceMatcher(None, tl, title_l).ratio() * 2.5
            if score > best_score:
                best_score = score
                best_hwnd = hwnd
        except Exception:
            pass
        return 1

    try:
        user32.EnumWindows(enum_type(cb), 0)
    except Exception:
        return None
    if best_hwnd and best_score >= 3.0:
        return best_hwnd
    return None


def focus_window(title: str, proc: str = "") -> bool:
    """Traz a janela gravada para primeiro plano (melhor esforço)."""
    hwnd = find_window(title, proc)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)             # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd:
            # truque do ALT: o Windows só concede foco em certas condições
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


# ----------------------------------------------------------------- replay


def _wait_until(until: float, stop_event: threading.Event) -> None:
    """Espera o instante alvo em fatias curtas — responde rápido ao ■ Parar."""
    while True:
        if stop_event.is_set():
            raise _Parada()
        remaining = until - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(min(CHUNK_S, remaining))


def _release(down_keys: list[str], down_buttons: list[str]) -> None:
    """Solta teclas/botões que ficariam presos ao parar no meio de um chord."""
    for key in reversed(down_keys):
        try:
            pyautogui.keyUp(key)
        except Exception:
            pass
    for button in down_buttons:
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            pass
    down_keys.clear()
    down_buttons.clear()


def _exec_event(ev: dict, rec_screen, cur_screen, focus,
                down_keys: list[str], down_buttons: list[str]) -> None:
    kind = ev.get("type")

    if kind == "window":
        focus(str(ev.get("title") or ""), str(ev.get("proc") or ""))
        return

    if kind == "move":
        x, y = scale_xy(int(ev.get("x", 0)), int(ev.get("y", 0)),
                        rec_screen, cur_screen)
        pyautogui.moveTo(x, y)
        return

    if kind in ("down", "up"):
        button = str(ev.get("button") or "left")
        if button not in BUTTONS_OK:
            return                        # x1/x2 não têm equivalente
        x, y = scale_xy(int(ev.get("x", 0)), int(ev.get("y", 0)),
                        rec_screen, cur_screen)
        pyautogui.moveTo(x, y)
        if kind == "down":
            pyautogui.mouseDown(button=button)
            if button not in down_buttons:
                down_buttons.append(button)
        else:
            pyautogui.mouseUp(button=button)
            if button in down_buttons:
                down_buttons.remove(button)
        return

    if kind == "wheel":
        x, y = scale_xy(int(ev.get("x", 0)), int(ev.get("y", 0)),
                        rec_screen, cur_screen)
        pyautogui.moveTo(x, y)
        amount = int(ev.get("amount") or 0)
        if amount:
            from .actions import _wheel   # pyautogui rola 1/120 do gravado
            _wheel(amount, (x, y), False)
        return

    if kind == "key":
        key = str(ev.get("key") or "")
        if not key:
            return
        if ev.get("state") == "down":
            try:
                pyautogui.keyDown(key)
            except Exception:
                return                    # nome que o PyAutoGUI não conhece
            if key not in down_keys:
                down_keys.append(key)
        else:
            try:
                pyautogui.keyUp(key)
            except Exception:
                pass
            if key in down_keys:
                down_keys.remove(key)
        return


def play(routine: dict, times: int = 1, *, stop_event=None,
         on_status=None, focus=None, speed: float = 1.0) -> str:
    """Repete a rotina ``times`` vezes. Retorna a mensagem final pro log."""
    events = sorted(routine.get("events") or [], key=lambda e: e.get("t", 0))
    if not events:
        return "! Rotina vazia — nada a repetir."
    times = max(1, int(times or 1))
    stop_event = stop_event or threading.Event()
    on_status = on_status or (lambda _m: None)
    focus = focus_window if focus is None else focus
    speed = max(0.1, float(speed or 1.0))
    try:
        cur = pyautogui.size()
    except Exception:
        cur = (1920, 1080)
    rec = routine.get("screen") or [cur[0], cur[1]]
    virt = _virtual()
    if virt and routine.get("virtual") == virt:
        cur = virt          # mesma configuração de monitores: sem escala
    name = routine.get("name") or "rotina"
    down_keys: list[str] = []
    down_buttons: list[str] = []
    old_pause = getattr(pyautogui, "PAUSE", 0.1)
    pyautogui.PAUSE = 0               # ritmo vem do timing gravado, sem folgas
    if _winmm is not None:
        try:
            _winmm.timeBeginPeriod(1)  # sleeps de 1 ms: espera milimétrica
        except Exception:
            pass

    try:
        for volta in range(1, times + 1):
            if stop_event.is_set():
                return "⏹ Repetição parada pelo usuário."
            on_status(f"▶ {name} — volta {volta}/{times}")
            t0 = time.perf_counter()
            for ev in events:
                _wait_until(t0 + ev.get("t", 0) / 1000.0 / speed, stop_event)
                _exec_event(ev, rec, cur, focus, down_keys, down_buttons)
            on_status(f"✔ Volta {volta}/{times} concluída")
        return f"✔ '{name}' repetida {times}x — concluída."
    except _Parada:
        return "⏹ Repetição parada pelo usuário."
    except pyautogui.FailSafeException:
        return "⚠ Failsafe: mouse no canto — repetição abortada."
    finally:
        _release(down_keys, down_buttons)
        pyautogui.PAUSE = old_pause
        if _winmm is not None:
            try:
                _winmm.timeEndPeriod(1)
            except Exception:
                pass


def play_combo(routine: dict, times: int = 1, *, stop_event=None,
               on_status=None, resolve=None, focus=None) -> str:
    """Repete uma Rotina do Dia (📚): sequência de rotinas com N vezes cada."""
    steps = [s for s in (routine.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return "! Rotina do Dia sem etapas."
    times = max(1, int(times or 1))
    stop_event = stop_event or threading.Event()
    on_status = on_status or (lambda _m: None)
    resolve = resolve or (lambda _rid: None)
    name = routine.get("name") or "Rotina do Dia"

    for rep in range(1, times + 1):
        for idx, step in enumerate(steps, 1):
            if stop_event.is_set():
                return "⏹ Repetição parada pelo usuário."
            rid = str(step.get("id") or "")
            child = None
            if resolve and rid:
                try:
                    child = resolve(rid)
                except Exception:
                    child = None
            if not child:
                on_status(f"⚠ etapa ausente ({rid}) — pulando")
                continue
            vezes = max(1, int(step.get("vezes") or 1))
            on_status(
                f"📚 {name} {rep}/{times} — etapa {idx}/{len(steps)}: "
                f"{child.get('name', '')}")
            msg = play(child, vezes, stop_event=stop_event,
                       on_status=on_status, focus=focus)
            if stop_event.is_set():
                return "⏹ Repetição parada pelo usuário."
            if msg.startswith("⚠ Failsafe"):
                return msg
    return f"✔ '{name}' concluída ({times}x)."


__all__ = [
    "play", "play_combo", "focus_window", "find_window", "scale_xy",
    "foreground_info",
]
