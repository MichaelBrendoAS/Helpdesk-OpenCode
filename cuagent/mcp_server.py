"""Servidor MCP (stdio): dá ao OpenCode as "mãos" deste PC.

O OpenCode é o agente — decide os passos com o modelo configurado nele
(chave própria ou créditos do Zen; o plano grátis do Zen recusa este uso)
e chama estas ferramentas. Toda ação já devolve a TELA ATUALIZADA, então o
modelo nunca gasta uma chamada extra só para ver o resultado.

Coordenadas normalizadas 0–1000 nos dois eixos (Gemini/Qwen/MiMo apontam
nativamente nessa grade; medido: pixels faziam os cliques caírem longe).

Execução: ``python -m cuagent.mcp_server`` (o opencode.json já aponta para ele).
"""

from __future__ import annotations

import io
import re
import os
import sys
import time

from mcp.server.mcpserver import Image, MCPServer

from . import actions, safety
from .config import load_env
from .screen import Screen

load_env()

_SETTLE = 0.35          # a tela precisa de um instante para redesenhar
_JPEG_QUALITY = 60      # nitidez suficiente para ler botões; ~40% menos bytes

_screen: Screen | None = None
# CJK, cirílico, árabe, hebraico, tailandês, coreano… (emojis e acentos passam)
_FOREIGN_RE = re.compile("[Ѐ-ӿ֐-ۿ฀-๿぀-ヿ㐀-鿿가-힯豈-﫿]")


def _scr() -> Screen:
    global _screen
    if _screen is None:
        # 1280: mesma precisão da nativa (8/8, ~2 px) com 55% menos tokens;
        # 960 já erra alvos pequenos (medido 25/09/2026)
        width = int(os.getenv("CUA_MAX_SCREENSHOT_WIDTH", "1280") or 1280)
        _screen = Screen(width)
        # dxcam imprime avisos no stdout — que aqui é o canal do protocolo;
        # a captura GDI (mss) é silenciosa e rápida o bastante por passo
        _screen._cam_failed = True
    # servidor fica ligado entre tarefas: segue o monitor escolhido na GUI
    # (salvo no .env) sem precisar reiniciar
    try:
        from dotenv import dotenv_values
        from .config import env_path
        mon = int(dotenv_values(env_path()).get("CUA_MONITOR") or 0)
        if mon and mon != _screen.monitor:
            _screen.set_monitor(mon)
    except Exception:
        pass
    return _screen


def _shot() -> Image:
    buf = io.BytesIO()
    _scr().capture().save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return Image(data=buf.getvalue(), format="jpeg")


def _px(x: float, y: float) -> list[int]:
    """0–1000 -> pixels da imagem do modelo (Screen converte p/ nativo)."""
    mw, mh = _scr().model_size()
    x = min(max(float(x), 0.0), 1000.0)
    y = min(max(float(y), 0.0), 1000.0)
    return [min(mw - 1, round(x * mw / 1000)), min(mh - 1, round(y * mh / 1000))]


def _confirm(action: dict) -> bool:
    """Ação destrutiva: caixa nativa do Windows (sempre visível no topo)."""
    if not safety.is_destructive(action):
        return True
    if os.getenv("CUA_CONFIRM_DESTRUCTIVE", "1").strip() in ("0", "false", "no"):
        return True
    desc = safety.describe(action) or str(action.get("action"))
    if sys.platform != "win32":
        return False
    import ctypes
    # MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
    res = ctypes.windll.user32.MessageBoxW(
        None, f"O agente quer executar:\n\n{desc}\n\nPermitir?",
        "Helpdesk-OpenCode — confirmar ação", 0x4 | 0x30 | 0x40000 | 0x10000)
    return res == 6  # IDYES


def _run(action: dict, xy: tuple | None = None) -> list:
    if not _confirm(action):
        return ["Usuário NEGOU esta ação (não executada). Siga por outro "
                "caminho ou explique ao usuário.", _shot()]
    try:
        _, note = actions.execute(action, _scr())
    except Exception as exc:     # erro vira informação para o modelo
        return [f"Erro: {exc}", _shot()]
    time.sleep(_SETTLE)
    # o modelo pensa em 0-1000: devolver pixels o confundiria no passo seguinte
    note = note.split(" | cursor")[0]
    if xy is not None:
        note = re.sub(r"\(-?\d+, -?\d+\)", f"({xy[0]:g}, {xy[1]:g})", note,
                      count=1)
    return [f"ok: {note}", _shot()]


