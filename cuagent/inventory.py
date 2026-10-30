"""Inventário do computador: sistema operacional + programas instalados.

Escaneia Menus Iniciar (.lnk), Registro de desinstalação, PATH e apps UWP,
monta um índice nome -> alvo de lançamento (com cache em inventory.json ao
lado do .env) e expõe:

* :func:`scan`      — varre (ou lê o cache) e devolve o dicionário de dados;
* :func:`resolve`   — acha o programa por nome exato/parecido (busca difusa);
* :func:`context_block` — texto curto para o system prompt do agente.

Nada é enviado para a API daqui: o bloco de contexto é montado pelo agent.
"""
from __future__ import annotations

import difflib
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path

_CACHE: dict | None = None
_TTL = 24 * 3600          # revalida o inventário uma vez por dia

# prioridade de fonte ao haver nomes repetidos (menor = melhor)
_RANK = {"menu": 0, "uwp": 1, "path": 2, "registro": 3}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _cache_path() -> Path:
    from .config import env_path
    return env_path().parent / "inventory.json"


# ----------------------------------------------------------------- coleta


def _so_info() -> str:
    """Lê a edição/versão do Windows direto do Registro."""
    version = platform.platform()
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion",
        )
        vals = {}
        for name in ("ProductName", "DisplayVersion", "CurrentBuild"):
            try:
                vals[name] = str(winreg.QueryValueEx(key, name)[0])
            except OSError:
                pass
        winreg.CloseKey(key)
        if vals:
            product = vals.get("ProductName", "Windows")
            disp = vals.get("DisplayVersion", "")
            build = vals.get("CurrentBuild", "")
            version = f"{product} {disp} (build {build})".strip()
        else:
            version = platform.platform()   # Registro inacessível: fallback
    except Exception:
        pass
    arch = "64" if platform.machine().lower().endswith("64") else "32"
    return f"{version} {arch}-bit"


def _tela_info() -> str:
    try:
        import ctypes
        u = ctypes.windll.user32
        return f"{u.GetSystemMetrics(0)}x{u.GetSystemMetrics(1)}"
    except Exception:
        return "?"


def _start_menu() -> list[dict]:
    out: list[dict] = []
    for base in (os.environ.get("PROGRAMDATA", ""), os.environ.get("APPDATA", "")):
        root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        if not root.is_dir():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                out.append({"name": lnk.stem, "target": str(lnk), "src": "menu"})
        except OSError:
            pass
    return out


def _registro() -> list[dict]:
    out: list[dict] = []
    try:
        import winreg
    except ImportError:
        return out
    hives = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in hives:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            try:
                sk = winreg.OpenKey(root, sub)

                def gv(n: str) -> str:
                    try:
                        return str(winreg.QueryValueEx(sk, n)[0])
                    except OSError:
                        return ""

                name = gv("DisplayName").strip()
                if not name:
                    continue
                target = ""
                icon = gv("DisplayIcon").split(",")[0].strip().strip('"')
                if icon.lower().endswith(".exe") and Path(icon).exists():
                    target = icon
                if not target:
                    loc = gv("InstallLocation").strip().strip('"')
                    if loc and Path(loc).is_dir():
                        try:
                            exes = sorted(Path(loc).glob("*.exe"))
                        except OSError:
                            exes = []
                        if exes:
                            hint = re.sub(r"[^a-z0-9]", "", name)[:6].lower()
                            pick = [e for e in exes if hint and hint in e.stem.lower()]
                            target = str(pick[0] if pick else exes[0])
                out.append({"name": name, "target": target, "src": "registro"})
            except OSError:
                continue
        winreg.CloseKey(root)
    return out


def _path_exes() -> list[dict]:
    out, seen = [], set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if not folder:
            continue
        p = Path(folder)
        if not p.is_dir():
            continue
        try:
            for exe in p.glob("*.exe"):
                low = exe.name.lower()
                if low in seen:
                    continue
                seen.add(low)
                out.append({"name": exe.stem, "target": str(exe), "src": "path"})
        except OSError:
            pass
    return out


