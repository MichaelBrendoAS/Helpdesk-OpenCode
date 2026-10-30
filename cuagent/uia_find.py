"""Busca de texto em QUALQUER lugar da janela via UI Automation do Windows.

Navegadores (Chrome/Edge/Brave/Firefox) e a maioria dos apps expõem o
conteúdo — inclusive o que está fora da área visível — na árvore de
acessibilidade. Isso deixa o agente achar "Chelsea" no fim da página sem
precisar enxergar: rola o elemento para a vista (ScrollIntoView) e devolve o
retângulo na tela. Apps sem acessibilidade (jogos, canvas) não expõem nada;
aí o agente cai na procura rolando a página.
"""

from __future__ import annotations

import time
import unicodedata

_TIME_BUDGET = 4.0     # segundos máximos varrendo a árvore


_COM_THREADS: set = set()


def _com_init() -> None:
    """UI Automation é COM: cada thread precisa de CoInitialize.

    O servidor MCP roda cada ferramenta numa thread de trabalho; sem isto a
    busca falhava com "CoInitialize não foi chamado" (só no .exe/OpenCode).
    """
    import threading
    tid = threading.get_ident()
    if tid in _COM_THREADS:
        return
    try:
        import comtypes
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:
        pass                      # já inicializado nesta thread
    _COM_THREADS.add(tid)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower().strip()


def target_window(monitor_rect=None) -> int:
    """Janela mais à frente no monitor controlado (ignora o próprio app).

    Com a GUI do Helpdesk em foco (o usuário clicou em Executar), a janela
    em foco NÃO é o alvo — vale a primeira janela visível, em ordem de
    empilhamento, cujo centro está no monitor que o agente controla.
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetTopWindow.restype = wintypes.HWND
    user32.GetWindow.restype = wintypes.HWND
    hwnd = user32.GetTopWindow(None)
    buf = ctypes.create_unicode_buffer(256)
    while hwnd:
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            user32.GetWindowTextW(hwnd, buf, 256)
            title = buf.value
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            big = (r.right - r.left) > 200 and (r.bottom - r.top) > 150
            # janelas com "dono" são popups/balões/dicas — o alvo é a principal
            owned = bool(user32.GetWindow(hwnd, 4))          # GW_OWNER
            toolwin = user32.GetWindowLongW(hwnd, -20) & 0x80  # WS_EX_TOOLWINDOW
            if (title and big and not owned and not toolwin
                    and not title.startswith("Helpdesk-OpenCode")):
                if monitor_rect is None:
                    return int(hwnd)
                ml, mt, mw, mh = monitor_rect
                cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
                if ml <= cx < ml + mw and mt <= cy < mt + mh:
                    return int(hwnd)
        hwnd = user32.GetWindow(hwnd, 2)          # GW_HWNDNEXT
    return 0


def _root_window(monitor_rect=None):
    import uiautomation as auto
    hwnd = target_window(monitor_rect)
    if not hwnd:
        return None
    return auto.ControlFromHandle(hwnd)


def _walk(root, deadline):
    import uiautomation as auto
    for ctrl, _depth in auto.WalkControl(root, maxDepth=90):
        if time.monotonic() > deadline:
            return
        yield ctrl


def find(text: str, max_results: int = 5, monitor_rect=None) -> list[dict]:
    """Elementos cujo nome contém ``text`` (sem acento/caixa), melhores 1º.

    Cada item: {"name", "type", "rect": (l, t, r, b), "ctrl"}.
    Exato > começa com > contém; controles clicáveis ganham prioridade.
    """
    _com_init()
    alvo = _norm(text)
    if not alvo:
        return []
    root = _root_window(monitor_rect)
    if root is None:
        return []
    deadline = time.monotonic() + _TIME_BUDGET
    achados: list[tuple[int, dict]] = []
    for attempt in range(2):          # Chrome liga a acessibilidade na 1ª consulta
        for ctrl in _walk(root, deadline):
            try:
                nome = (ctrl.Name or "").strip()
            except Exception:
                continue
            if not nome or len(nome) > 200:
                continue
            n = _norm(nome)
            if alvo not in n:
                continue
            try:
                tipo = ctrl.ControlTypeName
            except Exception:
                tipo = ""
            score = 0 if n == alvo else 1 if n.startswith(alvo) else 2
            if tipo in ("ButtonControl", "HyperlinkControl", "ListItemControl",
                        "MenuItemControl", "TabItemControl", "TreeItemControl"):
                score -= 0.5
            achados.append((score, {"name": nome, "type": tipo, "ctrl": ctrl}))
        if achados or attempt:
            break
        time.sleep(0.6)
    achados.sort(key=lambda x: x[0])
    out = []
    for _, item in achados[:max_results]:
        out.append(item)
    return out


def reveal(item: dict) -> tuple[int, int, int, int] | None:
    """Rola o elemento para a vista (se possível) e devolve o retângulo."""
    import uiautomation as auto
    _com_init()
    ctrl = item["ctrl"]
    try:
        pat = ctrl.GetPattern(auto.PatternId.ScrollItemPattern)
        if pat:
            pat.ScrollIntoView()
            time.sleep(0.4)
    except Exception:
        pass
    try:
        r = ctrl.BoundingRectangle
    except Exception:
        return None
    if r.right - r.left <= 0 or r.bottom - r.top <= 0:
        return None
    return (r.left, r.top, r.right, r.bottom)