mcp = MCPServer(
    "desktop",
    instructions=(
        "Controla o PC do usuário. Coordenadas x,y NORMALIZADAS 0-1000 "
        "(0,0 = canto sup. esquerdo; 1000,1000 = inf. direito). Toda ação "
        "devolve a tela atualizada."
    ),
)


@mcp.tool()
def screenshot() -> Image:
    """Captura a tela atual. Use só no início; as ações já devolvem a tela."""
    return _shot()


@mcp.tool()
def click(x: float, y: float, button: str = "left", clicks: int = 1) -> list:
    """Clica no ponto (x,y) normalizado 0-1000. button: left|right|middle.
    clicks: 1, 2 (duplo) ou 3 (triplo). Mire no CENTRO do elemento."""
    name = {"right": "right_click", "middle": "middle_click"}.get(button)
    if name is None:
        name = {2: "double_click", 3: "triple_click"}.get(int(clicks), "left_click")
    return _run({"action": name, "coordinate": _px(x, y)}, (x, y))


@mcp.tool()
def type_text(text: str, press_enter: bool = False) -> list:
    """Digita o texto (clique no campo antes). press_enter=true envia Enter."""
    # o modelo grátis às vezes mistura chinês/cirílico no meio do português:
    # recusa ANTES de digitar (evita digitar lixo e ter de apagar depois)
    estranho = _FOREIGN_RE.findall(text)
    if estranho:
        return [f"NÃO digitado: o texto tem caracteres de outro idioma "
                f"({''.join(estranho[:10])}). Reescreva só em português do "
                "Brasil e chame type_text de novo.", _shot()]
    out = _run({"action": "type", "text": text})
    if press_enter and out[0].startswith("ok"):
        return _run({"action": "key", "text": "enter"})
    return out


@mcp.tool()
def press_key(keys: str) -> list:
    """Tecla ou atalho: "enter", "esc", "ctrl+s", "alt+tab", "win"..."""
    return _run({"action": "key", "text": keys})


@mcp.tool()
def scroll(x: float, y: float, amount: int = -5) -> list:
    """Rola no ponto (x,y). amount negativo = para baixo, positivo = cima."""
    return _run({"action": "scroll", "coordinate": _px(x, y), "amount": amount,
                 "scroll_direction": "vertical"}, (x, y))


@mcp.tool()
def drag(x1: float, y1: float, x2: float, y2: float) -> list:
    """Arrasta de (x1,y1) até (x2,y2), coordenadas 0-1000."""
    return _run({"action": "left_click_drag", "start_coordinate": _px(x1, y1),
                 "coordinate": _px(x2, y2)}, (x2, y2))