def _uwp() -> list[dict]:
    """Apps da Store via pasta virtual AppsFolder (PowerShell silencioso)."""
    ps = ("(New-Object -ComObject Shell.Application)"
          ".Namespace('shell:AppsFolder').Items() |"
          "ForEach-Object { $_.Name + '|' + $_.Path }")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=15,
            creationflags=_NO_WINDOW,
        )
    except Exception:
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        if "|" not in line:
            continue
        name, _, target = line.partition("|")
        name, target = name.strip(), target.strip()
        if not name:
            continue
        # só aceita o Path como alvo se for um caminho de lançamento real
        if target and not target.lower().startswith("shell:appsfolder") \
                and ".exe" not in target.lower():
            target = ""
        out.append({"name": name, "target": target, "src": "uwp"})
    return out


# ------------------------------------------------------------------ público


def scan(force: bool = False) -> dict:
    """Varre o sistema (ou lê o cache) e devolve o inventário."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    if not force:
        try:
            data = json.loads(_cache_path().read_text(encoding="utf-8"))
            if time.time() - float(data.get("hora", 0)) < _TTL:
                _CACHE = data
                return data
        except Exception:
            pass

    merged: dict[str, dict] = {}
    for item in _start_menu() + _uwp() + _path_exes() + _registro():
        name = item["name"].strip()
        if not name or len(name) > 90:
            continue
        key = name.casefold()
        cur = merged.get(key)
        if cur is None:
            merged[key] = item
            continue
        # melhor fonte vence; empate: fica com quem tem alvo de lançamento
        better_src = _RANK.get(item["src"], 9) < _RANK.get(cur["src"], 9)
        if better_src and (item["target"] or not cur["target"]):
            merged[key] = item
        elif item["target"] and not cur["target"] and item["src"] == cur["src"]:
            merged[key] = item

    programas = sorted(merged.values(), key=lambda p: p["name"].casefold())
    data = {
        "hora": time.time(),
        "so": _so_info(),
        "tela": _tela_info(),
        "programas": programas,
    }
    _CACHE = data
    try:
        _cache_path().write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        pass
    return data


def resolve(name: str) -> dict:
    """Acha ``name`` no inventário (exato -> contém -> difuso).

    Devolve ``{"name", "target"}``; levanta :class:`ValueError` com sugestões
    quando não acha nada (o modelo se corrige com o erro no feedback).
    """
    progs = scan()["programas"]
    query = (name or "").strip()
    q = query.casefold()
    if not q:
        raise ValueError("open_app sem nome de programa")

    def matched(pool):
        exact = [p for p in pool if p["name"].casefold() == q]
        if exact:
            return exact
        contains = [p for p in pool
                    if q in p["name"].casefold() or p["name"].casefold() in q]
        # mais curto primeiro: "Chrome" deve achar "Google Chrome"
        return sorted(contains, key=lambda p: len(p["name"]))

    with_target = [p for p in progs if p["target"]]
    pool = matched(with_target) or matched(progs)
    if pool:
        return pool[0]

    close = difflib.get_close_matches(
        q, [p["name"].casefold() for p in progs], n=3, cutoff=0.5
    )
    hint = ", ".join(close) if close else "nenhuma sugestão"
    raise ValueError(
        f"Programa {query!r} não encontrado no inventário (parecidos: {hint})"
    )


def context_block(max_chars: int = 4000) -> str:
    """Bloco de texto curto com SO + nomes dos programas (para o prompt)."""
    try:
        data = scan()
    except Exception as exc:
        return f"- Inventário indisponível ({exc})"
    nomes = [p["name"] for p in data["programas"]]
    lista = ", ".join(nomes)
    if len(lista) > max_chars:
        lista = lista[:max_chars] + "…"
    return (
        f"- Sistema: {data['so']} | tela {data['tela']} px | "
        f"{len(nomes)} programas identificados\n"
        f"- Programas do PC (abra com open_app; busca por nome parecido funciona): "
        f"{lista}"
    )
