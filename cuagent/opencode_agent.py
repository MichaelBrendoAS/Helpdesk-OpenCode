"""Agente via OpenCode: o app entrega a tarefa ao `opencode run`.

O OpenCode decide os passos com os modelos gratuitos do Zen e age no PC
pelas ferramentas do servidor MCP (`cuagent.mcp_server`). Quando um modelo
recusa, esgota a cota ou falha, a tarefa recomeça no próximo da lista.

Só entram modelos que ENXERGAM imagem — os de texto (Nemotron, Big Pickle,
Ling) não conseguem ler a captura da tela.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from . import safety

# preferência + plano B se o catálogo não puder ser lido. A lista real vem
# do catálogo do Zen (discover_models) a cada abertura do app.
DEFAULT_MODELS = (
    "space-bunny-free",
    "muse-spark-1.3-contributor-free",
    "mimo-v2.6-flash-free",
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# "clica no Chelsea", "clique em Transfers", "entra na Gallery ai",
# "seleciona o Arsenal" -> alvo = o texto a clicar
_FAST_RE = re.compile(
    r"^(?:por favor,?\s*)?(?:clica|clique|clicar|click|aperta|aperte|apertar|"
    r"seleciona|selecione|selecionar|entra|entre|entrar|vai|va)"
    r"(?:\s+(?:em|no|na|nos|nas|o|a|os|as|pra|para|pro|ao|à))*\s+"
    r"(?P<alvo>.+?)(?:\s+(?:a[ií]|pra mim|por favor))?[\s.!?]*$",
    re.I)

AGENTS_MD = """\
Você controla o computador do usuário pelas ferramentas `desktop_*`.

- A TELA ATUAL já vem anexada à tarefa: NÃO chame `desktop_screenshot` no início. Toda ação também devolve a tela atualizada.
- Coordenadas x,y NORMALIZADAS 0-1000 ([0,0] = canto superior esquerdo, [1000,1000] = inferior direito). Mire no CENTRO do elemento.
- Aja só sobre o que VÊ na última tela. Use o mínimo de passos e atalhos de teclado.
- Alvo com NOME/TEXTO (botão, time, link, menu)? Prefira `desktop_click_text` — acha o texto na página INTEIRA (mesmo fora da tela ou cortado), rola até ele e clica. Para só localizar, `desktop_find_text`.
- Se a busca por texto não achar (jogos, imagens, apps sem texto), procure ROLANDO: `desktop_scroll` (amount -5) sobre a área da lista/página e olhe a tela nova; repita até achar ou até a tela parar de mudar (fim da página). Role de volta (amount positivo) se passou do alvo.
- Programas: `desktop_open_app`. Se a janela não apareceu, `desktop_wait` (nunca repita open_app).
- Clique no campo antes de digitar.
- Idioma: SOMENTE português do Brasil (pt-BR), em tudo — mensagens digitadas e respostas. Use o jeito natural de brasileiro falar (vocabulário e expressões do Brasil, nada de português de Portugal: "você", "celular", "time", "ônibus"). Nunca misture palavras ou caracteres de outros idiomas.
- Nada destrutivo sem pedido explícito. Falhou 2 vezes? Pare e explique.
- Faça SOMENTE o que foi pedido. Assim que o último passo pedido estiver feito, TERMINE na hora com uma frase curta em português do Brasil — sem conferir de novo, sem repetir e sem ações extras. Se o clique pedido foi feito, a tarefa acabou.
"""


def _for_ai(img):
    """Imagem para a IA em 1280 px de largura (precisão igual à nativa)."""
    w = int(os.getenv("CUA_MAX_SCREENSHOT_WIDTH", "1280") or 1280)
    if img.width > w:
        from PIL import Image
        img = img.resize((w, round(img.height * w / img.width)), Image.BILINEAR)
    return img


def app_dir() -> Path:
    """Pasta do app (ao lado do .exe quando empacotado)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _mcp_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--mcp"]          # o próprio .exe vira o MCP
    return [sys.executable, "-m", "cuagent.mcp_server"]


