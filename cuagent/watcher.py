"""Assistente proativo: lê a tela periodicamente e antecipa a resposta.

Enquanto o `ComputerAgent` (agent.py) *age* — clicando e digitando quando
você manda uma tarefa — este módulo é somente leitura: a cada N segundos
captura a tela, pergunta ao modelo o que está acontecendo (contexto do
cliente, resposta pronta, próximo passo) e devolve o resultado para a GUI
exibir no painel "💡 Assistente". Nunca mexe em mouse ou teclado.
"""

from __future__ import annotations

import base64
import io
import os
import re
import threading
import time
from typing import Any, Callable

from openai import OpenAI
from PIL import Image

from .config import Config
from .screen import Screen

from . import agent as _agent    # flag compartilhada de reasoning_effort

_CLIENTS: dict = {}
MAX_TENTATIVAS = 10   # falhas de fila antes de desligar (chave/modelo: na hora)


def _client_for(key: str, base_url: str) -> OpenAI:
    """Cliente reutilizado entre leituras — TLS quente + teto de 120 s."""
    ck = (key, base_url)
    if ck not in _CLIENTS:
        _CLIENTS[ck] = OpenAI(api_key=key, base_url=base_url,
                              timeout=60.0, max_retries=0)
    return _CLIENTS[ck]


def _reset_clients() -> None:
    """Esquece os clientes (usado em testes)."""
    _CLIENTS.clear()


def _create(client, **kw):
    """create() com esforço mínimo de raciocínio — bench: ~2x mais rápido.

    Se a API recusar o parâmetro, corta nas próximas (flag compartilhada
    com o agente) e segue sem ele.
    """
    if kw.get("model") not in _agent._NO_REASONING:
        try:
            return client.chat.completions.create(
                reasoning_effort="minimal", **kw)
        except Exception as exc:
            m = str(exc).lower()
            if ("reasoning_effort" in m or "unknown name" in m
                    or "unknown parameter" in m or "thinking level" in m):
                _agent._NO_REASONING.add(kw.get("model"))
            else:
                raise
    return client.chat.completions.create(**kw)

# (tipo, carga) — a GUI enfileira; quem toca no Tk é o _poll dela.
EmitFn = Callable[[str, Any], None]

WATCH_SYSTEM = """\
Você é o assistente proativo de um atendente (helpdesk). Você enxerga a tela \
do computador do atendente enquanto ele trabalha, normalmente conversando \
com clientes (WhatsApp, e-mail, chat de suporte, tickets, redes sociais).

Sempre que houver uma conversa ou atendimento em andamento, responda no \
formato obrigatório abaixo (em português do Brasil; cada campo pode ocupar \
várias linhas):

CONTEXTO: quem é o cliente, o que ele quer, o que já foi respondido e o que \
ficou em aberto.
RESPOSTA: a resposta que o atendente poderia enviar ao cliente AGORA, pronta \
para copiar, tom profissional e cordial, curta. Se faltar informação na tela, \
redija a resposta perguntando o que é preciso — nunca invente dados.
PRÓXIMO PASSO: qual programa/aba/página abrir e qual ação concreta tomar \
(ex.: "responder no WhatsApp Web, conversa com Maria"; "abrir o ticket #123 \
no sistema X").

Se a tela não tiver conversa, cliente ou tarefa de atendimento relevante, \
responda somente:

NADA

Baseie-se também na memória da leitura anterior, quando houver, para não \
perder o fio da conversa; se ela não corresponder mais à tela atual, \
ignore-a. Nunca invente informações que não estejam na tela.
"""

_MARKERS = ("CONTEXTO", "RESPOSTA", "PRÓXIMO PASSO")


class ConfigError(Exception):
    """Configuração local ruim (ex.: chave ausente) — repetir não resolve."""


def _hora() -> str:
    return time.strftime("%H:%M:%S")


def parse_reply(text: str) -> dict | None:
    """Extrai os campos do formato CONTEXTO/RESPOSTA/PRÓXIMO PASSO.

    Devolve None quando o modelo respondeu que não há nada relevante (NADA).
    """
    raw = (text or "").strip()
    if not raw:
        return None

    first = next((ln for ln in raw.splitlines() if ln.strip()), "")
    if (first.upper().rstrip("!").startswith("NADA")
            and not any(m in raw.upper() for m in _MARKERS)):
        return None

    hits = list(re.finditer(
        r"(?im)^\s*(CONTEXTO|RESPOSTA|PRÓXIMO\s+PASSO)\s*:\s*", raw))
    if not hits:  # formato fora do padrão: o texto inteiro vira o contexto
        return {"contexto": raw, "resposta": "", "proximo": ""}

    fields: dict[str, str] = dict.fromkeys(_MARKERS, "")
    for i, hit in enumerate(hits):
        name = re.sub(r"\s+", " ", hit.group(1).upper())
        end = hits[i + 1].start() if i + 1 < len(hits) else len(raw)
        if name in fields:
            fields[name] = raw[hit.end():end].strip()
    return {
        "contexto": fields["CONTEXTO"],
        "resposta": fields["RESPOSTA"],
        "proximo": fields["PRÓXIMO PASSO"],
    }


