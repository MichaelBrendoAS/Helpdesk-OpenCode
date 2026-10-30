"""Camada de segurança: descreve e confirma ações antes de executá-las."""

from __future__ import annotations

import re


class Abort(Exception):
    """Usuário pediu para encerrar a execução."""


# ações que não alteram o estado da tela: dispensam confirmação
_READ_ONLY = {"screenshot", "wait", "cursor_position"}

_CONFIRMABLE = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_mouse_down",
    "left_mouse_up",
    "left_click_drag",
    "scroll",
    "type",
    "key",
    "hold_key",
    "open_app",
}

# comandos realmente destrutivos quando DIGITADOS (terminal, Executar...).
# Palavras soltas ("apagar", "excluir") em mensagens NÃO contam.
_DESTRUCTIVE_RE = re.compile(
    r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|\bdel\s+/[sfq]|\brmdir\s+/s|"
    r"\bformat\s+[a-z]:|\bfdisk\b|\bdiskpart\b|\bshutdown\s+[/-]|"
    r"\bremove-item\b.*-recurse|\bdrop\s+(table|database)\b|\bmkfs\b",
    re.I,
)


def is_destructive(action: dict) -> bool:
    """True só para o que é irreversível de verdade.

    Tecla Delete/Backspace comum (limpar campo, mandar p/ Lixeira) passa;
    Shift+Delete (exclui sem Lixeira) e comandos perigosos digitados pedem
    confirmação.
    """
    name = str(action.get("action", ""))
    text = str(action.get("text", ""))
    if name in {"key", "hold_key"}:
        keys = {k.strip().lower() for k in text.replace(" ", "").split("+")}
        return "shift" in keys and bool(keys & {"delete", "del"})
    if name == "type":
        return bool(_DESTRUCTIVE_RE.search(text))
    return False


def _clip(text: str, limit: int = 70) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


def describe(action: dict) -> str | None:
    """Descrição legível da ação, ou ``None`` se não precisa de confirmação."""
    name = str(action.get("action", ""))
    if name in _READ_ONLY:
        return None
    if name not in _CONFIRMABLE:
        return name  # desconhecidas passam pela confirmação por segurança

    if name == "open_app":
        return f"abrir programa {str(action.get('name') or action.get('text', ''))!r}"
    if name == "type":
        return f'digitar "{_clip(str(action.get("text", "")))}"'
    if name in {"key", "hold_key"}:
        return f"tecla {action.get('text', '')}"
    if name == "scroll":
        coord = action.get("coordinate")
        where = f" em ({coord[0]}, {coord[1]})" if coord else ""
        return f"rolar {action.get('amount', 0)}{where}"
    if name == "left_click_drag":
        end = action.get("coordinate", ["?", "?"])
        return f"arrastar até ({end[0]}, {end[1]})"
    coord = action.get("coordinate")
    where = f" ({coord[0]}, {coord[1]})" if coord else ""
    return f"{name}{where}"


def approve(action: dict, auto_approve: bool) -> str:
    """Pede confirmação ao usuário.

    Retorna ``"ok"`` para executar ou ``"skip"`` para pular.
    Levanta :class:`Abort` se o usuário mandou encerrar.
    """
    desc = describe(action)
    if desc is None:
        return "ok"
    if auto_approve and not is_destructive(action):
        return "ok"

    print(f"  ? {desc}")
    try:
        answer = input("    [Enter] executar · [p] pular · [q] encerrar > ")
    except EOFError:
        return "ok"  # stdin fechado (execução em lote): segue direto
    answer = answer.strip().lower()
    if answer in {"q", "quit", "sair"}:
        raise Abort("Execução encerrada pelo usuário.")
    if answer in {"p", "pular", "skip"}:
        return "skip"
    return "ok"