def ensure_workspace() -> Path:
    """Grava opencode.json + AGENTS.md na pasta do app (sempre atualizados)."""
    root = app_dir()
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"desktop": {"type": "local", "command": _mcp_command(),
                            "enabled": True}},
        # ferramenta com "deny" some do prompt: 7,4k -> 3,8k tokens por chamada
        "permission": {t: "deny" for t in (
            "edit", "bash", "webfetch", "read", "glob", "grep", "task",
            "todowrite", "websearch", "skill", "question")},
    }
    (root / "opencode.json").write_text(json.dumps(cfg, indent=2),
                                        encoding="utf-8")
    (root / "AGENTS.md").write_text(AGENTS_MD, encoding="utf-8")
    return root


def opencode_env(root: Path, monitor: int | None = None) -> dict:
    env = dict(os.environ)
    env["PWD"] = str(root)
    env["OPENCODE_DISABLE_EXTERNAL_SKILLS"] = "1"   # ~62k tokens a menos
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"       # sem CLAUDE.md/skills do Claude
    # OpenCode ISOLADO: config, plugins, dados e cache próprios do app. Sem
    # isto o app herdava a configuração global de quem roda (plugins, MCPs,
    # "code mode"...) e quebrava em PCs configurados de outro jeito.
    casa = root / ".opencode_home"
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        pasta = casa / sub
        pasta.mkdir(parents=True, exist_ok=True)
        env[var] = str(pasta)
    if monitor is not None:
        env["CUA_MONITOR"] = str(monitor)
    return env