def _friendly(exc: Exception, status: Any = None) -> str:
    if status == 401:
        return "chave inválida (401) — confira em ⚙ Configurações."
    if status == 429:
        return "limite de requisições atingido (429) — tento de novo em seguida."
    if status in (500, 502, 503, 504) or "unavailable" in str(exc).lower():
        return (f"fila da API do Google ({status or 'indisponível'}) — "
                "é transitório, continuo tentando.")
    if status == 529 or "overloaded" in str(exc).lower():
        return "API sobrecarregada (529) — tento de novo em seguida."
    text = str(exc).strip() or type(exc).__name__
    return (text[:160] + "…") if len(text) > 160 else text


# ------------------------------------------------- leitura econômica da tela

_SIG_GRID = (80, 50)      # grade da assinatura visual (4.000 células)
_SIG_CELL_D = 14          # variação de cinza mínima para a célula "mudar"
_SIG_MIN_CELLS = 6        # células mudadas mínimas para consultar a IA
_SHRINK_WIDTH = 1280      # leitura manda imagem reduzida (menos tokens)


def _decode(shot_b64: str):
    """Abre o JPEG da captura uma única vez (ou None se inválido)."""
    try:
        return Image.open(io.BytesIO(base64.b64decode(shot_b64)))
    except Exception:
        return None


def _sig_of(img) -> bytes | None:
    """Assinatura leve (grade 80x50 em cinza) de uma imagem já aberta."""
    try:
        if img is None:
            return None
        return img.convert("L").resize(_SIG_GRID, Image.BOX).tobytes()
    except Exception:
        return None


def _signature(shot_b64: str) -> bytes | None:
    """Assinatura da tela direto do JPEG (compat/testes)."""
    return _sig_of(_decode(shot_b64))


def _changed(old: bytes | None, new: bytes | None) -> bool:
    """True quando a tela mudou o bastante para valer nova consulta à IA."""
    if old is None or new is None or len(old) != len(new):
        return True
    mudou = sum(1 for a, b in zip(old, new) if abs(a - b) > _SIG_CELL_D)
    return mudou >= _SIG_MIN_CELLS


def _shrink_of(img, original: str) -> str:
    """Reenquadra em 1280 px — menos tokens e mais rápido na fila."""
    try:
        if img is None or img.width <= _SHRINK_WIDTH:
            return original
        hgt = max(1, round(img.height * _SHRINK_WIDTH / img.width))
        small = img.convert("RGB").resize((_SHRINK_WIDTH, hgt))
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return original


def _shrink(shot_b64: str) -> str:
    """Reenquadra o JPEG direto (compat/testes)."""
    return _shrink_of(_decode(shot_b64), shot_b64)