def _visible_rect(item) -> tuple[int, int, int, int] | None:
    """Traz o elemento para dentro do monitor: ScrollIntoView e, se o app
    não suportar, rola com a roda do mouse na direção dele."""
    from . import uia_find
    left, top, mw, mh = _scr().monitor_rect()
    rect = uia_find.reveal(item)
    for _ in range(25):
        if rect is None:
            return None
        cy = (rect[1] + rect[3]) // 2
        if top <= cy < top + mh:          # centro dentro do monitor = visível
            return rect
        # ~100 px por clique da roda nos navegadores: rola na medida certa
        dist = cy - (top + mh // 2)
        clicks = max(2, min(15, abs(dist) // 100))
        clicks = -clicks if dist > 0 else clicks
        cx = left + mw // 2
        actions._wheel(clicks, (cx, top + mh // 2), False)
        time.sleep(0.25)
        rect = uia_find.reveal(item)
    return None


def _to_norm(rect) -> tuple[int, int]:
    left, top, mw, mh = _scr().monitor_rect()
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return (round((cx - left) * 1000 / mw), round((cy - top) * 1000 / mh))


@mcp.tool()
def find_text(text: str) -> list:
    """Procura um texto na JANELA INTEIRA (inclusive fora da tela, abaixo na
    página), rola até ele e devolve onde está (x,y 0-1000) + a tela.
    Use quando o alvo não aparece, está cortado ou pode estar mais abaixo."""
    from . import uia_find
    try:
        itens = uia_find.find(text, monitor_rect=_scr().monitor_rect())
    except Exception as exc:
        return [f"Busca indisponível ({exc}). Procure rolando com scroll.", _shot()]
    if not itens:
        return [f'"{text}" não encontrado na árvore da janela (app pode não '
                "expor texto). Procure rolando com scroll e olhando a tela.",
                _shot()]
    rect = _visible_rect(itens[0])
    extras = "; ".join(i["name"][:40] for i in itens[1:4])
    if rect is None:
        return [f'Achei "{itens[0]["name"][:60]}" mas não consegui trazê-lo '
                "para a tela. Tente rolar com scroll.", _shot()]
    x, y = _to_norm(rect)
    msg = f'Encontrado "{itens[0]["name"][:60]}" em ({x}, {y}).'
    if extras:
        msg += f" Outros parecidos: {extras}."
    return [msg, _shot()]


@mcp.tool()
def click_text(text: str) -> list:
    """Procura o texto na janela inteira (mesmo fora da tela), rola até ele
    e CLICA no centro. Ideal para 'clique no Chelsea', 'abra Configurações'."""
    from . import uia_find
    try:
        itens = uia_find.find(text, monitor_rect=_scr().monitor_rect())
    except Exception as exc:
        return [f"Busca indisponível ({exc}). Procure rolando com scroll.", _shot()]
    if not itens:
        return [f'"{text}" não encontrado na árvore da janela. Procure rolando '
                "com scroll e clique pela tela.", _shot()]
    rect = _visible_rect(itens[0])
    if rect is None:
        return [f'Achei "{itens[0]["name"][:60]}" mas não consegui trazê-lo '
                "para a tela.", _shot()]
    x, y = _to_norm(rect)
    return _run({"action": "left_click", "coordinate": _px(x, y)}, (x, y))


@mcp.tool()
def open_app(name: str) -> list:
    """Abre um programa instalado pelo nome (busca aproximada local)."""
    out = _run({"action": "open_app", "name": name})
    return out


@mcp.tool()
def wait(seconds: float = 1.5) -> list:
    """Espera a tela carregar (máx. 10 s) e devolve a tela."""
    return _run({"action": "wait", "duration": min(max(seconds, 0.2), 10.0)})


def main() -> None:
    """stdout é o canal JSON-RPC com o OpenCode — exclusivo do protocolo.

    Bibliotecas escrevem avisos direto em sys.stdout (a UI Automation loga
    "Find Control Timeout" quando um elemento some após um clique). Um único
    texto desses corrompia o canal e TODAS as ferramentas passavam a dar
    erro até reiniciar. Aqui o protocolo recebe uma cópia privada do stdout
    e o sys.stdout do processo passa a apontar para o stderr.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    # erros/avisos vão para logs\mcp.log (diagnóstico em qualquer PC)
    try:
        from .opencode_agent import app_dir
        pasta = app_dir() / "logs"
        pasta.mkdir(parents=True, exist_ok=True)
        log = pasta / "mcp.log"
        if log.exists() and log.stat().st_size > 2_000_000:
            log.unlink()                       # não deixa crescer sem fim
        sys.stderr = open(log, "a", encoding="utf-8", buffering=1)
        sys.stderr.write(f"\n--- MCP iniciado {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    except Exception:
        if sys.stderr is None:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")

    proto_out = io.TextIOWrapper(os.fdopen(os.dup(sys.stdout.fileno()), "wb"),
                                 encoding="utf-8", line_buffering=True,
                                 write_through=True)
    proto_in = io.TextIOWrapper(os.fdopen(os.dup(sys.stdin.fileno()), "rb"),
                                encoding="utf-8")
    sys.stdout = sys.stderr            # tudo que "vazar" vai para o stderr
    try:                               # nível do SO também (C/print nativos)
        os.dup2(sys.stderr.fileno(), 1)
    except OSError:
        pass

    async def _serve() -> None:
        async with stdio_server(stdin=anyio.wrap_file(proto_in),
                                stdout=anyio.wrap_file(proto_out)) as (rd, wr):
            low = mcp._lowlevel_server
            await low.run(rd, wr, low.create_initialization_options())

    try:
        anyio.run(_serve)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
