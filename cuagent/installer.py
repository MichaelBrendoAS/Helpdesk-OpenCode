"""Instalação automática do que o app precisa (hoje: o OpenCode).

Python, bibliotecas e o servidor de ferramentas já vão dentro do .exe; o
único requisito externo é o OpenCode. Se não houver um no PATH nem na pasta
do app, baixa o binário OFICIAL do GitHub (releases do projeto opencode) para
``tools\\opencode\\`` ao lado do .exe — sem precisar de Node/npm nem de
permissão de administrador.
"""

from __future__ import annotations

import io
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

# repositório oficial (sst/opencode passou a redirecionar para este)
_REPOS = ("anomalyco/opencode", "sst/opencode")
# versão TESTADA com o app. Versões diferentes mudam a API do servidor
# (ex.: /mcp respondendo HTML) — por isso o app nunca usa o OpenCode do
# sistema: sempre esta, baixada para a pasta dele.
PINNED_VERSION = "1.18.32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def local_opencode() -> Path:
    return _app_dir() / "tools" / "opencode" / "opencode.exe"


def find() -> str | None:
    """Só o OpenCode da pasta do app (versão fixa). O do sistema é ignorado."""
    loc = local_opencode()
    return str(loc) if loc.is_file() else None


def _version(exe: str) -> str:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=60, creationflags=_NO_WINDOW)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _works(exe: str) -> bool:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=60, creationflags=_NO_WINDOW)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def _assets() -> list[str]:
    """Nomes do pacote Windows em ordem de preferência para esta máquina."""
    if platform.machine().lower() in ("arm64", "aarch64"):
        return ["opencode-windows-arm64.zip"]
    # baseline = CPUs antigas sem AVX2 (plano B se o normal não rodar)
    return ["opencode-windows-x64.zip", "opencode-windows-x64-baseline.zip"]


def _latest_urls() -> dict[str, str]:
    """URLs do pacote da versão fixa (tag v{PINNED_VERSION})."""
    for repo in _REPOS:
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/releases/tags/v{PINNED_VERSION}",
                headers={"Accept": "application/vnd.github+json",
                         "User-Agent": "Helpdesk-OpenCode"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            urls = {a["name"]: a["browser_download_url"]
                    for a in data.get("assets", [])}
            if urls:
                return urls
        except Exception:
            continue
    return {}


def _download(url: str, log: Callable[[str], None]) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Helpdesk-OpenCode"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        buf, got, last = io.BytesIO(), 0, -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            buf.write(chunk)
            got += len(chunk)
            if total:
                pct = got * 100 // total
                if pct // 20 != last:          # avisa a cada 20%
                    last = pct // 20
                    log(f"⬇ baixando OpenCode… {pct}% ({got >> 20}/{total >> 20} MB)")
    return buf.getvalue()


def install(log: Callable[[str], None] = print) -> str:
    """Baixa e instala o OpenCode na pasta do app. Devolve o caminho do .exe."""
    urls = _latest_urls()
    if not urls:
        raise RuntimeError("não consegui consultar as versões do OpenCode no "
                           "GitHub (sem internet?)")
    dest = local_opencode().parent
    ultimo_erro = ""
    for nome in _assets():
        url = urls.get(nome)
        if not url:
            continue
        dados = _download(url, log)
        tmp = dest.with_name("opencode_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(dados)) as zf:
            zf.extractall(tmp)
        achado = next(tmp.rglob("opencode.exe"), None)
        if achado is None:
            ultimo_erro = f"{nome} sem opencode.exe"
            continue
        if not _works(str(achado)):
            ultimo_erro = f"{nome} não executou nesta máquina"
            log(f"⚠ {ultimo_erro} — tentando outra versão")
            continue
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        achado.parent.rename(dest) if achado.parent != tmp else tmp.rename(dest)
        shutil.rmtree(tmp, ignore_errors=True)
        return str(local_opencode())
    raise RuntimeError(f"falha ao instalar o OpenCode: {ultimo_erro or 'pacote não encontrado'}")


def ensure(log: Callable[[str], None] = print) -> str:
    """Garante o OpenCode (usa o existente ou instala). Devolve o caminho."""
    exe = find()
    if exe and _version(exe) == PINNED_VERSION:
        return exe
    if exe:
        log(f"🔧 OpenCode local em versão diferente — trocando pela {PINNED_VERSION}…")
    else:
        log(f"🔧 Instalando o OpenCode {PINNED_VERSION} (1ª vez, ~60 MB)…")
    exe = install(log)
    log("✔ OpenCode instalado.")
    return exe