class ScreenWatcher:
    """Loop de leitura periódica da tela (somente sugestão, sem ações)."""

    def __init__(self, screen: Screen, emit: EmitFn) -> None:
        self.screen = screen
        self._emit_fn = emit
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._interval = 15.0
        self._memory = ""      # resumo da última leitura (memória entre ciclos)
        self._errors = 0
        self._backoff = 0           # falhas consecutivas (espera crescente)
        self._last_sig = None       # assinatura visual da última leitura boa
        self._last_payload = None   # última análise aceita (cache do painel)
        self._last_hora = ""        # hora dessa análise

    # ------------------------------------------------------------- controle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def start(self, interval: float | None = None) -> None:
        if interval:
            self.set_interval(interval)
        if self.running:            # já rodando: só garante que está ativo
            self._paused.clear()
            return
        self._stop.clear()
        self._paused.clear()
        self._errors = 0
        self._backoff = 0
        self._last_sig = None       # 1ª leitura depois de ligar consulta a IA
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="screen-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._paused.clear()        # destrava para o loop conseguir sair

    def set_interval(self, seconds: float) -> None:
        self._interval = max(3.0, float(seconds))

    def set_paused(self, paused: bool) -> None:
        (self._paused.set() if paused else self._paused.clear())

    def _emit(self, kind: str, data: Any) -> None:
        try:
            self._emit_fn(kind, data)
        except Exception:
            pass

    # ------------------------------------------------------------ chamada API

    def _ask(self, shot: str) -> str:
        key = (os.getenv("CUA_API_KEY", "").strip()
               or os.getenv("GEMINI_API_KEY", "").strip())
        if not key or key.startswith("COLE_"):
            raise ConfigError("chave da API não configurada — abra ⚙ Configurações.")
        base_url = (os.getenv("CUA_BASE_URL")
                    or Config.base_url).strip().rstrip("/")
        model = (os.getenv("CUA_WATCH_MODEL")
                 or os.getenv("CUA_MODEL") or Config.model)

        width, height = self.screen.model_size()
        lines = [f"Tela atual ({width}×{height} px) do atendente:"]
        if self._memory:
            lines.append(
                "\nMemória da leitura anterior (pode estar desatualizada):\n"
                + self._memory)
        text = "\n".join(lines)
        mensagens = [
            {"role": "system", "content": WATCH_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{shot}"}},
            ]},
        ]

        def _perguntar(cliente, alvo: str) -> str:
            resp = _create(cliente, model=alvo, max_tokens=1024,
                           messages=mensagens)
            choice = resp.choices[0] if resp.choices else None
            return ((getattr(choice.message, "content", "") or "")
                    if choice else "")

        # time de APIs: Groq primeiro (se houver chave); senão Gemini —
        # se o Groq falhar, o Gemini responde na hora (plano B)
        groq_key = (os.getenv("CUA_GROQ_API_KEY", "").strip()
                    or os.getenv("GROQ_API_KEY", "").strip())
        if groq_key and not groq_key.startswith("COLE_"):
            try:
                return _perguntar(
                    _client_for(groq_key, Config.groq_base),
                    os.getenv("CUA_GROQ_VISION_MODEL")
                    or Config.groq_vision_model)
            except Exception:
                pass  # plano B abaixo
        return _perguntar(_client_for(key, base_url), model)

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._paused.is_set():       # tarefa do agente em execução
                self._stop.wait(0.4)
                continue

            t0 = time.perf_counter()
            anchor = t0              # onde a espera deste ciclo começa
            wait = self._interval    # espera do ciclo (intervalo ou backoff)
            try:
                shot = self.screen.capture_b64()
                img = _decode(shot)      # decodifica UMA vez (sig + encolhe)
                sig = _sig_of(img)
                if (self._last_sig is not None
                        and not _changed(self._last_sig, sig)):
                    # tela igual à última análise: NÃO consulta a IA —
                    # o painel só atualiza (zero tokens, zero servidor)
                    self._emit("cache", {
                        "hora": _hora(),
                        "hora_analise": self._last_hora,
                        "dur": round(time.perf_counter() - t0),
                        "wait": wait,
                    })
                    self._errors = 0
                    self._backoff = 0
                else:
                    self._emit("thinking", None)
                    small = _shrink_of(img, shot)
                    reply = self._ask(small)
                    if not reply.strip():
                        # resposta vazia = falha transitória: 1 retry na hora
                        reply = self._ask(_shrink(self.screen.capture_b64()))
                    payload = parse_reply(reply)
                    dur = round(time.perf_counter() - t0)
                    self._errors = 0
                    self._backoff = 0
                    self._last_sig = sig   # congela a tela SÓ depois da IA ok
                    if payload is None:
                        self._last_payload = None
                        self._last_hora = _hora()
                        self._emit("nada", {"hora": self._last_hora,
                                            "dur": dur, "wait": wait})
                    else:
                        payload["hora"] = _hora()
                        payload["dur"] = dur
                        payload["wait"] = wait
                        self._memory = _remember(payload)
                        self._last_payload = payload
                        self._last_hora = payload["hora"]
                        self._emit("result", payload)
            except ConfigError as exc:
                self._emit("error", {"hora": _hora(), "msg": str(exc),
                                     "fatal": True})
                break
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                dur = round(time.perf_counter() - t0)
                # chave/modelo errado não resolve esperando: desliga na
                # hora; fila/lentidão (429/5xx/timeout) é transitória e
                # espera crescente (backoff) — não martelamos o servidor
                # durante o pico.
                chave_ruim = status in (400, 401, 403, 404, 422)
                if not chave_ruim:
                    self._errors += 1
                    self._backoff += 1
                    wait = min(self._interval * (2 ** min(self._backoff, 6)),
                               60.0)
                    anchor = time.perf_counter()  # espera a partir do erro
                fatal = chave_ruim or self._errors >= MAX_TENTATIVAS
                msg = _friendly(exc, status)
                if fatal and not chave_ruim:
                    msg += f" — desligo após {MAX_TENTATIVAS} falhas seguidas."
                data_err = {
                    "hora": _hora(),
                    "msg": msg,
                    "fatal": fatal,
                    "dur": dur,
                    "wait": wait,
                }
                if self._last_hora and not fatal:
                    data_err["mantendo"] = self._last_hora  # última análise
                self._emit("error", data_err)
                if fatal:
                    break

            # espera a partir do anchor (início do ciclo ou do erro) em
            # fatias de 0,4 s — reage a parada e a pausa de tarefa
            deadline = anchor + wait
            while not self._stop.is_set() and not self._paused.is_set():
                left = deadline - time.perf_counter()
                if left <= 0:
                    break
                time.sleep(min(0.4, left))


def _remember(payload: dict) -> str:
    """Memória enxuta entre leituras (contexto + próximo passo, truncado)."""
    text = " ".join(
        p for p in (payload.get("contexto", ""),
                    "Próximo passo: " + payload.get("proximo", ""))
        if p and p != "—").strip()
    return text[-1200:]