class _Server:
    """`opencode serve` sempre ligado: sem boot por tarefa (~20 s -> ~2 s).

    Fala com a API HTTP local (sessão -> mensagem); o servidor MCP de
    ferramentas também fica quente entre as tarefas.
    """

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.url = ""
        self.root: Path | None = None
        self._auth = ""
        self._lock = threading.Lock()
        self.last_error = ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _req(self, method: str, path: str, body=None, timeout: float = 30.0):
        import urllib.parse
        import urllib.request
        q = ("&" if "?" in path else "?") + "directory=" + urllib.parse.quote(str(self.root))
        req = urllib.request.Request(
            self.url + path + q, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json",
                     "Authorization": "Basic " + self._auth})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else None

    def start(self, monitor: int | None = None) -> bool:
        with self._lock:
            if self.alive():
                return True
            exe = find_opencode()
            if not exe:
                return False
            _kill_stale()          # servidor órfão de execução anterior (travou/foi morto)
            import base64
            import secrets
            import socket
            root = ensure_workspace()      # config não recarrega: grava antes
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            senha = secrets.token_urlsafe(16)
            env = opencode_env(root, monitor)
            env["OPENCODE_SERVER_PASSWORD"] = senha
            self._auth = base64.b64encode(f"opencode:{senha}".encode()).decode()
            self.root, self.url = root, f"http://127.0.0.1:{port}"
            self.proc = subprocess.Popen(
                [exe, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                cwd=str(root), env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW)
            try:
                _PIDFILE().write_text(str(self.proc.pid), encoding="utf-8")
            except Exception:
                pass
            # 1ª vez num PC novo (antivírus, catálogo de modelos) pode demorar
            fim = time.monotonic() + 120
            while time.monotonic() < fim and self.alive():
                try:
                    self._req("GET", "/mcp", timeout=60)   # sobe projeto + MCP
                    return True
                except Exception:
                    time.sleep(0.5)
            self.last_error = ("o OpenCode não respondeu em 2 min"
                               if self.alive() else "o OpenCode fechou ao iniciar")
            self.stop()
            return False

    def tools_status(self) -> tuple[bool, str]:
        """(conectado?, detalhe) das ferramentas de tela (MCP "desktop")."""
        try:
            st = (self._req("GET", "/mcp", timeout=30) or {}).get("desktop") or {}
        except Exception as exc:
            return False, f"sem resposta do OpenCode ({exc})"
        if st.get("status") == "connected":
            return True, "ok"
        detalhe = st.get("error") or st.get("status") or "desconhecido"
        return False, str(detalhe)

    def stop(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None:
            _kill_tree(proc)
        try:
            _PIDFILE().unlink()
        except Exception:
            pass


def _PIDFILE() -> Path:
    return app_dir() / ".opencode_server.pid"


def _kill_stale() -> None:
    try:
        pid = int(_PIDFILE().read_text(encoding="utf-8").strip())
    except Exception:
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, creationflags=_NO_WINDOW)


SERVER = _Server()


def _restart(monitor: int | None = None) -> bool:
    SERVER.stop()
    return SERVER.start(monitor)


INSTALL_STATE = {"status": "pendente", "erro": ""}


def warm_up(monitor: int | None = None, log=None) -> None:
    """Ao abrir o app: garante o OpenCode (instala se faltar) e liga o servidor."""
    log = log or (lambda m: None)

    def work():
        from . import installer
        INSTALL_STATE["status"] = "instalando"
        try:
            exe = installer.ensure(log)
            INSTALL_STATE["status"] = "ok"
            log(f"✔ [1/4] OpenCode {installer.PINNED_VERSION} pronto")
        except Exception as exc:
            INSTALL_STATE.update(status="erro", erro=str(exc))
            log(f"! [1/4] não consegui instalar o OpenCode: {exc}")
            return
        achados = discover_models(exe)
        if achados:
            log("✔ [2/4] modelos grátis com visão: "
                + ", ".join(m.split("/", 1)[1] for m in achados))
        else:
            log("⚠ [2/4] catálogo de modelos indisponível — usando a lista padrão")
        log("… [3/4] iniciando o servidor do OpenCode + ferramentas de tela (MCP)…")
        t0 = time.monotonic()
        if not SERVER.start(monitor):
            log(f"! [3/4] servidor do OpenCode não iniciou ({SERVER.last_error}) "
                "— as tarefas usarão o modo lento")
            return
        log(f"✔ [3/4] servidor do OpenCode no ar ({SERVER.url}, "
            f"{time.monotonic() - t0:.0f}s)")
        ok, det = SERVER.tools_status()
        if ok:
            log("✔ [4/4] ferramentas de tela conectadas — pronto para tarefas")
        else:
            log(f"! [4/4] ferramentas de tela NÃO conectaram: {det} "
                f"(detalhes em {app_dir() / 'logs' / 'mcp.log'})")

    threading.Thread(target=work, daemon=True).start()


def shutdown() -> None:
    SERVER.stop()


import atexit  # noqa: E402
atexit.register(shutdown)


def find_opencode() -> str | None:
    from . import installer
    return installer.find()      # pasta do app (auto-instalado) ou PATH


_DISCOVERED: list[str] = []
_REFUSED: set = set()        # recusaram nesta sessão (ex.: free tier) — pula


def discover_models(exe: str | None = None) -> list[str]:
    """Modelos GRÁTIS do Zen que ENXERGAM imagem, lidos do catálogo atual.

    O catálogo muda com frequência (modelos entram e saem); assim um modelo
    grátis novo entra na rotação sem atualizar o app. Space Bunny primeiro
    (único aceito com as ferramentas de tela em 25/09/2026).
    """
    exe = exe or find_opencode()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "models", "opencode", "--verbose"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=90,
                             env=opencode_env(app_dir()), cwd=str(app_dir()),
                             creationflags=_NO_WINDOW).stdout
    except Exception:
        return []
    achados = []
    for bloco in re.split(r"\n(?=opencode/)", out):
        nome, _, corpo = bloco.partition("\n")
        try:
            info = json.loads(corpo)
        except ValueError:
            continue
        custo = info.get("cost") or {}
        entrada = (info.get("capabilities") or {}).get("input") or {}
        if (custo.get("input") == 0 and custo.get("output") == 0
                and entrada.get("image") and info.get("status", "active") == "active"):
            achados.append(nome.strip())
    # os comprovados SEMPRE na frente: na 1ª execução o OpenCode usa um
    # catálogo embutido antigo (sem o Space Bunny) até atualizar pela rede
    prefer = [f"opencode/{m}" for m in DEFAULT_MODELS]
    achados = prefer + [m for m in achados if m not in prefer]
    _DISCOVERED[:] = achados
    return achados


def models() -> list[str]:
    raw = os.getenv("CUA_OPENCODE_MODELS", "").strip()
    lista = ([m.strip() for m in raw.split(",") if m.strip()]
             or list(_DISCOVERED) or list(DEFAULT_MODELS))
    lista = [m if "/" in m else f"opencode/{m}" for m in lista]
    vivos = [m for m in lista if m not in _REFUSED]
    return vivos or lista            # todos recusaram? tenta de novo a lista


