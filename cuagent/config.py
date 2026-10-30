"""Configuração do agente (Helpdesk-OpenCode — API gratuita compatível com OpenAI).

Todos os valores podem ser sobrescritos por variáveis de ambiente
(ver arquivo .env.example) ou pelos argumentos de linha de comando.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# --- time de APIs: Groq principal (mais rápido), Gemini como plano B ---
GROQ_BASE = "https://api.groq.com/openai/v1"
GROQ_VISION_MODEL = "qwen/qwen3.8-27b"  # lê screenshots
GROQ_TEXT_MODEL = "openai/gpt-oss-120b"                       # rotinas/nomes


def env_path() -> Path:
    """Caminho do arquivo .env.

    Quando empacotado com PyInstaller, procura ao lado do .exe; na fonte,
    na raiz do projeto.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".env"
    return Path(__file__).resolve().parent.parent / ".env"


def load_env() -> None:
    """Carrega o .env para o ambiente (opcional: só preenche o que faltar)."""
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path())
    except Exception:  # pragma: no cover
        pass


def write_env(updates: dict) -> Path:
    """Grava/atualiza chaves no .env preservando as demais linhas existentes."""
    path = env_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)

    for key, value in remaining.items():
        out.append(f"{key}={value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


load_env()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_SYSTEM_PROMPT = """\
Você controla o computador do usuário. A cada passo recebe a TELA ATUAL e responde \
SÓ com JSON (nada fora dele):
{"action":"left_click","coordinate":[x,y],"observation":"botão Salvar"}
Lote previsível (até 3): {"actions":[{"action":"left_click","coordinate":[x,y]},\
{"action":"type","text":"olá"}],"observation":"..."}

Ações: left_click, right_click, double_click, triple_click, mouse_move, \
left_click_drag (coordinate=destino, start_coordinate=origem), \
scroll (coordinate, amount: negativo=baixo, scroll_direction), \
type (text), key (text ex.: "ctrl+s", "enter"), hold_key, wait (duration s), \
open_app (name do programa; o PC acha por nome aproximado).
Coordenadas NORMALIZADAS 0-1000 nos dois eixos: [0,0]=canto sup. esquerdo, \
[1000,1000]=inf. direito. Mire no CENTRO do elemento.
"observation": até 8 palavras sobre o que você viu.
Se a ação (ou lote) JÁ CONCLUI a tarefa de forma previsível (ex.: o usuário \
pediu só um clique), acrescente "done":true — o agente executa e encerra sem \
nova chamada. Não use done quando precisar conferir o resultado na tela.

Regras:
- Aja só sobre o que VÊ na captura; use o mínimo de passos e atalhos.
- Clique no campo antes de digitar. Após open_app, se a janela não apareceu, \
use wait 1.5 (nunca repita open_app).
- Nada destrutivo sem pedido explícito. Falhou 2x? Pare e explique.
- Digite o texto exatamente como pedido.
- Execute TODOS os comandos da tarefa no app citado. Conclua só ao VER o \
resultado: então responda texto puro curto (sem JSON), o que encerra.
"""


@dataclass(frozen=True)
class Config:
    api_key: str
    base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    model: str = "gemini-3.5-flash-lite"
    max_tokens: int = 2048
    max_steps: int = 30
    auto_approve: bool = True
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_screenshot_width: int = 1280   # precisão igual à nativa, 55% menos tokens
    # --- Groq opcional (chave em CUA_GROQ_API_KEY) — vira o principal ---
    groq_key: str = ""
    groq_base: str = GROQ_BASE
    groq_vision_model: str = GROQ_VISION_MODEL
    groq_text_model: str = GROQ_TEXT_MODEL

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        """Monta a configuração a partir do ambiente + sobrescritas pontuais."""
        api_key = (
            os.getenv("CUA_API_KEY", "").strip()
            or os.getenv("GEMINI_API_KEY", "").strip()
        )
        if not api_key or api_key.startswith("COLE_"):
            raise SystemExit(
                "Erro: defina CUA_API_KEY no ambiente ou no arquivo .env "
                "(chave gratuita em https://aistudio.google.com/apikey — veja .env.example)."
            )

        groq_key = (os.getenv("CUA_GROQ_API_KEY", "").strip()
                    or os.getenv("GROQ_API_KEY", "").strip())
        if groq_key.startswith("COLE_"):
            groq_key = ""

        values: dict = {
            "api_key": api_key,
            "base_url": os.getenv("CUA_BASE_URL", cls.base_url).strip().rstrip("/"),
            "model": os.getenv("CUA_MODEL", cls.model),
            "groq_key": groq_key,
            "groq_base": os.getenv(
                "CUA_GROQ_BASE_URL", cls.groq_base).strip().rstrip("/"),
            "groq_vision_model": os.getenv(
                "CUA_GROQ_VISION_MODEL", cls.groq_vision_model),
            "groq_text_model": os.getenv(
                "CUA_GROQ_TEXT_MODEL", cls.groq_text_model),
            "max_tokens": int(os.getenv("CUA_MAX_TOKENS", str(cls.max_tokens))),
            "max_steps": int(os.getenv("CUA_MAX_STEPS", str(cls.max_steps))),
            "auto_approve": _bool("CUA_AUTO_APPROVE", cls.auto_approve),
            "system_prompt": os.getenv("CUA_SYSTEM_PROMPT", cls.system_prompt),
            "max_screenshot_width": int(
                os.getenv("CUA_MAX_SCREENSHOT_WIDTH", str(cls.max_screenshot_width))
            ),
        }

        # sobrescreve apenas campos válidos e que não sejam None (vindos do argparse)
        for key, value in overrides.items():
            if value is not None and key in values:
                values[key] = value

        return cls(**values)

    # ------------------------------------------------------------ provedores
    def vision_route(self) -> list[tuple[str, str, str, bool]]:
        """Rota da visão: (chave, base, modelo, esforço) — Gemini → Groq."""
        # Gemini primeiro: no teste de cliques acertou 4/4 contra 1/2 do Qwen
        # (Groq), que ainda estoura o limite grátis em poucas chamadas
        rota: list[tuple[str, str, str, bool]] = [
            (self.api_key, self.base_url, self.model, True)]
        if self.groq_key:
            rota.append((self.groq_key, self.groq_base,
                         self.groq_vision_model, False))
        return rota

    def text_route(self) -> list[tuple[str, str, str, bool]]:
        """Rota do texto (rotinas): Groq → Gemini."""
        rota: list[tuple[str, str, str, bool]] = []
        if self.groq_key:
            rota.append((self.groq_key, self.groq_base,
                         self.groq_text_model, False))
        rota.append((self.api_key, self.base_url, self.model, True))
        return rota

    @property
    def route_active(self) -> str:
        """Linha curta do que está ativo (UI de configurações)."""
        return ("Gemini → Groq (plano B)" if self.groq_key else "Gemini")
