"""Execução das ações pedidas pelo modelo (mouse, teclado e tela).

Cada função traduz uma ação do formato da API `computer` para uma chamada
real do PyAutoGUI. Todas as coordenadas que chegam aqui estão no "espaço do
modelo" e são convertidas para pixels nativos via :class:`Screen`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pyautogui
import pyperclip

from .screen import Screen

pyautogui.PAUSE = 0.03          # pausa mínima: a velocidade vem do protocolo
pyautogui.FAILSAFE = True       # mouse no canto superior esquerdo => aborta
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # sem janela de console


class Interrupted(RuntimeError):
    """Parada pedida pelo usuário durante uma espera longa."""


def _sleep(secs: float, should_stop=None) -> None:
    """time.sleep interrompível (checa o ■ Parar a cada 0,1 s)."""
    end = time.monotonic() + secs
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        if should_stop is not None and should_stop():
            raise Interrupted("Parada solicitada pelo usuário.")
        time.sleep(min(0.1, left))


# ---------------------------------------------------------------- utilitários


def _wheel(clicks: int, pos: tuple[int, int] | None, horizontal: bool) -> None:
    """Roda do mouse em "cliques" reais, em qualquer monitor.

    O pyautogui no Windows manda o valor cru como delta (1 = 1/120 de um
    clique da roda: praticamente não rola) e ainda prende x/y à tela
    principal — num monitor à esquerda (x negativo) rolava no monitor errado.
    """
    if sys.platform != "win32":
        kw = {"x": pos[0], "y": pos[1]} if pos else {}
        (pyautogui.hscroll if horizontal else pyautogui.scroll)(clicks, **kw)
        return
    import ctypes
    user32 = ctypes.windll.user32
    if pos is not None:
        user32.SetCursorPos(int(pos[0]), int(pos[1]))
        time.sleep(0.02)
    flag = 0x1000 if horizontal else 0x0800   # MOUSEEVENTF_HWHEEL / WHEEL
    user32.mouse_event(flag, 0, 0, ctypes.c_uint32(clicks * 120 & 0xFFFFFFFF), 0)


def _key_alias(key: str) -> str:
    """Normaliza nomes de tecla usados pela API para os do PyAutoGUI."""
    key = key.strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    if key in {"super", "meta", "cmd", "command", "windows", "winkey"}:
        if sys.platform == "darwin":
            return "command"
        return "win"
    return _KEY_ALIASES.get(key, key)


# nomes que os modelos usam (estilo DOM/xdotool) -> nomes do PyAutoGUI
_KEY_ALIASES = {
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left",
    "arrowright": "right", "control": "ctrl", "ctl": "ctrl", "option": "alt",
    "escape": "esc", "del": "delete", "ins": "insert", "spacebar": "space",
    "returnkey": "enter", "pgup": "pageup", "pgdn": "pagedown",
    "pagedn": "pagedown", "bksp": "backspace", "caps": "capslock",
    "prtsc": "printscreen", "printscr": "printscreen", "plus": "+",
    "minus": "-", "menu": "apps", "contextmenu": "apps",
}


def _keys(combo: str) -> list[str]:
    """``"ctrl+shift+t"`` -> ``["ctrl", "shift", "t"]``.

    Tecla desconhecida vira erro explícito: o PyAutoGUI ignoraria em
    silêncio e o modelo acharia que funcionou.
    """
    combo = combo.strip()
    parts = ["+"] if combo == "+" else [p for p in combo.split("+") if p.strip()]
    keys = [_key_alias(p) for p in parts]
    bad = [k for k in keys if k not in pyautogui.KEYBOARD_KEYS]
    if bad:
        raise ValueError(f"tecla desconhecida: {', '.join(bad)} "
                         "(use nomes como enter, esc, tab, up, pagedown, f5, ctrl+s)")
    return keys


def _type_text(text: str) -> None:
    """Digita texto via área de transferência (funciona com acentos/emoji)."""
    try:
        old_clipboard = pyperclip.paste()
    except Exception:
        old_clipboard = None

    try:
        pyperclip.copy(text)
    except Exception:
        # fallback: digitação direta (não funciona bem com acentos)
        pyautogui.write(text, interval=0.02)
        return

    time.sleep(0.03)
    paste_keys = ("command", "v") if sys.platform == "darwin" else ("ctrl", "v")
    pyautogui.hotkey(*paste_keys)
    # o alvo lê a área de transferência de forma assíncrona: restaurar cedo
    # demais faz colar o conteúdo ANTIGO (apps lentos, área remota)
    time.sleep(0.3)
    if old_clipboard is not None:
        try:
            pyperclip.copy(old_clipboard)
        except Exception:
            pass


def _clip(text: str, limit: int = 70) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def _image_block(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }


def _ok() -> dict:
    return _text_block("Ação executada com sucesso.")


def _pos(action: dict, screen: Screen) -> tuple[int, int] | None:
    coord = action.get("coordinate")
    if not coord or len(coord) != 2:
        return None
    return screen.from_model(coord[0], coord[1])


def _fmt_pos(action: dict) -> str:
    coord = action.get("coordinate")
    if not coord:
        return ""
    return f" ({coord[0]}, {coord[1]})"


def _foreground() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _bring_to_monitor(before: int, screen: Screen, should_stop=None) -> str:
    """Leva a janela recém-aberta para o monitor que o agente enxerga.

    O Windows abre programas no monitor principal; se o agente controla
    outro monitor ele não via a janela e saía caçando pelo menu Iniciar.
    Só move se a janela em foco MUDOU depois do lançamento (é a nova).
    """
    if sys.platform != "win32":
        return ""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = 0
    for _ in range(20):                      # até ~2 s pela janela nova
        hwnd = user32.GetForegroundWindow()
        if hwnd and hwnd != before:
            break
        _sleep(0.1, should_stop)
    if not hwnd or hwnd == before:
        return ""
    left, top, mw, mh = screen.monitor_rect()
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    moved = not (left <= cx < left + mw and top <= cy < top + mh)
    # sempre abre MAXIMIZADO no monitor controlado: janela no tamanho/posição
    # antigos saía cortada ou fora da resolução
    user32.ShowWindow(hwnd, 9)               # SW_RESTORE (permite mover)
    if moved:
        w = min(rect.right - rect.left, mw)
        h = min(rect.bottom - rect.top, mh)
        user32.SetWindowPos(hwnd, 0, left + max(0, (mw - w) // 2),
                            top + max(0, (mh - h) // 2), w, h,
                            0x0004 | 0x0010)  # NOZORDER|NOACTIVATE
    user32.ShowWindow(hwnd, 3)               # SW_MAXIMIZE neste monitor
    return " (janela maximizada neste monitor)"


# ------------------------------------------------------------------ dispatcher


def execute(action: dict, screen: Screen,
            should_stop=None) -> tuple[list[dict], str]:
    """Executa uma ação da API `computer`.

    Retorna ``(conteúdo_do_tool_result, descrição_para_o_log)``.
    """
    try:
        return _dispatch(action, screen, should_stop)
    except pyautogui.FailSafeException as exc:
        raise RuntimeError(
            "Failsafe acionado (mouse no canto superior esquerdo). "
            "Mova o mouse para o centro da tela e tente novamente."
        ) from exc


def _dispatch(action: dict, screen: Screen,
              should_stop=None) -> tuple[list[dict], str]:
    name = str(action.get("action", ""))

    # ---------------- somente leitura (não alteram a tela) ----------------
    if name == "screenshot":
        mw, mh = screen.model_size()
        return [_image_block(screen.capture_b64())], f"screenshot ({mw}x{mh})"

    if name == "wait":
        secs = max(0.0, min(float(action.get("duration", 1) or 1), 60.0))
        _sleep(secs, should_stop)
        return [_ok()], f"wait {secs:.1f}s"

    if name == "cursor_position":
        x, y = pyautogui.position()
        mx, my = screen.to_model(x, y)
        return [_text_block(f"{mx},{my}")], f"cursor_position ({mx},{my})"

    # ---------------- abrir programas (inventário local) ----------------
    if name == "open_app":
        query = str(action.get("name") or action.get("text")
                    or action.get("app") or action.get("application") or "")
        from . import inventory
        found = inventory.resolve(query)     # ValueError com sugestões
        target = (found.get("target") or "").strip()
        before = _foreground()
        if target.lower().startswith("shell:appsfolder"):
            subprocess.Popen(["explorer.exe", target], creationflags=_NO_WINDOW)
        elif target:
            os.startfile(target)             # .lnk / .exe / arquivo
        else:
            # só o nome conhecido: deixa o Windows procurar no Menu Iniciar
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "", found["name"]],
                creationflags=_NO_WINDOW,
            )
        _sleep(1.0, should_stop)             # janela do Windows leva ~1-2s a aparecer
        moved = _bring_to_monitor(before, screen, should_stop)
        return [_ok()], f"open_app {found['name']}{moved}"

    # ---------------- entrada de texto ----------------
    if name == "type":
        text = str(action.get("text", ""))
        _type_text(text)
        return [_ok()], f'type "{_clip(text)}"'

    if name == "key":
        keys = _keys(str(action.get("text", "")))
        if keys:
            pyautogui.hotkey(*keys)
        return [_ok()], "key " + "+".join(keys)

    if name == "hold_key":
        keys = _keys(str(action.get("text", "")))
        dur = max(0.0, min(float(action.get("duration", 1) or 1), 30.0))
        try:
            for k in keys:
                pyautogui.keyDown(k)
            _sleep(dur, should_stop)
        finally:                             # nunca deixa tecla presa
            for k in reversed(keys):
                pyautogui.keyUp(k)
        return [_ok()], f"hold_key {'+'.join(keys)} {dur:.1f}s"

    # ---------------- mouse ----------------
    if name in {
        "mouse_move",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "triple_click",
        "left_mouse_down",
        "left_mouse_up",
    }:
        pos = _pos(action, screen)
        kwargs: dict = {}
        if pos is not None:
            # clica direto na coordenada (sem moveTo/duração): cai exato
            # e elimina ~0,15 s de movimento animado por clique
            kwargs["x"], kwargs["y"] = pos

        if name == "mouse_move":
            if pos is not None:
                pyautogui.moveTo(pos[0], pos[1], duration=0.05)
        elif name == "left_click":
            pyautogui.click(**kwargs)
        elif name == "right_click":
            pyautogui.rightClick(**kwargs)
        elif name == "middle_click":
            pyautogui.middleClick(**kwargs)
        elif name == "double_click":
            pyautogui.doubleClick(**kwargs)
        elif name == "triple_click":
            pyautogui.tripleClick(**kwargs)
        elif name == "left_mouse_down":
            pyautogui.mouseDown(button="left", **kwargs)
        elif name == "left_mouse_up":
            pyautogui.mouseUp(button="left", **kwargs)

        px, py = pyautogui.position()
        mx, my = screen.to_model(px, py)
        return [_ok()], f"{name}{_fmt_pos(action)} | cursor em ({mx}, {my})"

    if name == "left_click_drag":
        start = action.get("start_coordinate")
        end = action.get("coordinate")
        if not end:
            raise ValueError("left_click_drag requer 'coordinate'")
        if start and len(start) == 2:
            sx, sy = screen.from_model(start[0], start[1])
            pyautogui.moveTo(sx, sy, duration=0.04)
        ex, ey = screen.from_model(end[0], end[1])
        pyautogui.dragTo(ex, ey, duration=0.15, button="left")
        return [_ok()], f"left_click_drag -> ({end[0]}, {end[1]})"

    if name == "scroll":
        pos = _pos(action, screen)
        direction = str(action.get("scroll_direction", "vertical")).lower()
        amount = int(action.get("amount", 0) or 0) * max(
            1, int(action.get("repeat", 1) or 1)
        )
        if amount == 0:
            amount = -3 if direction != "up" else 3
        # "down"/"up"/"left"/"right" definem o sinal (o modelo nem sempre acerta)
        if direction == "down":
            amount = -abs(amount)
        elif direction == "up":
            amount = abs(amount)
        elif direction == "left":
            amount = -abs(amount)
        elif direction == "right":
            amount = abs(amount)
        horizontal = direction in {"left", "right", "horizontal"}
        _wheel(amount, pos, horizontal)
        return [_ok()], f"scroll {direction} {amount}" + _fmt_pos(action)

    raise ValueError(f"Ação desconhecida: {name!r}")
