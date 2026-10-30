#!/usr/bin/env python3
"""Ponto de entrada do Helpdesk (Computer-Using Agent).

Exemplos:
    python main.py "abra o navegador e pesquise por clima em São Paulo"
    python main.py -y --max-steps 50 "abra o Bloco de notas e escreva 'oi'"
    python main.py                      # modo interativo (várias tarefas em sequência)
"""

from __future__ import annotations

import argparse
import sys

from cuagent import safety
from cuagent.agent import ComputerAgent
from cuagent.config import Config
from cuagent.screen import Screen


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cua",
        description=(
            "Agente que controla o seu computador (mouse, teclado e tela) "
            "usando uma API de IA compatível com OpenAI (padrão: Gemini gratuito)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "exemplos:\n"
            '  python main.py "abra o Chrome e pesquise por clima em São Paulo"\n'
            '  python main.py -y "abra o Explorador de Arquivos na pasta Downloads"\n'
            "  python main.py   # modo interativo"
        ),
    )
    parser.add_argument("task", nargs="*", help="tarefa em linguagem natural")
    parser.add_argument(
        "-y",
        "--auto-approve",
        action="store_true",
        default=None,
        help="executa as ações SEM pedir confirmação (use com cuidado)",
    )
    parser.add_argument("--model", default=None, help="ID do modelo a usar")
    parser.add_argument(
        "--max-steps", type=int, default=None, help="máximo de passos da API"
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    try:
        cfg = Config.from_env(
            model=args.model,
            max_steps=args.max_steps,
            auto_approve=args.auto_approve,
        )
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(str(exc), file=sys.stderr)
            return 1
        return 0

    screen = Screen(max_width=cfg.max_screenshot_width)
    agent = ComputerAgent(cfg, screen)

    native = "x".join(map(str, screen.native_size()))
    model = "x".join(map(str, screen.model_size()))
    print(f"Modelo: {cfg.model}  |  Tela: {native}px (vista do modelo: {model}px)")
    print("Segurança: confirmação de ação "
          + ("DESATIVADA (-y)" if cfg.auto_approve else "ativada")
          + " | failsafe: mova o mouse para o canto superior esquerdo para abortar")

    task = " ".join(args.task).strip()

    try:
        if task:
            agent.run(task)
        else:
            print('\nModo interativo — digite uma tarefa (vazio ou "sair" para encerrar).')
            while True:
                try:
                    line = input("\ntarefa> ").strip()
                except EOFError:
                    break
                if not line or line.lower() in {"sair", "exit", "quit"}:
                    break
                agent.run(line)
    except safety.Abort as exc:
        print(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrompido (Ctrl+C).")
        return 130
    except Exception as exc:
        print(f"\nErro: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
