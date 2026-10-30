"""IA das rotinas: nomeia/descreve gravações e monta a Rotina do Dia.

Recebe um *digesto* de cada gravação (contagens de eventos, janelas/site
acessados na ordem e a sequência de teclas digitadas) e devolve JSON:
nome + descrição para a rotina, ou a sequência combinada "Rotina do Dia"
com insights sobre a rotina do usuário. Texto puro: vai para o Groq
(Llama 3.3 70B) quando há chave CUA_GROQ_API_KEY, senão para o Gemini —
que também é o plano B se o Groq falhar.
"""

from __future__ import annotations

import json
import re

from openai import OpenAI

from .config import Config

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)

_DESCRIBE_SYS = """\
Você analisa uma ROTINA gravada no computador de um usuário (movimentos,
cliques, teclas e janelas que ele acessou) e responde SOMENTE com um objeto JSON:
{"nome": "...", "descricao": "..."}
Regras: "nome" com no máximo 40 caracteres, em português, no estilo
"Pesquisar notícias no Chrome"; "descricao" com no máximo 120 caracteres
explicando em 1 frase o que a rotina faz. Nada fora do JSON."""

_ANALISE_SYS = """\
Você recebe rotinas que um usuário gravou no computador dele e deve montar a
"ROTINA DO DIA": a sequência de rotinas que ele repete no dia a dia.
Responda SOMENTE com um objeto JSON:
{"nome": "...", "passos": [{"id": "<id exato recebido>", "vezes": 1}],
 "insights": ["..."]}
Regras: use APENAS os ids recebidos, na ordem lógica do dia; "vezes" é de
1 a 10; "nome" com no máximo 40 caracteres; 1 a 3 "insights" em português
(como o usuário usa o PC), cada um com no máximo 120 caracteres. Nada fora
do JSON."""


# ------------------------------------------------------------------ parsing


def extract_json(text: str) -> dict | None:
    """Pega o primeiro objeto JSON válido (aceita cerca ```json ... ```)."""
    raw = (text or "").strip()
    if not raw:
        return None
    candidates: list[str] = []
    fence = _FENCE.search(raw)
    if fence:
        candidates.append(fence.group(1))
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def parse_describe(text: str) -> dict | None:
    """JSON da IA -> ``{"nome", "descricao"}`` (None se não veio JSON)."""
    obj = extract_json(text)
    if not obj:
        return None
    nome = str(obj.get("nome") or obj.get("name") or "").strip()
    if not nome:
        return None
    desc = str(obj.get("descricao") or obj.get("description") or "").strip()
    return {"nome": nome[:80], "descricao": desc[:240]}


def parse_analyze(text: str, known_ids: set) -> dict | None:
    """JSON da IA -> rotina do dia válida (só ids conhecidos; None se vazia)."""
    obj = extract_json(text)
    if not obj:
        return None
    passos: list[dict] = []
    seen: set = set()
    raw_passos = obj.get("passos") or obj.get("steps") or []
    for step in raw_passos if isinstance(raw_passos, list) else []:
        if not isinstance(step, dict):
            continue
        rid = str(step.get("id") or "")
        if rid not in known_ids or rid in seen:
            continue
        seen.add(rid)
        try:
            vezes = int(step.get("vezes", 1) or 1)
        except (TypeError, ValueError):
            vezes = 1
        passos.append({"id": rid, "vezes": max(1, min(10, vezes))})
    if not passos:
        return None
    insights_raw = obj.get("insights") or []
    insights = [str(s).strip()[:240]
                for s in insights_raw if str(s).strip()][:5] \
        if isinstance(insights_raw, list) else []
    nome = str(obj.get("nome") or obj.get("name") or "").strip()
    return {
        "nome": (nome or "Rotina do Dia")[:80],
        "passos": passos,
        "insights": insights,
    }


# ------------------------------------------------------------------ digesto


def digest(routine: dict) -> str:
    """Resumo textual da gravação — o que a IA recebe (economia de tokens)."""
    events = routine.get("events") or []
    moves = clicks = wheels = keys = 0
    downs: list[str] = []
    windows: list[str] = []
    last_win = ""
    for ev in events:
        kind = ev.get("type")
        if kind == "window":
            label = (f"{ev.get('proc') or '?'} "
                     f"({ev.get('title') or 'sem título'})")
            if label != last_win:
                windows.append(label)
                last_win = label
        elif kind == "key":
            keys += 1
            if ev.get("state") == "down":
                key = str(ev.get("key") or "")
                if key and len(downs) < 40:
                    downs.append(key)
        elif kind == "down":
            clicks += 1
        elif kind == "move":
            moves += 1
        elif kind == "wheel":
            wheels += 1

    secs = (routine.get("duration_ms") or 0) / 1000.0
    screen = routine.get("screen") or [0, 0]
    palavra = "".join(k for k in downs if len(k) == 1)[:120]

    lines = [
        f"Nome atual: {routine.get('name', '')} · duração {secs:.1f} s · "
        f"tela {screen[0]}x{screen[1]}",
        f"Eventos: {moves} movimentos, {clicks} cliques, {wheels} rolagens, "
        f"{keys} teclas, {len(windows)} trocas de janela.",
        "Janelas acessadas (ordem): "
        + (" → ".join(windows[:12]) if windows else "—"),
        "Teclas na ordem: " + (", ".join(downs[:40]) if downs else "—"),
    ]
    if palavra:
        lines.append(f"Texto digitado (letras/dígitos na ordem): {palavra}")
    return "\n".join(lines)


# ------------------------------------------------------------------ chamadas


def _call(cfg: Config, system: str, user: str, max_tokens: int) -> str:
    """Texto puro: Groq primeiro (se houver chave), Gemini como plano B."""
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    rota = cfg.text_route() if hasattr(cfg, "text_route") else [
        (cfg.api_key, cfg.base_url, cfg.model)]
    erro: Exception | None = None
    for chave, base, modelo, *_ in rota[:2]:
        try:
            client = OpenAI(api_key=chave, base_url=base,
                            timeout=60.0, max_retries=0)
            response = client.chat.completions.create(
                model=modelo,
                max_tokens=max_tokens,
                messages=msgs,
            )
        except Exception as exc:
            erro = exc          # tenta o próximo provedor da rota
            continue
        choice = response.choices[0] if response.choices else None
        return (getattr(choice.message, "content", "") or "") if choice else ""
    raise RuntimeError(f"Falha na chamada da API: {erro}") from erro


def describe_routine(cfg: Config, routine: dict) -> dict:
    """Nome + descrição da rotina (IA). Levanta ValueError se não vier JSON."""
    text = _call(cfg, _DESCRIBE_SYS, digest(routine), max_tokens=512)
    parsed = parse_describe(text)
    if not parsed:
        raise ValueError("a IA não devolveu nome/descrição válidos")
    return parsed


def analyze_routines(cfg: Config, routines: list[dict]) -> dict:
    """Monta a Rotina do Dia a partir de várias gravações (IA)."""
    if not routines:
        raise ValueError("grave ao menos uma rotina antes de analisar")
    blocos = []
    for routine in routines:
        blocos.append(f'id={routine.get("id")}\n{digest(routine)}')
    user = "ROTINAS GRAVADAS:\n\n" + "\n\n".join(blocos)
    text = _call(cfg, _ANALISE_SYS, user, max_tokens=1024)
    parsed = parse_analyze(text, {r.get("id") for r in routines})
    if not parsed:
        raise ValueError("a IA não devolveu uma sequência válida de passos")
    return parsed