def _next_model_error(msg: str) -> bool:
    """Falha em que vale tentar o próximo modelo (não é culpa da tarefa)."""
    m = msg.lower()
    return any(t in m for t in (
        "free tier", "quota", "rate limit", "429", "503", "500", "overloaded",
        "unavailable", "server error", "not found", "no endpoints",
        "insufficient", "credit", "timeout", "does not support image",
        "image input",
    ))


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=_NO_WINDOW, timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _fmt_tool(part: dict) -> str:
    tool = str(part.get("tool", "")).replace("desktop_", "")
    inp = (part.get("state") or {}).get("input") or {}
    args = ", ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}"
                     for k, v in inp.items())
    return f"> {tool}({args})"


class OpenCodeAgent:
    """Mesma interface do ComputerAgent: ``run(tarefa) -> mensagem``."""

    def __init__(self, screen, *, on_log: Callable[[str], None] | None = None,
                 on_screenshot: Callable[[str], None] | None = None,
                 should_stop: Callable[[], bool] | None = None,
                 step_timeout: float = 90.0):
        self.screen = screen
        self.log = on_log or print
        self.on_screenshot = on_screenshot or (lambda b: None)
        self.should_stop = should_stop or (lambda: False)
        self.step_timeout = step_timeout   # sem nenhum evento por este tempo
        self.typed: list[str] = []         # textos digitados (memória do loop)
        self.last_text = ""                # resposta final da última execução

    def _emit_shot(self) -> None:
        try:
            self.on_screenshot(self.screen.capture_b64())
        except Exception:
            pass

    def _fast_click(self, task: str) -> str | None:
        """Comando de 1 clique ("clica no Chelsea") resolvido SEM IA.

        Acha o texto na janela pela UI Automation, rola até ele e clica —
        ~2 s em vez de ~30 s de OpenCode. Devolve a mensagem final, ou None
        para seguir pelo OpenCode (texto não achado / comando complexo).
        """
        m = _FAST_RE.match(task.strip())
        if not m:
            return None
        alvo = m.group("alvo").strip(" .!?\"'")
        # comando composto ("... e depois ...") precisa da IA
        if not alvo or len(alvo) > 60 or re.search(r"\s(e|depois|então|,)\s", alvo):
            return None
        from . import actions, uia_find
        try:
            rect_mon = self.screen.monitor_rect()
            itens = uia_find.find(alvo, max_results=3, monitor_rect=rect_mon)
        except Exception:
            return None
        # só confia quando o nome bate de verdade (exato ou começa com)
        itens = [i for i in itens
                 if uia_find._norm(i["name"]).startswith(uia_find._norm(alvo))]
        if not itens:
            return None
        rect = None
        left, top, mw, mh = rect_mon
        for _ in range(20):
            rect = uia_find.reveal(itens[0])
            if rect is None:
                return None
            cy = (rect[1] + rect[3]) // 2
            if top <= cy < top + mh:
                break
            dist = cy - (top + mh // 2)
            clicks = max(2, min(15, abs(dist) // 100))
            actions._wheel(-clicks if dist > 0 else clicks,
                           (left + mw // 2, top + mh // 2), False)
            time.sleep(0.25)
        else:
            return None
        cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        import pyautogui
        pyautogui.click(cx, cy)
        self.log(f"⚡ clique direto em «{itens[0]['name'][:50]}» (sem IA)")
        time.sleep(0.3)
        self._emit_shot()
        return "Tarefa concluída."

    def run(self, task: str) -> str:
        rapido = self._fast_click(task)
        if rapido:
            return rapido
        exe = find_opencode()
        if not exe:
            raise RuntimeError("OpenCode não encontrado no PATH — instale com "
                               "'npm i -g opencode-ai' e tente de novo.")
        monitor = getattr(self.screen, "monitor", 1)
        usar_servidor = SERVER.alive() or SERVER.start(monitor)
        if usar_servidor:
            ok, det = SERVER.tools_status()
            if not ok:
                # ferramentas de tela fora: reinicia 1x antes de desistir
                self.log(f"⚠ ferramentas de tela desconectadas ({det}) — reiniciando o OpenCode")
                if _restart(monitor):
                    ok, det = SERVER.tools_status()
                if not ok:
                    raise RuntimeError(
                        f"As ferramentas de tela não conectaram: {det}. "
                        f"Detalhes em {app_dir() / 'logs' / 'mcp.log'}")
        else:
            self.log(f"! servidor do OpenCode não subiu ({SERVER.last_error}) — usando o modo lento")
        root = ensure_workspace() if not usar_servidor else SERVER.root
        env = opencode_env(root, monitor)
        tentativas = models()
        ultimo = ""
        for i, model in enumerate(tentativas, 1):
            if self.should_stop():
                raise safety.Abort("Parada solicitada pelo usuário.")
            curto = model.split("/", 1)[-1]
            self.log(f"🤖 OpenCode · {curto} ({i}/{len(tentativas)})")
            if usar_servidor:
                ok, msg, agiu = self._run_server(model, task)
                if not ok and msg == "FERRAMENTAS_TRAVADAS":
                    self.log("⚠ ferramentas travaram — reiniciando o OpenCode e tentando de novo")
                    _restart(monitor)
                    ok, msg, agiu = self._run_server(model, task)
                    if not ok and msg == "FERRAMENTAS_TRAVADAS":
                        raise RuntimeError("As ferramentas de tela não respondem, "
                                           "mesmo após reiniciar o OpenCode.")
            else:
                ok, msg, agiu = self._run_model(exe, root, env, model, task)
            if ok:
                return msg or "Tarefa concluída."
            ultimo = msg
            if agiu:
                # já mexeu na tela: recomeçar em outro modelo repetiria ações
                raise RuntimeError(f"{curto} falhou no meio da tarefa: {msg}")
            if not _next_model_error(msg):
                raise RuntimeError(msg)
            _REFUSED.add(model)
            self.log(f"⚠ {curto} indisponível ({msg[:90]}) — próximo modelo")
        raise RuntimeError(f"Nenhum modelo gratuito disponível agora. Último: {ultimo}")

    def _run_server(self, model: str, task: str):
        """Uma tentativa pelo servidor quente. (sucesso, mensagem, já_agiu)."""
        import base64
        import io
        prov, mid = model.split("/", 1)
        sid = SERVER._req("POST", "/session", {"title": "helpdesk"})["id"]
        parts = [{"type": "text", "text": task}]
        try:    # tela anexada: o modelo já começa vendo (poupa 1 chamada)
            buf = io.BytesIO()
            _for_ai(self.screen.capture()).save(buf, format="JPEG", quality=60)
            parts.insert(0, {"type": "file", "mime": "image/jpeg",
                             "filename": "tela.jpg",
                             "url": "data:image/jpeg;base64,"
                                    + base64.b64encode(buf.getvalue()).decode()})
        except Exception:
            pass
        box: dict = {}

        def enviar():
            try:
                box["res"] = SERVER._req(
                    "POST", f"/session/{sid}/message",
                    {"parts": parts, "model": {"providerID": prov, "modelID": mid}},
                    timeout=600)
            except Exception as exc:
                box["err"] = str(exc)

        th = threading.Thread(target=enviar, daemon=True)
        th.start()
        vistos: set = set()
        agiu, ultimo_evento = False, time.monotonic()
        erros_seguidos = 0
        while True:
            if self.should_stop():
                try:
                    SERVER._req("POST", f"/session/{sid}/abort", timeout=5)
                except Exception:
                    pass
                # a ferramenta interrompida pode ter ficado presa: servidor novo
                threading.Thread(target=_restart, args=(getattr(self.screen, "monitor", 1),),
                                 daemon=True).start()
                raise safety.Abort("Parada solicitada pelo usuário.")
            th.join(0.5)
            try:
                msgs = SERVER._req("GET", f"/session/{sid}/message", timeout=10) or []
            except Exception:
                msgs = []
            for m in msgs:
                for p in m.get("parts") or []:
                    st = (p.get("state") or {}).get("status")
                    if p.get("type") != "tool" or st not in ("completed", "error"):
                        continue
                    if p.get("id") in vistos:
                        continue
                    vistos.add(p.get("id"))
                    ultimo_evento = time.monotonic()
                    if p.get("tool") != "desktop_screenshot":
                        agiu = True
                        self.log(_fmt_tool(p))
                    if p.get("tool") == "desktop_type_text":
                        txt = ((p.get("state") or {}).get("input") or {}).get("text")
                        if txt:
                            self.typed.append(str(txt))
                    if st == "error":
                        self.log(f"! {(p.get('state') or {}).get('error', '')}"[:200])
                        erros_seguidos += 1
                    else:
                        erros_seguidos = 0
                    self._emit_shot()
            if erros_seguidos >= 2:
                # ferramentas quebradas (servidor MCP travado): não adianta
                # o modelo insistir — o chamador reinicia e tenta de novo
                try:
                    SERVER._req("POST", f"/session/{sid}/abort", timeout=5)
                except Exception:
                    pass
                return False, "FERRAMENTAS_TRAVADAS", agiu
            if not th.is_alive():
                break
            if time.monotonic() - ultimo_evento > self.step_timeout:
                try:
                    SERVER._req("POST", f"/session/{sid}/abort", timeout=5)
                except Exception:
                    pass
                return False, f"sem resposta por {int(self.step_timeout)}s (timeout)", agiu
        if "err" in box:
            return False, box["err"], agiu
        try:     # leitura final (a última volta do laço pode ser anterior ao fim)
            msgs = SERVER._req("GET", f"/session/{sid}/message", timeout=10) or msgs
        except Exception:
            pass
        texto, erro = "", ""
        for m in msgs:
            info = m.get("info") or {}
            if info.get("role") != "assistant":
                continue
            if info.get("error"):
                e = info["error"]
                erro = str((e.get("data") or {}).get("message") or e.get("name") or e)
            for p in m.get("parts") or []:
                if p.get("type") == "text" and p.get("text", "").strip():
                    texto = p["text"].strip()
        if erro and not texto:
            return False, erro, agiu
        self.last_text = texto
        if texto:
            self.log(texto)
        return True, "Tarefa concluída.", agiu

    def _run_model(self, exe, root, env, model, task):
        """Roda um modelo. Retorna (sucesso, mensagem, já_agiu_na_tela)."""
        # --title pronto: evita a chamada extra a outro modelo só p/ nomear a sessão
        cmd = [exe, "run", "-m", model, "--title", "helpdesk", "--format", "json"]
        # tela anexada: o modelo já começa vendo — poupa 1 chamada (~7 s)
        try:
            shot = root / "tela_atual.jpg"
            _for_ai(self.screen.capture()).save(shot, format="JPEG", quality=60)
            cmd += ["-f", str(shot)]
        except Exception:
            pass
        cmd += ["--", task]
        proc = subprocess.Popen(
            cmd, cwd=str(root), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        lines: list[str] = []
        last_event = [time.monotonic()]

        def reader():
            for line in proc.stdout:
                lines.append(line)
                last_event[0] = time.monotonic()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        texto, erro, agiu, idx = "", "", False, 0
        try:
            while True:
                while idx < len(lines):
                    ev_line = lines[idx]
                    idx += 1
                    try:
                        ev = json.loads(ev_line)
                    except ValueError:
                        continue
                    kind, part = ev.get("type"), ev.get("part") or {}
                    if kind == "tool_use":
                        status = (part.get("state") or {}).get("status")
                        if status in ("completed", "error"):
                            if part.get("tool") != "desktop_screenshot":
                                agiu = True
                                self.log(_fmt_tool(part))
                            if status == "error":
                                self.log(f"! {(part.get('state') or {}).get('error', '')}"[:200])
                            self._emit_shot()
                    elif kind == "text":
                        texto = str(part.get("text", "")).strip() or texto
                    elif kind == "error":
                        data = (ev.get("error") or {}).get("data") or {}
                        erro = str(data.get("message") or ev.get("error"))
                if self.should_stop():
                    _kill_tree(proc)
                    raise safety.Abort("Parada solicitada pelo usuário.")
                if proc.poll() is not None and not t.is_alive() and idx >= len(lines):
                    break
                if time.monotonic() - last_event[0] > self.step_timeout:
                    _kill_tree(proc)
                    erro = f"sem resposta por {int(self.step_timeout)}s (timeout)"
                    break
                time.sleep(0.1)
        finally:
            if proc.poll() is None:
                _kill_tree(proc)
        if erro:
            return False, erro, agiu
        self.last_text = texto
        if texto:
            self.log(texto)
        return True, "Tarefa concluída.", agiu
