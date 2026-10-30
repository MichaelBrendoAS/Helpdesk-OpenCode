"""Loop do agente: tela -> modelo (API compatível com OpenAI) -> ação -> tela -> ...

O agente é independente de interface: recebe *callbacks* para log, exibição
da tela, confirmação de ações e parada — assim a mesma classe serve tanto
o CLI (`main.py`, com `input()`) quanto a GUI (`main_gui.py`, com diálogos).

Protocolo: em vez da ferramenta nativa `computer` (só da Anthropic), o
modelo responde a cada passo com UM objeto JSON no formato
`{"action": "...", ...}` — funcionando com qualquer API compatível com
OpenAI (Gemini, MiMo, OpenRouter, ...). Só a captura mais recente fica no
histórico como imagem; as antigas viram texto para economizar tokens.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from openai import OpenAI

_CLIENTS: dict = {}
# memória da sessão do app (sobrevive entre tarefas): modelo que está
# respondendo, sem cota hoje e ocupados recentemente
_SESSION: dict = {"good": None, "dead": set(), "busy_until": {}}
_BUSY_COOLDOWN = 300.0   # ocupado agora = fim da fila por 5 min
_TIMEOUT = 30.0   # resposta normal: 5-15 s; passou disso, troca de modelo
_NO_REASONING: set = set()   # modelos que recusam reasoning_effort=minimal


def _client_for(api_key: str, base_url: str) -> OpenAI:
    """Cliente reutilizado entre tarefas — mantém a conexão TLS quente."""
    key = (api_key, base_url)
    if key not in _CLIENTS:
        _CLIENTS[key] = OpenAI(api_key=api_key, base_url=base_url,
                               timeout=_TIMEOUT, max_retries=0)
    return _CLIENTS[key]

from . import actions, safety
from .config import Config
from .screen import Screen

# assinaturas dos callbacks
Approver = Callable[[dict], str]   # "ok" | "skip"  (pode levantar safety.Abort)
LogFn = Callable[[str], None]
ShotFn = Callable[[str], None]     # recebe imagem em base64
StopFn = Callable[[], bool]

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_MAX_HISTORY = 12        # mensagens (fora o system) mantidas entre passos/tarefas
_MAX_BAD_JSON = 3        # respostas inválidas seguidas antes de desistir
_PLACEHOLDER = "[tela antiga]"

# tolerância a variações que modelos costumam emitir (o protocolo deve ser
# robusto: um apelido tipo "click" não pode quebrar o actions.py)
_ACTION_ALIASES = {
    "click": "left_click",
    "leftclick": "left_click",
    "left_clicks": "left_click",
    "rightclick": "right_click",
    "doubleclick": "double_click",
    "tripleclick": "triple_click",
    "move": "mouse_move",
    "mousemove": "mouse_move",
    "type_text": "type",
    "writetext": "type",
    "write": "type",
    "press": "key",
    "presskey": "key",
    "press_key": "key",
    "hotkey": "key",
    "key_combo": "key",
    "drag": "left_click_drag",
    "drag_to": "left_click_drag",
    "sleep": "wait",
    "pause": "wait",
    "open": "open_app",
    "launch": "open_app",
    "run": "open_app",
    "start_app": "open_app",
    "open_program": "open_app",
    "open_application": "open_app",
    "abrir": "open_app",
    "abrir_programa": "open_app",
    "abrir_aplicativo": "open_app",
}

# "terminou a tarefa" — modelos adoram JSON para encerrar; é fim, não ação
_FINISH_ACTIONS = {
    "finish", "finished", "done", "task_done", "task_complete",
    "task_completed", "final_answer", "conclude", "concluded", "concluir",
    "finalizar", "terminar", "complete", "completed", "success", "end", "fim",
}

_COORD_KEYS = ("coordinate", "coordinates", "coords", "coord", "pos",
               "position", "point", "location")


def _transient(exc: BaseException) -> bool:
    """Erro que costuma passar sozinho (sobrecarga, limite, rede)."""
    msg = str(exc).lower()
    return any(t in msg for t in (
        "503", "unavailable", "high demand", "overloaded", "429",
        "rate limit", "resource_exhausted", "500", "502", "504",
        "timeout", "timed out", "connection"))


def _dead(msg: str) -> bool:
    """Falha que não passa em segundos: cota do dia ou modelo inexistente."""
    return ("perday" in msg or "per day" in msg or "model_not_found" in msg
            or "not found" in msg and "model" in msg
            or "does not exist" in msg)


def _why_dead(msg: str) -> str:
    return ("cota grátis do dia esgotada" if "day" in msg
            else "modelo indisponível")


def _short(exc: BaseException) -> str:
    msg = str(exc)
    for code in ("503", "429", "500", "502", "504"):
        if code in msg:
            return f"erro {code}"
    return "falha de rede" if "onnect" in msg or "imeout" in msg else "erro"


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "sim", "yes")


def _num(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return value


def _as_xy(value):
    """Converte o valor da coordenada em [x, y] (aceita dict/string/lista)."""
    if isinstance(value, dict) and "x" in value and "y" in value:
        return [_num(value["x"]), _num(value["y"])]
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [_num(value[0]), _num(value[1])]
    if isinstance(value, str) and "," in value:
        a, _, b = value.partition(",")
        pair = (a.strip(), b.strip())
        try:
            return [_num(pair[0]), _num(pair[1])]
        except Exception:
            return None
    return None


def _as_text(value):
    """key/combo como lista -> 'ctrl+s' (formato que o actions.py espera)."""
    if isinstance(value, (list, tuple)):
        return "+".join(str(v) for v in value)
    return value


def normalize_action(obj: dict) -> dict:
    """Deixa a ação no formato canônico esperado por `actions.execute`."""
    name = str(obj.get("action", "")).strip().lower().replace("-", "_").replace(" ", "_")
    name = _ACTION_ALIASES.get(name, name)
    out = dict(obj)
    out["action"] = name

    # coordenada: coords/pos/dict/x-y/string -> coordinate [x, y]
    src = None
    if "coordinate" in out:
        src = "coordinate"
    else:
        for key in _COORD_KEYS:
            if key in out:
                src = key
                break
    if src is not None:
        raw = out.pop(src)
        xy = _as_xy(raw)
        if xy is not None:
            out["coordinate"] = xy
        elif src == "coordinate":
            out["coordinate"] = raw
    elif "x" in out and "y" in out:
        out["coordinate"] = [_num(out.pop("x")), _num(out.pop("y"))]

    if "start_coordinate" not in out:
        for key in ("start", "start_pos", "start_position", "origin", "from"):
            if key in out:
                xy = _as_xy(out.pop(key))
                if xy is not None:
                    out["start_coordinate"] = xy
                break

    if name == "type" and "text" not in out:
        for key in ("value", "string", "content", "typed_text", "input"):
            if key in out:
                out["text"] = _as_text(out.pop(key))
                break

    if name == "key" and "text" not in out:
        for key in ("keys", "key", "key_combo", "combination", "combo", "hotkey"):
            if key in out:
                out["text"] = _as_text(out.pop(key))
                break

    if name == "open_app" and "name" not in out:
        for key in ("app", "application", "program", "programa", "text", "title"):
            if key in out:
                out["name"] = _as_text(out.pop(key))
                break

    if name in ("wait", "hold_key") and "duration" not in out:
        for key in ("seconds", "secs", "time", "seconds_to_wait"):
            if key in out:
                out["duration"] = out.pop(key)
                break

    if name == "scroll":
        if "scroll_direction" not in out:
            out["scroll_direction"] = out.pop("direction", None) \
                or out.pop("axis", None) or "vertical"
        if "amount" not in out:
            for key in ("count", "delta", "units", "scroll_amount"):
                if key in out:
                    out["amount"] = out.pop(key)
                    break

    return out


def extract_action(text: str) -> tuple[dict | None, bool]:
    """Procura a ação JSON na resposta do modelo.

    Retorna ``(ação, tentou_json)``:
    * ``ação`` — dict com "action" quando encontrou um JSON válido;
    * ``tentou_json`` — True se o texto parecia tentar um JSON (havia ``{`` e
      ``}``) mas veio inválido (aí o loop pede para reenviar em vez de
      encerrar a tarefa).
    """
    raw = (text or "").strip()
    if not raw:
        return None, False

    candidates: list[str] = []
    fence = _FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])

    tried = bool(candidates)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("action"), str):
            return normalize_action(obj), True
    return None, tried


def extract_actions(text: str) -> tuple[list[dict], bool]:
    """Extrai UMA ou VÁRIAS ações da resposta do modelo.

    Formatos aceitos:
      * ``{"action":"left_click","coordinate":[x,y]}``  (um objeto — o clássico)
      * ``{"actions":[{...},{...}]}``                   (lote: várias por chamada)
      * ``[{...},{...}]``                               (lista crua)

    Retorna ``(lista_normalizada, parecia_json)``. O segundo elemento diz se
    vale a pena pedir a resposta de novo (havia JSON mas estava quebrado).
    Lista vazia com ``parecia_json=False`` = o modelo respondeu texto = fim.
    """
    raw = (text or "").strip()
    if not raw:
        return [], False

    candidates: list[str] = []
    fence = _FENCE_RE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    astart, aend = raw.find("["), raw.rfind("]")
    if 0 <= astart < aend:
        candidates.append(raw[astart:aend + 1])

    # JSON cortado (ex.: limite de tokens) NÃO é "texto final": sem isto a
    # tarefa terminava como concluída sem ter feito nada
    tried = (bool(candidates) or raw.startswith(("{", "[", "```"))
             or '"action' in raw)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        items: list | None = None
        if isinstance(obj, dict) and isinstance(obj.get("actions"), list):
            items = obj["actions"]
        elif isinstance(obj, dict) and isinstance(obj.get("action"), str):
            items = [obj]
        elif isinstance(obj, list):
            items = obj
        if items is None:
            continue
        out = [
            normalize_action(it) for it in items
            if isinstance(it, dict) and isinstance(it.get("action"), str)
        ]
        if out:
            # "done" no objeto do lote vale para a última ação do lote
            if isinstance(obj, dict) and obj.get("done") and "actions" in obj:
                out[-1]["done"] = True
            return out[:6], True      # teto de segurança por lote
    return [], tried


_OBS_RE = re.compile(r'"observation"\s*:\s*"((?:[^"\\]|\\.)*)"')


def extract_observations(text: str) -> list[str]:
    """Campo opcional "observation": o que a IA identificou na tela.

    Só para exibir no log do usuário (🧠 ...) — nunca interfere na
    extração das ações. Aceita o JSON único ou o do lote.
    """
    out: list[str] = []
    for m in _OBS_RE.finditer(text or ""):
        try:
            s = json.loads('"' + m.group(1) + '"')
        except (ValueError, TypeError):
            s = m.group(1)
        s = str(s).strip()[:200]
        if s and s not in out:
            out.append(s)
    return out


class ComputerAgent:
    """Mantém a conversa com o modelo e executa as ações de tela."""

    def __init__(
        self,
        cfg: Config,
        screen: Screen,
        *,
        approver: Approver | None = None,
        on_log: LogFn | None = None,
        on_screenshot: ShotFn | None = None,
        should_stop: StopFn | None = None,
    ):
        self.cfg = cfg
        self.screen = screen
        # rota da visão: Gemini principal, Groq = plano B (se houver chave)
        rota = cfg.vision_route() if hasattr(cfg, "vision_route") else []
        if not rota:
            rota = [(getattr(cfg, "api_key", ""),
                     getattr(cfg, "base_url", Config.base_url),
                     getattr(cfg, "model", Config.model), True)]
        (k0, b0, m0, e0), *demais = rota
        self.client = _client_for(k0, b0)
        self.model_primary = m0
        self.effort_primary = e0
        if demais:
            k1, b1, m1, _ = demais[0]
            self.fallback_client = _client_for(k1, b1)
            self.fallback_model = m1
        else:
            self.fallback_client = None
            self.fallback_model = None

        # callbacks — padrões = comportamento do CLI (console/input)
        self.approver: Approver = approver or (
            lambda action: safety.approve(action, cfg.auto_approve)
        )
        self.log: LogFn = on_log or (lambda msg: print(msg))
        self.on_screenshot: ShotFn = on_screenshot or (lambda b64: None)
        self.should_stop: StopFn = should_stop or (lambda: False)

        # histórico da conversa — persiste entre tarefas no modo interativo
        self.messages: list[dict[str, Any]] = []

    # ------------------------------------------------------------ montagem

    def _image_block(self, b64: str) -> dict:
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        }

    def _strip_old_images(self) -> None:
        """Mantém só a imagem mais recente no histórico (economia de tokens)."""
        for msg in self.messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "image_url":
                    content[i] = {"type": "text", "text": _PLACEHOLDER}

    def _append_user(self, content: list[dict]) -> None:
        """Adiciona mensagem do usuário (mesclando se a anterior também for user).

        Só descarta as imagens antigas quando uma imagem nova está chegando —
        assim a ação "pular" (sem captura nova) não deixa o modelo às cegas.
        """
        has_image = any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        )
        if has_image:
            self._strip_old_images()
        if self.messages and self.messages[-1]["role"] == "user":
            last = self.messages[-1]["content"]
            if isinstance(last, str):
                last = [{"type": "text", "text": last}]
            self.messages[-1]["content"] = last + content
        else:
            self.messages.append({"role": "user", "content": content})

    def _trim_history(self) -> None:
        """Limita o histórico (tokens e latência crescem a cada passo).

        Mantém o system e as últimas mensagens, sempre começando num turno
        do usuário para a conversa continuar válida para a API.
        """
        head = self.messages[:1] if (
            self.messages and self.messages[0].get("role") == "system") else []
        body = self.messages[len(head):]
        if len(body) <= _MAX_HISTORY:
            return
        body = body[-_MAX_HISTORY:]
        while body and body[0].get("role") != "user":
            body.pop(0)
        self.messages = head + body

    def _exec(self, action: dict) -> str:
        """Executa a ação; parada no meio de uma espera vira Abort."""
        orig = action
        action = self._denormalize(action)
        try:
            _, note = actions.execute(action, self.screen, self.should_stop)
        except actions.Interrupted as exc:
            raise safety.Abort(str(exc)) from exc
        # o log/feedback fala a língua do modelo (0-1000), não pixels
        note = note.split(" | cursor")[0]
        xy = orig.get("coordinate")
        if isinstance(xy, (list, tuple)) and len(xy) >= 2:
            note = re.sub(r"\(-?\d+, -?\d+\)", f"({xy[0]}, {xy[1]})", note,
                          count=1)
        return note

    def _denormalize(self, action: dict) -> dict:
        """Coordenadas 0–1000 do modelo -> pixels da imagem enviada.

        Gemini e Qwen-VL apontam nativamente nessa grade normalizada; pedir
        pixels fazia os cliques caírem deslocados (medido: ~50% do alvo).
        """
        mw, mh = self.screen.model_size()
        out = dict(action)
        for key in ("coordinate", "start_coordinate"):
            xy = out.get(key)
            if (isinstance(xy, (list, tuple)) and len(xy) >= 2
                    and all(isinstance(v, (int, float)) for v in xy[:2])):
                x = min(max(float(xy[0]), 0.0), 1000.0)
                y = min(max(float(xy[1]), 0.0), 1000.0)
                out[key] = [min(mw - 1, round(x * mw / 1000)),
                            min(mh - 1, round(y * mh / 1000))]
        return out

    def _pause(self, secs: float) -> None:
        try:
            actions._sleep(secs, self.should_stop)
        except actions.Interrupted as exc:
            raise safety.Abort(str(exc)) from exc

    def _emit_screenshot(self, b64: str | None = None) -> None:
        """Envia a tela atual para quem exibe (GUI), sem quebrar o loop se falhar."""
        try:
            self.on_screenshot(b64 if b64 is not None else self.screen.capture_b64())
        except Exception:
            pass

    def _call_api(self) -> str:
        """Chamada à API numa thread auxiliar, abortável pelo ■ Parar.

        A requisição HTTP em si não pode ser cancelada; ao parar, a thread
        é abandonada (daemon) e o loop sai na hora em vez de esperar o
        timeout da API.
        """
        box: dict = {}
        messages = list(self.messages)   # snapshot: o loop pode seguir mexendo

        def work() -> None:
            try:
                box["ok"] = self._call_api_sync(messages)
            except BaseException as exc:  # repassa para a thread do loop
                box["err"] = exc

        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            if self.should_stop():
                raise safety.Abort("Parada solicitada pelo usuário.")
            t.join(0.1)
        if "err" in box:
            raise box["err"]
        return box.get("ok", "")

    def _model_chain(self) -> list[tuple]:
        """(cliente, modelo, esforço) na ordem de tentativa.

        Principal -> reservas do mesmo provedor (CUA_FALLBACK_MODELS) ->
        plano B (Groq). A memória é da SESSÃO do app (não da tarefa):
        começa pelo último modelo que respondeu, pula os sem cota hoje e
        deixa por último os que estavam ocupados nos últimos minutos.
        """
        primary = getattr(self, "model_primary", None) or self.cfg.model
        esforco = getattr(self, "effort_primary", True)
        spares = os.getenv(
            "CUA_FALLBACK_MODELS",
            os.getenv("CUA_FALLBACK_MODEL",
                      "gemini-3.6-flash,gemini-3.5-flash,gemini-3.7-flash"))
        chain = [(self.client, primary, esforco)]
        for m in (x.strip() for x in spares.split(",")):
            if m and all(m != c[1] for c in chain):
                chain.append((self.client, m, esforco))
        fb = getattr(self, "fallback_client", None)
        fb_model = getattr(self, "fallback_model", None)
        if fb is not None and fb_model:
            chain.append((fb, fb_model, False))
        chain = [c for c in chain if c[1] not in _SESSION["dead"]]
        agora = time.monotonic()
        busy = _SESSION["busy_until"]
        chain.sort(key=lambda c: (c[1] != _SESSION["good"],
                                  busy.get(c[1], 0) > agora))
        return chain

    def _call_api_sync(self, messages: list) -> str:
        """Percorre a cadeia de modelos até um responder — sem insistir.

        * cota do dia esgotada / modelo inexistente -> fora da sessão;
        * ocupado (503/429/timeout) -> vai direto para o próximo e esse
          modelo fica no fim da fila por alguns minutos.
        Só um aviso no log quando o modelo em uso MUDA.
        """
        last: Exception | None = None
        falhas: list[str] = []
        for client, model, esforco in self._model_chain():
            try:
                text = self._chat(client, model, esforco, messages)
            except Exception as exc:
                last = exc
                msg = str(exc).lower()
                if _dead(msg):
                    _SESSION["dead"].add(model)
                    falhas.append(f"{model} ({_why_dead(msg)})")
                    continue
                if not _transient(exc):
                    raise
                _SESSION["busy_until"][model] = time.monotonic() + _BUSY_COOLDOWN
                falhas.append(f"{model} ocupado")
                continue
            if model != _SESSION["good"]:
                if falhas or _SESSION["good"] is not None:
                    motivo = f"{'; '.join(falhas)} — " if falhas else ""
                    self.log(f"↪ {motivo}usando {model}")
                _SESSION["good"] = model
            return text
        if last is None:
            raise RuntimeError("Nenhum modelo disponível agora (todos sem cota hoje).")
        raise RuntimeError(
            "Todos os modelos estão ocupados ou sem cota agora "
            f"({'; '.join(falhas)}). Tente de novo em alguns minutos.")

    def _chat(self, client, model: str, esforco: bool,
              messages: list | None = None) -> str:
        """Uma chamada ao provedor indicado (com corte do reasoning_effort)."""
        params = dict(
            model=model,
            max_tokens=self.cfg.max_tokens,
            messages=messages if messages is not None else self.messages,
        )
        if esforco and model not in _NO_REASONING:
            # thinking mínimo = resposta mais rápida (bench: ~2x no Gemini)
            params["reasoning_effort"] = "minimal"
        try:
            response = client.chat.completions.create(**params)
        except Exception as exc:
            msg = str(exc)
            aceitou_param = (
                "reasoning_effort" in msg
                or "unknown name" in msg.lower()
                or "unknown parameter" in msg.lower()
                or "thinking level" in msg.lower()
            )
            if model in _NO_REASONING or not aceitou_param:
                raise RuntimeError(f"Falha na chamada da API: {exc}") from exc
            # este endpoint não entende o parâmetro: repete sem ele (1x)
            _NO_REASONING.add(model)   # só este modelo; os outros mantêm
            params.pop("reasoning_effort", None)
            try:
                response = client.chat.completions.create(**params)
            except Exception as exc2:
                raise RuntimeError(
                    f"Falha na chamada da API: {exc2}") from exc2

        choice = response.choices[0] if response.choices else None
        text = (getattr(choice.message, "content", "") or "") if choice else ""
        if choice is not None and getattr(choice, "finish_reason", "") == "length":
            self.log(
                "[aviso] Limite de tokens atingido — a resposta foi cortada. "
                "Aumente CUA_MAX_TOKENS se isso se repetir."
            )
        # modelos de raciocínio (Qwen) mandam <think>...</think> antes do JSON
        return _THINK_RE.sub("", text).strip()

    def _ensure_system(self) -> None:
        """Envia o prompt de sistema + inventário do PC (uma vez por sessão).

        Sem isto o modelo não recebe as regras do protocolo JSON e acaba
        adivinhando nomes de ação — gerando erros, retries e lentidão.
        """
        if self.messages and self.messages[0].get("role") == "system":
            return
        # a lista de programas NÃO vai no prompt (~1000 tokens por passo):
        # open_app resolve o nome localmente, com busca aproximada
        content = self.cfg.system_prompt
        try:
            from . import inventory
            data = inventory.scan()
            content += f"\nSO: {data['so']}."
            self.log(
                f"📋 inventário: {len(data['programas'])} programas · {data['so']}"
            )
        except Exception:
            pass
        self.messages.insert(0, {"role": "system", "content": content})

    def _open_repetido(self, action: dict) -> str | None:
        """Detecta a MESMA abertura duas vezes seguidas (não lança de novo).

        Devolve o nome quando a ação atual é open_app igual à anterior e ainda
        não esperamos a janela — assim o agente mostra a tela em vez de abrir
        o programa em duplicata. A 3ª tentativa idêntica passa (recuperação
        caso a 1ª abertura tenha realmente falhado).
        """
        if str(action.get("action", "")) != "open_app":
            return None
        nome = str(action.get("name") or action.get("text") or action.get("app")
                   or action.get("application") or "").strip().lower()
        ultimo = getattr(self, "_last_open", None)
        if not nome or not ultimo or nome != ultimo:
            return None
        if getattr(self, "_open_waited", False):
            return None            # já demos uma chance: deixa ele reabrir
        self._open_waited = True
        return str(action.get("name") or action.get("app") or nome)

    # ------------------------------------------------------------------ loop

    def run(self, task: str) -> str:
        """Executa uma tarefa até a conclusão (ou limite de passos/parada).

        Retorna a mensagem de encerramento (a GUI exibe o status real).
        """
        self._ensure_system()
        # anti-duplicação: só aberturas CONSECUTIVAS contam (estado por tarefa)
        self._last_open = None
        self._open_waited = False
        self._task_text = task.strip()
        width, height = self.screen.model_size()
        first_shot = self.screen.capture_b64()
        self._emit_screenshot(first_shot)
        self._append_user([
            {"type": "text",
             "text": "Tela atual:"},
            self._image_block(first_shot),
            {"type": "text", "text": f"Tarefa do usuário:\n{task.strip()}"},
        ])

        bad_json = 0
        for step in range(1, self.cfg.max_steps + 1):
            if self.should_stop():
                raise safety.Abort("Parada solicitada pelo usuário.")
            self._trim_history()

            self.log(f"— passo {step} —")

            reply = self._call_api()
            actions_list, tried_json = extract_actions(reply)

            # o que a IA identificou na tela aparece no log do usuário
            for obs in extract_observations(reply)[:2]:
                self.log(f"🧠 {obs}")

            if not actions_list:
                if tried_json and bad_json < _MAX_BAD_JSON:
                    bad_json += 1
                    # parecia um JSON mas veio inválido: pede de novo
                    self.log("! resposta sem JSON válido — pedindo de novo")
                    self.messages.append({"role": "assistant", "content": reply})
                    self.messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text":
                                     "JSON inválido. Responda somente com um único "
                                     "objeto JSON no formato "
                                     '{"action":"...","coordinate":[x,y]} — sem '
                                     "texto fora dele."}],
                    })
                    continue
                if tried_json:
                    self.log("! o modelo insistiu em JSON inválido — encerrando.")
                    return "Encerrado: respostas inválidas do modelo."
                # texto simples = tarefa concluída
                if reply.strip():
                    self.log(reply.strip())
                return "Tarefa concluída."

            bad_json = 0
            # {"action":"finish"...} também é conclusão (não é ação de tela)
            if len(actions_list) == 1 and actions_list[0]["action"] in _FINISH_ACTIONS:
                final = str(
                    actions_list[0].get("text") or actions_list[0].get("message")
                    or actions_list[0].get("response") or ""
                ).strip()
                self.log(final or "Tarefa concluída.")
                return "Tarefa concluída."
            if len(actions_list) > 1:
                for fin in actions_list:
                    if fin["action"] in _FINISH_ACTIONS:
                        final = str(
                            fin.get("text") or fin.get("message")
                            or fin.get("response") or ""
                        ).strip()
                        self.log(final or "Tarefa concluída.")
                        return "Tarefa concluída."
                self.log(f"== lote de {len(actions_list)} ações em 1 chamada ==")

            self.messages.append({"role": "assistant", "content": reply})

            if self.should_stop():
                raise safety.Abort("Parada solicitada pelo usuário.")

            if len(actions_list) == 1:
                # -------- caminho clássico: uma ação por passo --------
                action = actions_list[0]
                repetida = self._open_repetido(action)
                if repetida is not None:
                    # mesma abertura duas vezes seguidas: em vez de lançar de
                    # novo (calculadora em dobro), só espera a janela aparecer
                    self.log(f"(«{repetida}» já pedido — só aguardando a janela)")
                    self._pause(1.5)
                    feedback = (
                        f"Não abri de novo: «{repetida}» já foi solicitado no "
                        "passo anterior; aguardei 1,5 s e segue a tela nova. "
                        "Se a janela NÃO abriu, mande open_app outra vez; se "
                        "abriu, CONTINUE a tarefa do usuário daqui — só "
                        "responda texto final quando TODA a tarefa estiver "
                        "cumprida.\nTarefa do usuário: "
                        f"{getattr(self, '_task_text', '')}"
                    )
                else:
                    try:
                        decision = self.approver(action)
                    except safety.Abort:
                        raise
                    if decision == "skip":
                        self.log("(ação pulada pelo usuário)")
                        self._last_open = None
                        self._open_waited = False
                        feedback = ("O usuário pulou esta ação (não foi "
                                    "executada). Escolha outra forma de "
                                    "continuar a tarefa.")
                        self._append_user([{"type": "text", "text": feedback}])
                        continue
                    try:
                        note = self._exec(action)
                        self.log(f"> {note}")
                        feedback = f"Executado: {note}."
                    except safety.Abort:
                        raise
                    except Exception as exc:
                        self.log(f"! erro: {exc}")
                        feedback = f"Erro ao executar a ação: {exc}"
                        action = {"action": ""}   # falhou: libera nova tentativa
                    # estado anti-duplicação (só aberturas consecutivas "seguem")
                    if str(action.get("action", "")) == "open_app":
                        self._last_open = str(
                            action.get("name") or action.get("text")
                            or action.get("app") or action.get("application")
                            or "").strip().lower()
                        self._open_waited = False
                    else:
                        self._last_open = None
                        self._open_waited = False
            else:
                # -------- lote: várias ações numa única chamada de API --------
                notes: list[str] = []
                executed = 0
                for act in actions_list:
                    if self.should_stop():
                        raise safety.Abort("Parada solicitada pelo usuário.")
                    repetida = self._open_repetido(act)
                    if repetida is not None:
                        self.log(f"(«{repetida}» já pedido — só aguardando)")
                        self._pause(1.5)
                        notes.append(f"«{repetida}» duplicado — só aguardei "
                                     "a janela")
                        executed += 1
                        continue
                    try:
                        decision = self.approver(act)
                    except safety.Abort:
                        raise
                    if decision == "skip":
                        self.log("(ação pulada pelo usuário)")
                        self._last_open = None
                        self._open_waited = False
                        notes.append("pulada pelo usuário")
                        continue
                    try:
                        note = self._exec(act)
                        self.log(f"> {note}")
                        notes.append(note)
                        executed += 1
                    except safety.Abort:
                        raise
                    except Exception as exc:
                        # tela em estado incerto: corta o resto do lote
                        self.log(f"! erro: {exc}")
                        notes.append(f"erro em {act.get('action')}: {exc}")
                        self._last_open = None
                        self._open_waited = False
                        break
                    if str(act.get("action", "")) == "open_app":
                        self._last_open = str(
                            act.get("name") or act.get("text") or act.get("app")
                            or act.get("application") or "").strip().lower()
                        self._open_waited = False
                    else:
                        self._last_open = None
                        self._open_waited = False

                summary = "; ".join(notes)
                if executed:
                    feedback = f"Executado: {summary}."
                else:
                    feedback = (f"Nenhuma ação do lote foi executada "
                                f"({summary}).")

            # "done": a própria ação conclui a tarefa (ex.: 1 clique pedido)
            # -> encerra sem gastar outra chamada só para confirmar
            falhou = feedback.startswith(("Erro", "Nenhuma", "Não abri")) \
                or "erro em " in feedback
            if not falhou and any(_truthy(a.get("done")) for a in actions_list):
                obs = next((str(a.get("observation")) for a in actions_list
                            if a.get("observation")), "")
                self.log(f"✔ {obs}" if obs else "✔ Feito.")
                return "Tarefa concluída."

            # nova captura: o modelo sempre vê a tela atualizada
            try:
                new_shot = self.screen.capture_b64()
            except Exception as exc:
                new_shot = None
                feedback += f" (falha ao capturar a nova tela: {exc})"
            self._emit_screenshot(new_shot)

            blocks: list[dict] = [{"type": "text", "text": feedback}]
            if new_shot:
                blocks.append(self._image_block(new_shot))
            self._append_user(blocks)

        self.log("[limite] Número máximo de passos atingido.")
        return (f"Interrompida: limite de {self.cfg.max_steps} passos "
                "atingido (aumente CUA_MAX_STEPS se necessário).")
