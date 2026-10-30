#!/usr/bin/env python3
"""Interface gráfica do Helpdesk (CustomTkinter).

Mostra a tela que o agente vê, o log de atividade, diálogo de confirmação
por ação e controles de Executar/Parar. O painel "💡 Assistente" lê a tela
periodicamente e entrega contexto do cliente, resposta pronta e próximo
passo enquanto o usuário trabalha. Configurações (chave API, modelo) são
salvas no .env ao lado do programa.
"""

from __future__ import annotations

import base64
import io
import os
import queue
import re
import sys
import threading
import time
import traceback



def _std_from_handles() -> None:
    """No .exe janela (sem console) sys.stdin/stdout vêm None mesmo quando o
    OpenCode entrega pipes: reabre a partir dos handles herdados do Windows."""
    import ctypes
    import msvcrt
    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = ctypes.c_void_p
    for num, attr, mode in ((-10, "stdin", "r"), (-11, "stdout", "w"),
                            (-12, "stderr", "w")):
        if getattr(sys, attr) is not None:
            continue
        h = k32.GetStdHandle(num)
        if not h or h == ctypes.c_void_p(-1).value:
            continue
        fd = msvcrt.open_osfhandle(h, os.O_BINARY)
        setattr(sys, attr, io.TextIOWrapper(
            os.fdopen(fd, mode + "b", buffering=0 if mode == "w" else -1),
            encoding="utf-8", line_buffering=(mode == "w"),
            write_through=(mode == "w")))


if "--mcp" in sys.argv:
    if sys.platform == "win32":
        _std_from_handles()
    try:
        from cuagent.mcp_server import main as _mcp_main
        _mcp_main()
    except Exception:
        # falha antes do log do MCP existir (ex.: import): registra mesmo assim
        import traceback
        from pathlib import Path
        pasta = Path(sys.executable).resolve().parent / "logs"
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / "mcp.log", "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        sys.exit(1)
    sys.exit(0)

# em apps janela (PyInstaller --windowed) stdout/stderr podem ser None
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = sys.stdout

import cuagent  # noqa: F401,E402 — DPI por monitor antes do Tk/pyautogui
import customtkinter as ctk
from PIL import Image

from cuagent import envio_chat
from cuagent import player as playmod
from cuagent import recorder as recmod
from cuagent import rotinas_ai
from cuagent import safety
from cuagent.opencode_agent import OpenCodeAgent, find_opencode
from cuagent.opencode_agent import INSTALL_STATE, shutdown as oc_shutdown, warm_up
from cuagent.config import Config, env_path, write_env
from cuagent.screen import Screen
from cuagent.watcher import ScreenWatcher

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

PREVIEW_MAX = (470, 330)  # tamanho máximo do preview no painel esquerdo
PREVIEW_IDLE_MS = 1000    # ocioso: 1 quadro/s (30 fps o tempo todo era desperdício)

WATCH_OPTIONS = ["5 s", "10 s", "15 s", "30 s", "60 s"]
WATCH_DEFAULT = "15 s"    # intervalo entre leituras do assistente
WATCH_MAX_BLOCKS = 30     # leituras guardadas no painel do assistente


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Helpdesk-OpenCode")
        self.geometry("1240x820")
        self.minsize(1040, 680)

        self.q: queue.Queue = queue.Queue()   # mensagens do worker -> UI
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.agent: OpenCodeAgent | None = None
        self.screen = Screen(Config.max_screenshot_width)   # o que a IA recebe
        self.watcher = ScreenWatcher(
            self.screen, emit=lambda k, d: self.q.put(("watch", k, d))
        )
        # rastreia o "chat do cliente" SÓ quando você liga o assistente
        # (thread nasce aí; usada pelo 📨 Mandar pro cliente)
        self.cliente: envio_chat.ClienteTracker | None = None
        self._watch_blocks: list[str] = []   # leituras no painel do assistente
        self._watch_payload: dict | None = None  # último resultado (copiar/usar)
        self._photo = None                    # CTkImage reaproveitado no preview
        self._photo_size = None               # tamanho atual do CTkImage
        self._shot_info = ""                  # último texto do rodapé (evita reconfigurar)
        self._refresh_job = None              # id do after do preview (p/ cancelar)
        self._pending = None                  # (event, holder) de um diálogo aberto
        self.recorder = recmod.RoutineRecorder()     # ⏺ gravação de rotinas
        self._rec_job = None                  # id do after do indicador de gravação
        self._play_worker: threading.Thread | None = None  # repetição ativa
        self._play_stop: threading.Event | None = None     # sinal do ■ Parar
        self._rot_times: dict = {}            # id da rotina -> seletor "vezes"
        self._analyzing = False               # IA analisando as rotinas agora

        self._build()
        self.after(150, self._poll)
        self.after(80, self._place_on_screen)
        self.after(600, self._refresh_preview)
        self.after(PREVIEW_IDLE_MS, self._auto_refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._kick_inventory)       # varre PC (programas + SO)
        # OpenCode sempre ligado: a 1ª tarefa já não paga o boot (~20 s)
        self.after(800, lambda: warm_up(self.screen.monitor,
                                         log=lambda m: self.q.put(("log", m))))
    def _kick_inventory(self) -> None:
        """Varre programas/SO em segundo plano e anuncia o resultado no log."""
        def work() -> None:
            try:
                from cuagent import inventory
                data = inventory.scan()
                self.q.put((
                    "log",
                    f"📋 Escaneado: {len(data['programas'])} programas · "
                    f"{data['so']}",
                ))
            except Exception as exc:
                self.q.put(("log", f"! inventário: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _persist_auto(self) -> None:
        """Grava a preferência de confirmação (automático ou não) no .env."""
        try:
            write_env({"CUA_AUTO_APPROVE":
                       "1" if self.auto_switch.get() else "0"})
        except Exception:
            pass

    def _place_on_screen(self) -> None:
        """Restaura a última posição (pode ser o 2º monitor) ou centraliza."""
        saved = os.getenv("CUA_WIN_GEOMETRY", "").strip()
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", saved)
        if m:
            try:
                w, h, gx, gy = int(m[1]), int(m[2]), int(m[3]), int(m[4])
                import ctypes
                u32 = ctypes.windll.user32
                # SM_X/YVIRTUALSCREEN + SM_CX/CYVIRTUALSCREEN (espaço virtual)
                ux, uy = u32.GetSystemMetrics(76), u32.GetSystemMetrics(78)
                uw, uh = u32.GetSystemMetrics(80), u32.GetSystemMetrics(81)
                # só restaura se a janela ainda cair em algum monitor
                if (gx < ux + uw and gx + w > ux
                        and gy < uy + uh and gy + h > uy):
                    self.geometry(f"{w}x{h}{gx:+d}{gy:+d}")
                    return
            except Exception:
                pass
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            # SPI_GETWORKAREA = 0x0030 (área útil, sem a barra de tarefas)
            ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
            work_w = rect.right - rect.left
            work_h = rect.bottom - rect.top
            width = min(self.winfo_width(), work_w)
            height = min(self.winfo_height(), work_h)
            x = rect.left + max(0, (work_w - width) // 2)
            y = rect.top + max(0, (work_h - height) // 2)
            if (width, height) != (self.winfo_width(), self.winfo_height()):
                self.geometry(f"{width}x{height}+{x}+{y}")
            else:
                self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    # ---------------------------------------------------------------- layout

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)
        bold = ctk.CTkFont(size=14, weight="bold")

        # ---- topo ----
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 4))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            top, text="🖥️  Helpdesk-OpenCode",
            font=ctk.CTkFont(size=21, weight="bold"),
        ).grid(row=0, column=0, sticky="w")

        # seletor de monitor direto no topo: troca na hora e grava no .env
        mon_row = ctk.CTkFrame(top, fg_color="transparent")
        mon_row.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            mon_row, text="🖥 Monitor:", text_color="gray70",
            font=ctk.CTkFont(size=13),
        ).pack(side="left", padx=(0, 6))
        monitores = self.screen.list_monitors() or [
            {"index": 1, "label": "Monitor 1 — principal"}]
        self._mon_labels = [m["label"] for m in monitores]
        self._mon_idx = [m["index"] for m in monitores]
        self.mon_menu = ctk.CTkOptionMenu(
            mon_row, values=self._mon_labels, width=250,
            dynamic_resizing=False, fg_color="gray25",
            button_color="#2f8f4e", button_hover_color="#37a35a",
            command=self._change_monitor,
        )
        try:
            self.mon_menu.set(
                self._mon_labels[self._mon_idx.index(self.screen.monitor)])
        except ValueError:
            self.mon_menu.set(self._mon_labels[0])
        self.mon_menu.pack(side="left")

        ctk.CTkButton(
            top, text="⚙  Configurações", width=150,
            fg_color="gray25", hover_color="gray35", command=self._open_settings,
        ).grid(row=0, column=2, sticky="e", padx=(12, 0))
        ctk.CTkButton(
            top, text="❓  Como usar", width=130,
            fg_color="gray25", hover_color="gray35", command=self._show_help,
        ).grid(row=0, column=3, sticky="e", padx=(8, 0))

        # ---- esquerda: preview da tela (cartão) ----
        left = ctk.CTkFrame(self, corner_radius=12)
        left.grid(row=1, column=0, sticky="nsew", padx=(18, 8), pady=8)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(left, text="🖥  O que o agente vê", font=bold).grid(
            row=0, column=0, pady=(12, 6)
        )
        self.shot_label = ctk.CTkLabel(
            left, text="carregando tela…", fg_color="#101010",
            corner_radius=10, width=440, height=280,
            border_width=1, border_color="gray35",
        )
        self.shot_label.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 6))
        self.shot_info = ctk.CTkLabel(left, text="", text_color="gray65")
        self.shot_info.grid(row=2, column=0, pady=(0, 14))

        # ---- direita: abas Atividade / Assistente (cartão) ----
        right = ctk.CTkFrame(self, corner_radius=12)
        right.grid(row=1, column=1, sticky="nsew", padx=(8, 18), pady=8)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        self.tabs = ctk.CTkTabview(
            right, anchor="nw",
            segmented_button_fg_color=("gray86", "gray20"),
            segmented_button_selected_color="#2f8f4e",
            segmented_button_selected_hover_color="#37a35a",
            segmented_button_unselected_color=("gray80", "gray15"),
        )
        self.tabs.grid(row=0, column=0, sticky="nsew", padx=8, pady=(10, 8))
        self.tabs.add("Atividade")
        self.tabs.add("💡 Assistente")
        self._build_routines_tab()

        # — aba Atividade (log do agente) —
        t_log = self.tabs.tab("Atividade")
        t_log.grid_rowconfigure(0, weight=1)
        t_log.grid_columnconfigure(0, weight=1)
        self.log_box = ctk.CTkTextbox(
            t_log, wrap="word", font=ctk.CTkFont(family="Consolas", size=13)
        )
        self.log_box.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.log_box.configure(state="disabled")

        # — aba Assistente (leitura proativa da tela) —
        t_w = self.tabs.tab("💡 Assistente")
        t_w.grid_rowconfigure(2, weight=1)
        t_w.grid_columnconfigure(0, weight=1)

        w_head = ctk.CTkFrame(t_w, fg_color="transparent")
        w_head.grid(row=0, column=0, sticky="ew", padx=6, pady=(8, 2))
        self.watch_switch = ctk.CTkSwitch(
            w_head, text="Ler a tela e sugerir", progress_color="#2f8f4e",
            command=self._toggle_watcher,
        )
        self.watch_switch.pack(side="left")
        ctk.CTkLabel(w_head, text="a cada", text_color="gray65").pack(
            side="left", padx=(14, 4))
        self.watch_interval = ctk.CTkOptionMenu(
            w_head, values=WATCH_OPTIONS, width=76, button_color="gray30",
            dropdown_fg_color="gray20", command=self._change_watch_interval,
        )
        self.watch_interval.set(self._initial_watch_interval())
        self.watch_interval.pack(side="left")

        status_card = ctk.CTkFrame(t_w, corner_radius=10,
                                   fg_color=("gray90", "gray17"))
        status_card.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 6))
        status_card.grid_columnconfigure(0, weight=1)
        self.watch_status = ctk.CTkLabel(
            status_card,
            text="Desligado — ative para a IA observar sua tela e "
                 "preparar respostas enquanto você trabalha.",
            text_color="gray65", font=ctk.CTkFont(size=12),
            anchor="w", justify="left", wraplength=520,
        )
        self.watch_status.grid(row=0, column=0, sticky="ew", padx=12, pady=7)

        self.watch_box = ctk.CTkTextbox(
            t_w, wrap="word", font=ctk.CTkFont(size=13),
            border_width=1, border_color=("gray75", "gray35"),
            corner_radius=8,
        )
        self.watch_box.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self._watch_hint()

        w_btns = ctk.CTkFrame(t_w, fg_color="transparent")
        w_btns.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 8))
        self.copy_btn = ctk.CTkButton(
            w_btns, text="📋  Copiar resposta", width=155, state="disabled",
            fg_color="gray25", hover_color="gray35", command=self._copy_reply,
        )
        self.copy_btn.pack(side="left", padx=(0, 8))
        self.task_btn = ctk.CTkButton(
            w_btns, text="↗  Usar como tarefa", width=155, state="disabled",
            fg_color="gray25", hover_color="gray35", command=self._use_as_task,
        )
        self.task_btn.pack(side="left", padx=(0, 8))
        self.clear_btn = ctk.CTkButton(
            w_btns, text="🧹  Limpar", width=95, state="disabled",
            fg_color="gray25", hover_color="gray35", command=self._clear_watch,
        )
        self.clear_btn.pack(side="left")

        w_send = ctk.CTkFrame(t_w, fg_color="transparent")
        w_send.grid(row=4, column=0, sticky="ew", padx=6, pady=(0, 8))
        self.send_btn = ctk.CTkButton(
            w_send, text="📨  Mandar pro cliente…", state="disabled",
            fg_color="#2f8f4e", hover_color="#37a35a",
            command=self._send_to_client,
        )
        self.send_btn.pack(side="left", fill="x", expand=True)

        # ---- rodapé: tarefa + controles (cartão) ----
        bottom = ctk.CTkFrame(self, corner_radius=12)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 4))
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(bottom, text="⌨  Tarefa", font=bold).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 0))
        self.task_box = ctk.CTkTextbox(
            bottom, height=64, font=ctk.CTkFont(size=14),
            border_width=1, border_color=("gray75", "gray35"), corner_radius=8,
        )
        self.task_box.grid(row=1, column=0, columnspan=2, sticky="ew",
                           padx=14, pady=(6, 8))
        self.task_box.bind("<Control-Return>", self._shortcut)

        controls = ctk.CTkFrame(bottom, fg_color="transparent")
        controls.grid(row=2, column=0, sticky="w", padx=(14, 0), pady=(0, 12))
        self.run_btn = ctk.CTkButton(
            controls, text="▶  Executar", width=150, height=38,
            fg_color="#2f8f4e", hover_color="#37a35a", command=self._start
        )
        self.run_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ctk.CTkButton(
            controls, text="■  Parar", width=110, height=38, state="disabled",
            fg_color="#7a1f1f", hover_color="#a52a2a", command=self._stop,
        )
        self.stop_btn.pack(side="left", padx=(0, 18))
        auto_default = os.getenv("CUA_AUTO_APPROVE", "1").strip().lower() \
            not in {"0", "false", "no", "off"}
        self.auto_switch = ctk.CTkSwitch(
            controls, text="Executar sem confirmar cada ação",
            progress_color="#2f8f4e",
            command=self._persist_auto,
        )
        if auto_default:
            self.auto_switch.select()
        # versão OpenCode: o agente age sozinho (ações destrutivas pedem
        # confirmação numa caixa do Windows) — no lugar do switch, o modo
        modo = os.getenv("CUA_RUN_MODE", "Execução única")
        self.mode_var = ctk.StringVar(
            value=modo if modo in ("Execução única", "Loop", "Atendimento")
            else "Execução única")
        self.mode_seg = ctk.CTkSegmentedButton(
            controls, values=["Execução única", "Loop", "Atendimento"],
            variable=self.mode_var, command=self._persist_mode,
            selected_color="#2f8f4e", selected_hover_color="#37a35a",
        )
        self.mode_seg.pack(side="left")

        # opções do loop (só aparecem no modo Loop): N vezes ou contínuo
        self.loop_opts = ctk.CTkFrame(controls, fg_color="transparent")
        self.loop_times = ctk.CTkEntry(self.loop_opts, width=56, height=30,
                                       justify="center")
        self.loop_times.insert(0, os.getenv("CUA_LOOP_TIMES", "3"))
        self.loop_times.pack(side="left", padx=(10, 4))
        ctk.CTkLabel(self.loop_opts, text="vezes").pack(side="left", padx=(0, 12))
        self.loop_cont = ctk.CTkCheckBox(
            self.loop_opts, text="Contínuo", width=90,
            command=self._on_loop_cont, fg_color="#2f8f4e",
            hover_color="#37a35a")
        if os.getenv("CUA_LOOP_CONT", "0") == "1":
            self.loop_cont.select()
        self.loop_cont.pack(side="left")
        self._on_loop_cont(save=False)
        self._persist_mode(self.mode_var.get(), save=False)

        # 1 monitor só: o Helpdesk cobre os apps — minimiza durante a tarefa
        # (o agente vê/clica nos apps) e volta sozinho ao terminar.
        try:
            um_monitor = len(self.screen.list_monitors()) <= 1
        except Exception:
            um_monitor = True
        pref = os.getenv("CUA_MINIMIZE", "").strip()
        self.min_chk = ctk.CTkCheckBox(
            controls, text="Minimizar durante a tarefa (Ctrl+Alt+P para parar)",
            command=self._persist_min, fg_color="#2f8f4e", hover_color="#37a35a")
        if pref == "1" or (pref == "" and um_monitor):
            self.min_chk.select()
        self.min_chk.pack(side="left", padx=(14, 0))
        self._minimizado_pela_tarefa = False
        self._start_hotkey()
        ctk.CTkLabel(
            bottom, text="Ctrl+Enter executa · Enter quebra linha",
            text_color="gray60", font=ctk.CTkFont(size=12),
        ).grid(row=2, column=1, sticky="e", padx=(0, 14), pady=(0, 14))

        # ---- barra de status (pílula) ----
        self.status_var = ctk.StringVar(value="Pronto")
        pill = ctk.CTkFrame(self, corner_radius=10,
                            fg_color=("gray90", "gray17"))
        pill.grid(row=3, column=0, columnspan=2, sticky="ew",
                  padx=18, pady=(2, 10))
        pill.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            pill, textvariable=self.status_var, anchor="w",
            text_color="gray70", font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=5)

    # ------------------------------------------------------------- helpers UI

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _log(self, msg: str) -> None:
        self._progress_update(msg)
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        if "=== passo" in msg:
            step = msg.replace("=", "").strip()
            self.status_var.set(f"Executando — {step}")

    def _show_shot(self, source) -> None:
        """Exibe uma imagem de tela (aceita PIL.Image ou base64 PNG/JPEG)."""
        try:
            if isinstance(source, Image.Image):
                img = source
            else:
                img = Image.open(io.BytesIO(base64.b64decode(source)))
            if img.mode != "RGB":
                img = img.convert("RGB")
        except Exception:
            return
        scale = min(PREVIEW_MAX[0] / img.width, PREVIEW_MAX[1] / img.height, 1.0)
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        if img.size != size:  # redução única e barata (bilinear)
            img = img.resize(size, Image.BILINEAR)

        self._paint(img, size)

        model_w, model_h = self.screen.model_size()
        nat_w, nat_h = self.screen.native_size()
        info = f"Monitor {nat_w}×{nat_h} px"
        if (model_w, model_h) != (nat_w, nat_h):
            info += f" · IA vê {model_w}×{model_h}"
        if info != self._shot_info:  # texto constante: reconfigurar custava tempo
            self._shot_info = info
            self.shot_info.configure(text=info)

    def _paint(self, img: Image.Image, size: tuple[int, int]) -> None:
        """Atualiza os pixels do preview reaproveitando o PhotoImage.

        Bancada em loop real: trocar o objeto CTkImage a cada quadro custava
        ~9 ms (configure de imagem + largura/altura); colar os pixels novos
        no PhotoImage já criado custa ~0,2 ms e o Tk redesenha sozinho.
        """
        photo = self._photo
        if photo is None or self._photo_size != size:
            photo = ctk.CTkImage(dark_image=img, size=size)
            self._photo = photo
            self._photo_size = size
            self.shot_label.configure(image=photo, text="", width=size[0], height=size[1])
            return
        try:
            scaling = ctk.ScalingTracker.widget_scaling
            scaled = (max(1, round(size[0] * scaling)), max(1, round(size[1] * scaling)))
            src = img if img.size == scaled else img.resize(scaled, Image.BILINEAR)
            dark_pm = photo.create_scaled_photo_image(scaling, "dark")
            dark_pm.paste(src)
            light_pm = photo.create_scaled_photo_image(scaling, "light")
            if light_pm is not dark_pm:
                light_pm.paste(src)
        except Exception:
            # alguma coisa mudou (scaling/modo): refaz pelo caminho completo
            try:
                photo = ctk.CTkImage(dark_image=img, size=size)
                self._photo = photo
                self._photo_size = size
                self.shot_label.configure(image=photo, text="",
                                          width=size[0], height=size[1])
            except Exception:
                pass

    def _refresh_preview(self) -> None:
        """Captura a tela agora e atualiza o painel de preview."""
        try:
            self._show_shot(self.screen.capture_preview(PREVIEW_MAX))
        except Exception as exc:
            self._log(f"! falha ao capturar a tela: {exc}")

    def _auto_refresh(self) -> None:
        """Preview econômico: 1 quadro/s parado, ao vivo só durante a ação.

        O agente parado não enxerga nada novo — capturar a ~30 fps o tempo
        todo só queimava CPU/GPU para espelhar a própria tela. Durante
        tarefa/repetição quem pinta é o worker (msg "shot") e este loop
        só espera; no ocioso ele tira 1 foto/s para o painel viver.
        """
        try:
            minimizado = self.state() == "iconic"
        except Exception:
            minimizado = False
        if minimizado:
            # ninguém está vendo a prévia: não captura nada
            self._refresh_job = self.after(PREVIEW_IDLE_MS, self._auto_refresh)
            return
        if self._busy() or self._playing():
            # tarefa/repetição em andamento: libera CPU e evita disputa por
            # captura — o painel continua atualizando a cada ação (msg "shot")
            self._refresh_job = self.after(200, self._auto_refresh)
            return

        try:
            self._show_shot(self.screen.capture_preview(PREVIEW_MAX))
        except Exception:
            pass  # falha pontual de captura — tenta de novo no próximo ciclo
        self._refresh_job = self.after(PREVIEW_IDLE_MS, self._auto_refresh)

    def _cancel_refresh(self) -> None:
        """Cancela o auto-refresh antes da destruição (evita after órfão)."""
        if self._refresh_job is not None:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

    def _center(self, win: ctk.CTkToplevel) -> None:
        win.update_idletasks()
        x = self.winfo_x() + max(0, (self.winfo_width() - win.winfo_width()) // 2)
        y = self.winfo_y() + max(0, (self.winfo_height() - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")

    def _shortcut(self, _event) -> str:
        self._start()
        return "break"

    # ----------------------------------------------------------- fila de ev.

    def _poll(self) -> None:
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1])
                elif kind == "shot":
                    self._show_shot(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "confirm":
                    self._open_confirm(item[1], item[2], item[3])
                elif kind == "watch":
                    self._watch_update(item[1], item[2])
                elif kind == "play_status":
                    self.play_status.configure(text=item[1])
                elif kind == "play_done":
                    self.play_stop_btn.configure(state="disabled")
                    self.play_status.configure(text=item[1])
                    self.status_var.set(item[1])
                    self._log(f"— {item[1]} —")
                elif kind == "rot_refresh":
                    self._rot_refresh()
                elif kind == "rot_done":
                    self._analyzing = False
                    self.analyze_btn.configure(state="normal")
                    self._rot_refresh()
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "hotkey":
                    if self._busy():
                        self._stop()
                elif kind == "send_done":
                    self._send_done(item[1], item[2])
        except queue.Empty:
            pass
        self.after(120, self._poll)

    # --------------------------------------------------------------- execução

    def _start(self) -> None:
        if self._busy():
            return
        if self._playing():
            self.status_var.set(
                "Pare a repetição (■) antes de executar uma tarefa.")
            return
        task = self.task_box.get("1.0", "end").strip()
        if not task and self.mode_var.get() == "Atendimento":
            # atendimento sem instrução: atende a conversa aberta na tela
            task = ("Atenda a pessoa da conversa que está aberta na tela, como "
                    "um atendente educado e prestativo, ajudando a resolver o "
                    "problema ou a dúvida dela.")
            self._log("💬 Sem instrução: atendendo a conversa aberta na tela.")
        if not task:
            self.status_var.set("Escreva uma tarefa primeiro.")
            return

        # ---- rotina citada no chat? roda na hora, sem chamada de IA ----
        try:
            alvo = recmod.match_routine(task)
        except Exception as exc:
            self._log(f"! erro ao procurar rotina: {exc}")
            alvo = None
        if alvo is not None:
            rotina = alvo["routine"]
            vezes = alvo["times"]
            self._log(f"🔎 Tarefa = rotina '{rotina.get('name')}' {vezes}x — "
                      "rodando localmente, sem IA (veja ■ Parar).")
            self._rot_play(str(rotina.get("id")), times=vezes)
            return

        # versão OpenCode: quem decide os passos é o OpenCode (modelos
        # gratuitos do Zen); nenhuma chave de API é necessária aqui
        if find_opencode() is None:
            if INSTALL_STATE["status"] == "instalando":
                self.status_var.set("Aguarde: instalando o OpenCode (1ª vez)…")
            else:
                self._log("! OpenCode indisponível: "
                          f"{INSTALL_STATE['erro'] or 'reabra o app com internet'}")
            return
        self.stop_event.clear()
        self.agent = OpenCodeAgent(
            self.screen,
            on_log=lambda m: self.q.put(("log", m)),
            on_screenshot=lambda b: self.q.put(("shot", b)),
            should_stop=self.stop_event.is_set,
        )

        self.run_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("Executando…")
        if self.watcher.running:      # evita leituras duplas durante a tarefa
            self.watcher.set_paused(True)
            self._watch_status_paused()
        self._log(f"\n▶ Tarefa: {task.replace(chr(10), ' ')}")

        # lido aqui (thread da interface): o worker não pode tocar no Tk
        self._run_loop = self.mode_var.get() == "Loop"
        self._run_limit = self._loop_limit() if self._run_loop else 1
        self._run_atend = self.mode_var.get() == "Atendimento"
        self._run_minimize = bool(self.min_chk.get())
        self.worker = threading.Thread(target=self._worker, args=(task,), daemon=True)
        self.worker.start()
        if self._run_minimize:
            self._minimizado_pela_tarefa = True
            self.iconify()
            self._progress_show()

    # ------------------------------------------------------ painel de progresso

    def _progress_show(self) -> None:
        """Painel discreto no canto (só com o Helpdesk minimizado).

        Invisível para os prints do agente (WDA_EXCLUDEFROMCAPTURE) e
        atravessável por cliques — nunca atrapalha o que a IA vê ou clica.
        Feito em Tk puro (sem o ajuste automático de escala do
        CustomTkinter, que o reposicionava) e dimensionado pela escala REAL
        do monitor de destino: fica igual em qualquer resolução/escala.
        """
        import tkinter as tk
        esc = self._monitor_scale()
        if getattr(self, "_ov", None) is None or getattr(self, "_ov_esc", None) != esc:
            if getattr(self, "_ov", None) is not None:
                self._ov.destroy()
            px = lambda v: max(1, round(v * esc))          # noqa: E731
            ov = tk.Toplevel(self)
            ov.withdraw()
            ov.overrideredirect(True)
            ov.attributes("-topmost", True)
            ov.attributes("-alpha", 0.9)
            ov.configure(bg="#1b1f24")
            fonte = "Segoe UI"
            self._ov_title = tk.Label(ov, text="", anchor="w", bg="#1b1f24",
                                      fg="#7ee2a0", font=(fonte, -px(13), "bold"))
            self._ov_title.pack(fill="x", padx=px(12), pady=(px(8), 0))
            self._ov_step = tk.Label(ov, text="", anchor="w", justify="left",
                                     bg="#1b1f24", fg="#e6e6e6",
                                     wraplength=px(350), font=(fonte, -px(12)))
            self._ov_step.pack(fill="x", padx=px(12), pady=(px(2), 0))
            tk.Label(ov, text="Ctrl+Alt+P para parar", anchor="w", bg="#1b1f24",
                     fg="#8a8f96", font=(fonte, -px(10))
                     ).pack(fill="x", padx=px(12), pady=(px(2), px(8)))
            self._ov, self._ov_esc = ov, esc
            self._ov_size = (px(380), px(98))
            # tamanho fixo: texto novo não pode esticar/encolher o painel
            ov.pack_propagate(False)
            ov.geometry(f"{self._ov_size[0]}x{self._ov_size[1]}")
        self._ov_t0 = time.monotonic()
        self._ov_passos = 0
        self._ov_title.configure(text="⏳ Helpdesk trabalhando…")
        self._ov_step.configure(text="iniciando…")
        self._ov.deiconify()
        self._ov.update_idletasks()
        self._progress_winstyle()
        self._progress_place()
        self._progress_tick()

    def _monitor_scale(self) -> float:
        """Escala do Windows no monitor do agente (1.0 = 100%, 1.25 = 125%)."""
        try:
            import ctypes
            from ctypes import wintypes
            left, top, mw, mh = self.screen.monitor_rect()
            pt = wintypes.POINT(left + mw // 2, top + mh // 2)
            hmon = ctypes.windll.user32.MonitorFromPoint(pt, 2)   # NEAREST
            dx, dy = ctypes.c_uint(), ctypes.c_uint()
            ctypes.windll.shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx),
                                                  ctypes.byref(dy))
            return max(1.0, dx.value / 96.0)
        except Exception:
            return 1.0

    def _progress_place(self) -> None:
        """Canto inferior direito do monitor do agente, em pixels reais."""
        ov = getattr(self, "_ov", None)
        if ov is None:
            return
        try:
            left, top, mw, mh = self.screen.monitor_rect()
        except Exception:
            left, top, mw, mh = 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = self._ov_size
        w, h = min(w, mw - 8), min(h, mh - 8)            # tela pequena: cabe
        margem = max(8, round(mh * 0.012))
        barra = round(mh * 0.05)                         # folga p/ barra de tarefas
        x, y = left + mw - w - margem, top + mh - h - margem - barra
        if sys.platform == "win32":
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetAncestor(ov.winfo_id(), 2)
                ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x4 | 0x10)
                return
            except Exception:
                pass
        ov.geometry(f"{w}x{h}+{x}+{y}")

    def _progress_winstyle(self) -> None:
        if sys.platform != "win32" or getattr(self, "_ov", None) is None:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetAncestor(self._ov.winfo_id(), 2)       # GA_ROOT
            # fora dos prints (Windows 10 2004+): a IA não vê o painel
            user32.SetWindowDisplayAffinity(hwnd, 0x11)             # WDA_EXCLUDEFROMCAPTURE
            ex = user32.GetWindowLongW(hwnd, -20)
            # LAYERED|TRANSPARENT (cliques atravessam), TOOLWINDOW (sem
            # botão na barra), NOACTIVATE (nunca rouba o foco)
            user32.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80 | 0x08000000)
        except Exception:
            pass

    def _progress_tick(self) -> None:
        ov = getattr(self, "_ov", None)
        if ov is None or not ov.winfo_viewable():
            return
        seg = int(time.monotonic() - self._ov_t0)
        self._ov_title.configure(
            text=f"⏳ Helpdesk trabalhando… {seg // 60}:{seg % 60:02d}"
                 + (f" · {self._ov_passos} ação(ões)" if self._ov_passos else ""))
        self.after(1000, self._progress_tick)

    def _progress_update(self, msg: str) -> None:
        ov = getattr(self, "_ov", None)
        if ov is None or not ov.winfo_viewable():
            return
        linha = msg.strip()
        if not linha or linha.startswith("—"):
            return
        if linha.startswith(">"):
            self._ov_passos += 1
            linha = "▶ " + linha[1:].strip()
        self._ov_step.configure(text=linha[:160])

    def _progress_hide(self) -> None:
        ov = getattr(self, "_ov", None)
        if ov is not None:
            ov.withdraw()

    def _persist_min(self) -> None:
        try:
            write_env({"CUA_MINIMIZE": "1" if self.min_chk.get() else "0"})
        except Exception:
            pass

    def _start_hotkey(self) -> None:
        """Ctrl+Alt+P global: para a tarefa mesmo com o Helpdesk minimizado."""
        if sys.platform != "win32":
            return

        def loop():
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            # MOD_ALT|MOD_CONTROL|MOD_NOREPEAT, tecla P
            if not user32.RegisterHotKey(None, 0xB00B, 0x1 | 0x2 | 0x4000, 0x50):
                return                      # outro app já usa o atalho
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:   # WM_HOTKEY
                    self.q.put(("hotkey",))

        threading.Thread(target=loop, daemon=True).start()

    def _persist_mode(self, value: str, save: bool = True) -> None:
        if value == "Loop":
            self.loop_opts.pack(side="left")
        else:
            self.loop_opts.pack_forget()
        if save:
            try:
                write_env({"CUA_RUN_MODE": value})
            except Exception:
                pass

    def _on_loop_cont(self, save: bool = True) -> None:
        """Contínuo desativa o campo de vezes (roda até o ■ Parar)."""
        cont = bool(self.loop_cont.get())
        self.loop_times.configure(state="disabled" if cont else "normal")
        if save:
            try:
                write_env({"CUA_LOOP_CONT": "1" if cont else "0"})
            except Exception:
                pass

    def _loop_limit(self) -> int | None:
        """Nº de voltas do loop; None = contínuo."""
        if self.loop_cont.get():
            return None
        try:
            n = max(1, int(self.loop_times.get().strip()))
        except ValueError:
            n = 1
        try:
            write_env({"CUA_LOOP_TIMES": str(n)})
        except Exception:
            pass
        return n

    # ------------------------------------------------------------ atendimento

    _ATEND_REGRA = (
        "\n\n[Modo atendimento: você conversa com a pessoa pela tela, turno "
        "a turno. Leia a conversa atual. Se a ÚLTIMA mensagem for da pessoa e "
        "ainda não tiver resposta, envie UMA resposta útil (em português do "
        "Brasil) que avance para a solução e termine. Se a última mensagem for "
        "SUA (esperando a pessoa), NÃO faça nenhuma ação e responda só: "
        "AGUARDANDO. Se a pessoa confirmou que o problema foi resolvido (ou "
        "se despediu), responda só: RESOLVIDO.]")

    def _tela_sig(self):
        """Assinatura barata da tela (sem IA) para notar mensagem nova."""
        from PIL import ImageOps
        img = self.screen.capture_preview((160, 90))
        return ImageOps.grayscale(img).resize((96, 54))

    def _mudou(self, a, b) -> bool:
        from PIL import ImageChops, ImageStat
        diff = ImageChops.difference(a, b)
        # média ignora piscar de cursor/relógio; mensagem nova move vários px
        return ImageStat.Stat(diff).mean[0] > 1.2

    def _worker_atendimento(self, task: str) -> None:
        turnos = 0
        try:
            assert self.agent is not None
            while True:
                turnos += 1
                self.q.put(("log", f"💬 turno {turnos}"))
                self.q.put(("status", "Atendimento — respondendo… (■ Parar encerra)"))
                self.agent.run(task + self._ATEND_REGRA)
                final = (self.agent.last_text or "").upper()
                if "RESOLVIDO" in final:
                    self.q.put(("done", f"Atendimento concluído — {turnos} turno(s)."))
                    return
                # espera a pessoa responder: vigia a tela SEM IA (zero tokens)
                self.q.put(("status", "Atendimento — aguardando resposta da pessoa…"))
                if self.stop_event.wait(2.0):
                    raise safety.Abort("Parada solicitada pelo usuário.")
                base = self._tela_sig()
                while True:
                    if self.stop_event.wait(2.0):
                        raise safety.Abort("Parada solicitada pelo usuário.")
                    atual = self._tela_sig()
                    if self._mudou(base, atual):
                        # deixa a mensagem terminar de chegar/renderizar
                        if self.stop_event.wait(2.5):
                            raise safety.Abort("Parada solicitada pelo usuário.")
                        break
        except safety.Abort:
            self.q.put(("done", f"Atendimento parado — {turnos} turno(s)."))
        except Exception as exc:
            self.q.put(("log", f"! erro: {exc}"))
            self.q.put(("done", f"Falhou: {exc}"))

    def _worker(self, task: str) -> None:
        if getattr(self, "_run_minimize", False):
            # espera a janela sumir antes do 1º print (senão ela aparece nele)
            time.sleep(0.8)
        if getattr(self, "_run_atend", False):
            self._worker_atendimento(task)
            return
        loop = self._run_loop
        limite = self._run_limit
        total = "∞" if limite is None else str(limite)
        voltas = 0
        try:
            assert self.agent is not None
            while True:
                voltas += 1
                if loop:
                    self.q.put(("log", f"🔁 volta {voltas}/{total}"))
                    self.q.put(("status", f"Loop — volta {voltas}/{total} (■ Parar encerra)"))
                pedido = task
                if loop:
                    # cada volta = a tarefa UMA vez; sem memória o modelo
                    # repetia tudo (5 mensagens por volta em vez de 1)
                    pedido = (f"{task}\n\n[Modo loop: esta é a volta {voltas}"
                              f"{'' if limite is None else f' de {limite}'}. "
                              "Execute a tarefa UMA única vez nesta volta — se "
                              "for enviar mensagem, envie apenas UMA.")
                    if self.agent.typed:
                        ja = " | ".join(t[:120] for t in self.agent.typed[-15:])
                        pedido += (" Já foi digitado nas voltas anteriores "
                                   f"(não repita, faça diferente): {ja}")
                    pedido += "]"
                msg = self.agent.run(pedido)
                if not loop:
                    break
                if limite is not None and voltas >= limite:
                    msg = f"Loop concluído — {voltas} volta(s)."
                    break
                # pausa curta entre voltas (a tela assenta); ■ Parar corta
                if self.stop_event.wait(1.5):
                    raise safety.Abort("Parada solicitada pelo usuário.")
            self.q.put(("done", msg or "Tarefa concluída."))
        except safety.Abort as exc:
            if loop:
                feitas = voltas - 1
                self.q.put(("done", f"Loop parado — {feitas} volta(s) concluída(s)."))
            else:
                self.q.put(("done", str(exc)))
        except Exception as exc:
            self.q.put(("log", f"! erro: {exc}"))
            self.q.put(("log", traceback.format_exc(limit=6)))
            self.q.put(("done", f"Falhou: {exc}"))

    def _finish(self, message: str) -> None:
        self.run_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.stop_event.clear()
        if self.watcher.running:
            self.watcher.set_paused(False)
            if self.watch_switch.get():
                self._watch_status_on()
        self.status_var.set(message)
        self._log(f"— {message} —")
        self._progress_hide()
        if self._minimizado_pela_tarefa:
            self._minimizado_pela_tarefa = False
            try:
                self.deiconify()
                self.lift()
                self.focus_force()
            except Exception:
                pass

    def _stop(self) -> None:
        if not self._busy():
            return
        self.stop_event.set()
        self._release_pending()     # destrava o agente preso no diálogo
        self.status_var.set("Parando…")
        self._log("■ Parada solicitada.")

    # ------------------------------------------------------- confirmação/stop

    def _approver(self, action: dict) -> str:
        """Chamado pela thread do agente: abre diálogo e bloqueia até a escolha."""
        if self.auto_switch.get() and not safety.is_destructive(action):
            return "ok"
        event = threading.Event()
        holder: dict = {}
        self._pending = (event, holder)
        self.q.put(("confirm", action, event, holder))
        while not event.wait(0.1):
            if self.stop_event.is_set():
                raise safety.Abort("Parada solicitada pelo usuário.")
        self._pending = None
        result = str(holder.get("result", "ok"))
        if result == "quit":
            raise safety.Abort("Execução encerrada pelo usuário.")
        return result

    def _open_confirm(self, action: dict, event, holder) -> None:
        if event.is_set() or self.stop_event.is_set():
            return                  # parada chegou antes do diálogo abrir
        desc = safety.describe(action) or str(action.get("action", "?"))
        win = ctk.CTkToplevel(self)
        win.title("Confirmar ação")
        win.geometry("540x235")
        win.resizable(False, False)
        win.transient(self)
        win.attributes("-topmost", True)
        try:
            win.grab_set()
        except Exception:
            pass

        def choose(result: str) -> None:
            holder["result"] = result
            try:
                win.grab_release()
            except Exception:
                pass
            win.destroy()
            event.set()
            if self._pending and self._pending[1] is holder:
                self._pending = None

        ctk.CTkLabel(win, text="O agente quer executar:", text_color="gray70").pack(
            pady=(18, 2)
        )
        ctk.CTkLabel(
            win, text=desc, font=ctk.CTkFont(size=16, weight="bold"),
            wraplength=490, justify="center",
        ).pack(pady=6, padx=16)
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=16)
        ctk.CTkButton(
            row, text="▶  Executar", width=140, command=lambda: choose("ok")
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row, text="↩  Pular", width=110, fg_color="gray30",
            hover_color="gray42", command=lambda: choose("skip"),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            row, text="⏹  Encerrar", width=120, fg_color="#7a1f1f",
            hover_color="#a52a2a", command=lambda: choose("quit"),
        ).pack(side="left", padx=6)
        win.protocol("WM_DELETE_WINDOW", lambda: choose("quit"))
        self._confirm_win = win
        self._center(win)
        win.focus_force()

    def _release_pending(self) -> None:
        if self._pending is not None:
            event, holder = self._pending
            holder["result"] = "quit"
            event.set()
            self._pending = None
        win = getattr(self, "_confirm_win", None)
        self._confirm_win = None
        if win is not None:
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass

    def _remember_geometry(self) -> None:
        """Guarda a posição da janela (ex.: aberta no segundo monitor)."""
        try:
            geom = self.geometry()
            if re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", geom):
                write_env({"CUA_WIN_GEOMETRY": geom})
        except Exception:
            pass

    def _on_close(self) -> None:
        self._remember_geometry()
        oc_shutdown()
        self._stop_recording_quiet()
        self.watcher.stop()
        if self.cliente is not None:
            self.cliente.stop()
        if not self._busy() and not self._playing():
            self._play_stop_quiet()
            self._cancel_refresh()
            self.screen.close()
            self.destroy()
            return
        win = ctk.CTkToplevel(self)
        win.title("Encerrar?")
        win.geometry("460x160")
        win.transient(self)
        win.grab_set()
        ctk.CTkLabel(
            win, text="Há uma atividade em andamento.\nEncerrar mesmo assim?",
            justify="center",
        ).pack(pady=(20, 10))

        def confirm() -> None:
            self.stop_event.set()
            self._play_stop_quiet()
            self._release_pending()
            win.destroy()
            self._cancel_refresh()
            self.screen.close()
            self.destroy()

        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=6)
        ctk.CTkButton(
            row, text="Encerrar", width=120, fg_color="#7a1f1f",
            hover_color="#a52a2a", command=confirm,
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            row, text="Continuar", width=120, fg_color="gray30",
            hover_color="gray42", command=win.destroy,
        ).pack(side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._center(win)

    # ------------------------------------------------------------- assistente

    def _initial_watch_interval(self) -> str:
        raw = os.getenv("CUA_WATCH_INTERVAL", "").strip()
        try:
            label = f"{int(float(raw.replace(',', '.').split()[0]))} s"
            if label in WATCH_OPTIONS:
                return label
        except (ValueError, TypeError, IndexError):
            pass
        return WATCH_DEFAULT

    def _interval_seconds(self) -> float:
        try:
            return float(self.watch_interval.get().split()[0])
        except Exception:
            return 15.0

    def _watch_status_on(self) -> None:
        secs = int(self._interval_seconds())
        self.watch_status.configure(
            text=f"👀 Observando a cada {secs} s — contexto, resposta pronta "
                 "e próximo passo aparecem aqui.",
            text_color="gray65",
        )

    def _watch_status_paused(self) -> None:
        self.watch_status.configure(
            text="⏸ Pausado enquanto uma tarefa executa (retoma sozinho ao final).",
            text_color="gray65",
        )

    def _ensure_cliente(self):
        """Cria o rastreador do chat no primeiro uso (não roda à toa)."""
        if self.cliente is None:
            self.cliente = envio_chat.ClienteTracker()
        return self.cliente

    def _toggle_watcher(self) -> None:
        if self.watch_switch.get():
            key = (os.getenv("CUA_API_KEY", "").strip()
                   or os.getenv("GEMINI_API_KEY", "").strip())
            if not key or key.startswith("COLE_"):
                self.watch_switch.deselect()
                self.watch_status.configure(
                    text="⚠ Configure sua chave da API para usar o assistente.",
                    text_color="#e0b050",
                )
                self._log("→ Assistente precisa da chave: abra ⚙ Configurações.")
                self._open_settings()
                return
            self.watcher.start(self._interval_seconds())
            self._ensure_cliente()      # lembra o chat enquanto você atende
            if self._busy():
                self.watcher.set_paused(True)
                self._watch_status_paused()
            else:
                self._watch_status_on()
            self._log(
                f"💡 Assistente ligado (leitura a cada "
                f"{int(self._interval_seconds())} s) — a IA só é chamada "
                f"quando a tela muda.")
        else:
            self.watcher.stop()
            self.watch_status.configure(
                text="Desligado — ative para a IA observar sua tela e "
                     "preparar respostas enquanto você trabalha.",
                text_color="gray65",
            )
            self._log("💡 Assistente desligado.")

    def _change_monitor(self, label: str) -> None:
        """Troca o monitor que a IA lê/clica (seletor do topo) e grava."""
        try:
            idx = self._mon_idx[self._mon_labels.index(label)]
            self.screen.set_monitor(idx)
            write_env({"CUA_MONITOR": str(idx)})
            os.environ["CUA_MONITOR"] = str(idx)
            self._log(f"🖥 Agora a IA lê o Monitor {idx} — {label}")
        except Exception as exc:
            self._log(f"! não foi possível trocar o monitor: {exc}")

    def _change_watch_interval(self, label: str) -> None:
        try:
            seconds = float(label.split()[0])
        except Exception:
            return
        self.watcher.set_interval(seconds)
        try:
            write_env({"CUA_WATCH_INTERVAL": str(int(seconds))})
        except Exception:
            pass
        if self.watcher.running and not self.watcher.paused:
            self._watch_status_on()

    def _watch_update(self, kind: str, data) -> None:
        watching = bool(self.watch_switch.get())
        secs = int(self._interval_seconds())

        if kind == "thinking":
            if watching and not self.watcher.paused:
                self.watch_status.configure(
                    text="👀 Observando… consultando a IA agora.",
                    text_color="gray65")
            return

        if kind == "cache":
            # tela igual à última análise: só gira o relógio do painel
            hora = (data or {}).get("hora", "")
            ha = (data or {}).get("hora_analise", "")
            if watching and not self.watcher.paused:
                self.watch_status.configure(
                    text=(f"✔ {hora} — tela sem mudanças; análise de {ha} "
                          f"segue válida. Nova checagem em ~{secs} s."),
                    text_color="#7bd88f" if self._watch_payload else "gray65")
            return

        if kind == "result":
            hora = (data or {}).get("hora", "")
            self._watch_payload = data
            self._append_watch(
                f"🕐 {hora} ── CONTEXTO\n{data.get('contexto') or '—'}\n\n"
                f"💬 RESPOSTA PRONTA\n{data.get('resposta') or '—'}\n\n"
                f"➡ PRÓXIMO PASSO\n{data.get('proximo') or '—'}")
            self.copy_btn.configure(
                state="normal"
                if (data.get("resposta") or data.get("contexto"))
                else "disabled")
            self.task_btn.configure(
                state="normal"
                if (data.get("proximo") or data.get("resposta"))
                else "disabled")
            self.send_btn.configure(
                state="normal" if (data.get("resposta") or "").strip()
                else "disabled")
            self.clear_btn.configure(state="normal")
            if watching and not self.watcher.paused:
                dur = data.get("dur")
                self.watch_status.configure(
                    text=(f"✔ Leitura {hora} ({dur} s) — próxima em "
                          f"~{secs} s.") if dur else
                         f"✔ Leitura {hora} — próxima em ~{secs} s.",
                    text_color="#7bd88f")
            return

        if kind == "nada":
            hora = (data or {}).get("hora", "")
            self._append_watch(f"🕐 {hora} — nenhum atendimento na tela agora.")
            self.clear_btn.configure(state="normal")
            if watching and not self.watcher.paused:
                dur = data.get("dur")
                self.watch_status.configure(
                    text=(f"✔ Leitura {hora} ({dur} s) — nada relevante; "
                          f"nova leitura em ~{secs} s.") if dur else
                         f"✔ Leitura {hora} — nada relevante; nova leitura "
                         f"em ~{secs} s.",
                    text_color="gray65")
            return

        if kind == "error":
            msg = (data or {}).get("msg", "erro desconhecido")
            hora = (data or {}).get("hora", "")
            self._append_watch(f"🕐 {hora} — ⚠ {msg}")
            self._log(f"! assistente: {msg}")
            if (data or {}).get("fatal"):
                self.watch_switch.deselect()
                self.watch_status.configure(
                    text=f"⛔ Assistente desligado — {msg}",
                    text_color="#e05555")
            elif watching:
                w = (data or {}).get("wait")
                mant = (data or {}).get("mantendo")
                extra = (f"nova tentativa em ~{int(w)} s" if w
                         else "tento de novo no próximo ciclo")
                if mant:
                    extra += f"; mantendo análise de {mant}"
                self.watch_status.configure(
                    text=f"⚠ {msg} — {extra}.",
                    text_color="#e0b050")

    def _watch_hint(self) -> None:
        """Estado vazio do painel: dica em vez de caixa preta."""
        self.watch_box.configure(state="normal")
        self.watch_box.delete("1.0", "end")
        self.watch_box.insert(
            "1.0",
            '💡 Estado vazio — ligue "Ler a tela e sugerir" no topo e as '
            "leituras da tela aparecem aqui:\n"
            "     CONTEXTO do cliente · RESPOSTA PRONTA · PRÓXIMO PASSO.")
        try:
            box = self.watch_box._textbox
            box.tag_config("hint", foreground="#7a7a7a")
            box.tag_add("hint", "1.0", "end-1c")
        except Exception:
            pass
        self.watch_box.configure(state="disabled")

    def _append_watch(self, block: str) -> None:
        self._watch_blocks.append(block.strip())
        if len(self._watch_blocks) > WATCH_MAX_BLOCKS:
            del self._watch_blocks[:-WATCH_MAX_BLOCKS]
        self.watch_box.configure(state="normal")
        self.watch_box.delete("1.0", "end")
        if self._watch_blocks:
            self.watch_box.insert(
                "end",
                ("\n\n" + "─" * 46 + "\n\n").join(self._watch_blocks) + "\n")
        self.watch_box.configure(state="disabled")
        self.watch_box.see("end")

    def _copy_reply(self) -> None:
        if not self._watch_payload:
            return
        text = (self._watch_payload.get("resposta")
                or self._watch_payload.get("contexto") or "").strip()
        if not text or text == "—":
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.watch_status.configure(
            text="📋 Resposta copiada — cole no chat do cliente (Ctrl+V).",
            text_color="#7bd88f")

    def _use_as_task(self) -> None:
        if not self._watch_payload:
            return
        text = (self._watch_payload.get("proximo")
                or self._watch_payload.get("resposta") or "").strip()
        if not text or text == "—":
            return
        self.task_box.delete("1.0", "end")
        self.task_box.insert("1.0", text)
        self.task_box.focus_force()
        self.watch_status.configure(
            text="↗ Executando a tarefa sugerida pelo assistente…",
            text_color="gray65")
        self._start()

    # ------------------------------------------- mandar resposta pro cliente

    def _send_to_client(self) -> None:
        """Pergunta "quer mandar pro cliente?" — diálogo com destino+texto."""
        if not self._watch_payload:
            return
        texto = (self._watch_payload.get("resposta") or "").strip()
        if not texto or texto == "—":
            self.watch_status.configure(
                text="Não há RESPOSTA PRONTA nesta leitura para mandar.",
                text_color="#e0b341")
            return
        self._ensure_cliente()
        hwnd = self.cliente.hwnd if self.cliente.alive() else 0
        alvo = envio_chat.window_title(hwnd)

        dlg = ctk.CTkToplevel(self)
        dlg.title("Mandar pro cliente?")
        dlg.geometry("540x360")
        dlg.transient(self)
        dlg.grab_set()

        bold = ctk.CTkFont(size=14, weight="bold")
        ctk.CTkLabel(
            dlg, text="📨 Mandar esta resposta pro cliente?",
            font=bold,
        ).pack(padx=16, pady=(16, 4))
        if alvo:
            ctk.CTkLabel(
                dlg,
                text=f"Janela de destino: «{alvo}»",
                text_color="#7bd88f", anchor="w", justify="left",
                wraplength=500,
            ).pack(fill="x", padx=16, pady=(0, 4))
        else:
            ctk.CTkLabel(
                dlg,
                text="⚠ Nenhuma janela do chat encontrada. Abra o chat do "
                     "cliente e deixe o cursor piscando na caixa de "
                     "mensagem antes de mandar.",
                text_color="#e0b341", anchor="w", justify="left",
                wraplength=500,
            ).pack(fill="x", padx=16, pady=(0, 4))

        prev = ctk.CTkTextbox(dlg, height=110, font=ctk.CTkFont(size=13))
        prev.pack(fill="both", expand=True, padx=16, pady=4)
        prev.insert("1.0", texto[:600] + ("…" if len(texto) > 600 else ""))
        prev.configure(state="disabled")

        ctk.CTkLabel(
            dlg,
            text="No «Mandar agora» ele traz a janela pra frente, cola a "
                 "resposta (Ctrl+V) e aperta Enter — levam ~2 s, não "
                 "mexa no mouse/teclado. Se o seu chat não envia com "
                 "Enter, o texto fica colado: é só clicar em Enviar.",
            text_color="gray65", anchor="w", justify="left", wraplength=500,
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=16, pady=(4, 8))

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(pady=(0, 14))
        ctk.CTkButton(
            row, text="📨 Mandar agora", width=150,
            fg_color="#2f8f4e", hover_color="#37a35a",
            state="normal" if alvo else "disabled",
            command=lambda: self._send_confirm(dlg, hwnd, texto),
        ).pack(side="left", padx=8)
        ctk.CTkButton(
            row, text="Cancelar", width=110, fg_color="gray30",
            hover_color="gray42", command=dlg.destroy,
        ).pack(side="left", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

    def _send_confirm(self, dlg, hwnd: int, texto: str) -> None:
        """Usuário confirmou: fecha o diálogo e dispara o envio real."""
        titulo = envio_chat.window_title(hwnd)
        dlg.destroy()
        self.send_btn.configure(state="disabled")
        self.watch_status.configure(
            text=f"📨 Mandando pra «{titulo}»… não mexa no mouse/teclado.",
            text_color="#7bd88f")
        self._log(f"📨 Mandando a resposta pro cliente em «{titulo}» — "
                  "trazendo a janela, colando (Ctrl+V) e Enter…")
        threading.Thread(
            target=self._send_worker, args=(hwnd, texto),
            daemon=True,
        ).start()

    def _send_worker(self, hwnd: int, texto: str) -> None:
        """Thread: faz o envio real (não pode travar a GUI)."""
        try:
            ok, msg = envio_chat.send_text(hwnd, texto)
        except Exception as exc:
            ok, msg = False, f"erro ao enviar: {exc}"
        self.q.put(("send_done", ok, msg))

    def _send_done(self, ok: bool, msg: str) -> None:
        """Chegou o resultado do envio (chamado pela fila, na GUI)."""
        tem = bool(self._watch_payload
                   and (self._watch_payload.get("resposta") or "").strip())
        self.send_btn.configure(state="normal" if tem else "disabled")
        self.watch_status.configure(
            text=("✅ " if ok else "⚠ ") + msg,
            text_color="#7bd88f" if ok else "#e0b341")
        self._log(("✅ " if ok else "! ") + msg)

    def _clear_watch(self) -> None:
        self._watch_blocks.clear()
        self._watch_payload = None
        self._watch_hint()
        self.copy_btn.configure(state="disabled")
        self.task_btn.configure(state="disabled")
        self.send_btn.configure(state="disabled")
        self.clear_btn.configure(state="disabled")

    # --------------------------------------------------------------- rotinas

    def _playing(self) -> bool:
        return self._play_worker is not None and self._play_worker.is_alive()

    def _build_routines_tab(self) -> None:
        """Aba ⏺ Rotinas: gravar, repetir e analisar gravações com a IA."""
        self.tabs.add("⏺ Rotinas")
        t_r = self.tabs.tab("⏺ Rotinas")
        t_r.grid_rowconfigure(3, weight=1)
        t_r.grid_columnconfigure(0, weight=1)

        # — gravação —
        head = ctk.CTkFrame(t_r, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=6, pady=(8, 2))
        self.rec_btn = ctk.CTkButton(
            head, text="⏺  Gravar rotina", width=175,
            fg_color="#8f1f1f", hover_color="#a52a2a",
            command=self._rot_record,
        )
        self.rec_btn.pack(side="left")
        self.rec_stop_btn = ctk.CTkButton(
            head, text="⏹  Parar gravação", width=165, state="disabled",
            fg_color="gray30", hover_color="gray42",
            command=self._rot_stop_record,
        )
        self.rec_stop_btn.pack(side="left", padx=(8, 0))
        self.rec_status = ctk.CTkLabel(
            head, text="Parado — grave seus movimentos até ⏹ Parar.",
            text_color="gray65", font=ctk.CTkFont(size=12),
        )
        self.rec_status.pack(side="left", padx=14)

        # — dica —
        ctk.CTkLabel(
            t_r,
            text="Grave cliques, teclas, rolagens e as janelas/sites que você "
                 "acessa. A IA nomeia cada gravação; 🧠 Analisar tudo monta "
                 "sua ROTINA DO DIA — repita quantas vezes quiser com ▶.",
            text_color="gray55", font=ctk.CTkFont(size=12),
            anchor="w", justify="left", wraplength=760,
        ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

        # — barra: IA + repetição —
        bar = ctk.CTkFrame(t_r, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 6))
        self.analyze_btn = ctk.CTkButton(
            bar, text="🧠  Analisar tudo com IA", width=195,
            fg_color="#2f8f4e", hover_color="#37a35a",
            command=self._rot_analyze,
        )
        self.analyze_btn.pack(side="left")
        ctk.CTkButton(
            bar, text="↻  Atualizar lista", width=135,
            fg_color="gray25", hover_color="gray35",
            command=self._rot_refresh,
        ).pack(side="left", padx=(8, 0))
        self.play_stop_btn = ctk.CTkButton(
            bar, text="■  Parar repetição", width=155, state="disabled",
            fg_color="#7a1f1f", hover_color="#a52a2a",
            command=self._rot_stop_play,
        )
        self.play_stop_btn.pack(side="right")
        self.play_status = ctk.CTkLabel(
            bar, text="Pronto para repetir", text_color="gray65",
            font=ctk.CTkFont(size=12),
        )
        self.play_status.pack(side="right", padx=(0, 12))

        # — lista de rotinas —
        self.rot_list = ctk.CTkScrollableFrame(t_r)
        self.rot_list.grid(row=3, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self._rot_refresh()

    def _rot_refresh(self) -> None:
        """Redesenha a lista de rotinas salvas (gravadas e combos da IA)."""
        for child in self.rot_list.winfo_children():
            child.destroy()
        routines = recmod.list_routines()
        names = {str(r.get("id")): str(r.get("name") or r.get("id"))
                 for r in routines}
        if not routines:
            ctk.CTkLabel(
                self.rot_list,
                text="Nenhuma rotina gravada ainda — clique em ⏺ Gravar "
                     "rotina.",
                text_color="gray60",
            ).pack(padx=8, pady=14, anchor="w")
            return
        self._rot_times = {}
        for routine in routines:
            rid = str(routine.get("id"))
            is_combo = routine.get("kind") == "combo"
            row = ctk.CTkFrame(self.rot_list, fg_color=("gray88", "gray16"))
            row.pack(fill="x", padx=6, pady=4)
            left = ctk.CTkFrame(row, fg_color="transparent")
            left.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=6)
            prefix = "📚 " if is_combo else "⏺ "
            ctk.CTkLabel(
                left, text=prefix + str(routine.get("name") or rid),
                font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
            ).pack(fill="x")
            desc = str(routine.get("description") or "").strip()
            if desc:
                ctk.CTkLabel(
                    left, text=("✨ " if is_combo else "") + desc,
                    text_color="gray60", font=ctk.CTkFont(size=12),
                    anchor="w", wraplength=440, justify="left",
                ).pack(fill="x")
            if is_combo:
                steps = [s for s in (routine.get("steps") or [])
                         if isinstance(s, dict)]
                etapas = " → ".join(
                    names.get(str(s.get("id")), "?")
                    for s in steps[:8])
                meta = (f"{len(steps)} etapas"
                        + (f": {etapas}" if etapas else " — sem etapas"))
            else:
                secs = (routine.get("duration_ms") or 0) / 1000
                meta = (f"{len(routine.get('events') or [])} eventos · "
                        f"{secs:.0f} s")
            ctk.CTkLabel(
                left, text=meta, text_color="gray55",
                font=ctk.CTkFont(size=12), anchor="w",
            ).pack(fill="x")

            right = ctk.CTkFrame(row, fg_color="transparent")
            right.pack(side="right", padx=(0, 10))
            # "quantas vezes" — CTkOptionMenu (o customtkinter não tem
            # spinbox; e só renderiza linha quando há rotina salva)
            spin = ctk.CTkOptionMenu(
                right, width=74,
                values=[str(n) for n in range(1, 21)]
                       + ["30", "50", "100", "200", "500", "999"],
            )
            spin.set("1")
            spin.pack(side="left", padx=(0, 6))
            self._rot_times[rid] = spin
            ctk.CTkButton(
                right, text="▶", width=44,
                command=lambda i=rid: self._rot_play(i),
            ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                right, text="❌", width=42, fg_color="gray30",
                hover_color="#7a1f1f",
                command=lambda i=rid: self._rot_delete(i),
            ).pack(side="left")

    def _rot_record(self) -> None:
        """⏺ Começa a gravar os movimentos (hooks globais)."""
        if self.recorder.recording:
            return
        if self._playing():
            self.status_var.set("Pare a repetição antes de gravar.")
            return
        try:
            self.recorder.start()
        except Exception as exc:
            self._log(f"! não foi possível gravar: {exc}")
            self.status_var.set(f"Erro ao gravar: {exc}")
            return
        self.rec_btn.configure(state="disabled")
        self.rec_stop_btn.configure(state="normal")
        self.status_var.set("⏺ Gravando a rotina…")
        self._log("⏺ Gravação iniciada — use o mouse e o teclado "
                  "normalmente (os cliques neste painel são ignorados).")
        self._rec_tick()

    def _rec_tick(self) -> None:
        """Indicador ao vivo: tempo de gravação + contagem de eventos."""
        if not self.recorder.recording:
            return
        total, ms = self.recorder.stats()
        secs = int(ms // 1000)
        self.rec_status.configure(
            text=f"⏺ GRAVANDO {secs // 60:02d}:{secs % 60:02d} · "
                 f"{total} eventos")
        self._rec_job = self.after(250, self._rec_tick)

    def _rot_stop_record(self) -> None:
        """⏹ Para a gravação, salva e pede nome/descrição para a IA."""
        if not self.recorder.recording:
            return
        routine = self.recorder.stop()
        if self._rec_job is not None:
            try:
                self.after_cancel(self._rec_job)
            except Exception:
                pass
            self._rec_job = None
        self.rec_btn.configure(state="normal")
        self.rec_stop_btn.configure(state="disabled")
        self.rec_status.configure(text="Parado — gravação encerrada.")
        events = routine.get("events") or []
        if len(events) < 3:
            self._log("! gravação muito curta — nada relevante capturado; "
                      "descartada.")
            self.status_var.set("Gravação muito curta (descartada).")
            self._rot_refresh()
            return
        try:
            path = recmod.save_routine(routine)
        except Exception as exc:
            self._log(f"! erro ao salvar a rotina: {exc}")
            self.status_var.set("Erro ao salvar a rotina.")
            return
        secs = (routine.get("duration_ms") or 0) / 1000
        self._log(f"💾 Rotina salva: {len(events)} eventos · {secs:.0f} s "
                  f"→ {path.name}")
        self.status_var.set("Rotina salva — a IA vai nomeá-la…")
        self._rot_refresh()
        threading.Thread(target=self._rot_describe, args=(routine["id"],),
                         daemon=True).start()

    def _rot_describe(self, rid: str) -> None:
        """Pede à IA um nome e uma descrição para a rotina recém-gravada."""
        try:
            cfg = Config.from_env()
            routine = recmod.load_routine(rid)
            res = rotinas_ai.describe_routine(cfg, routine)
            routine["name"] = res["nome"]
            routine["description"] = res["descricao"]
            routine["ai"] = "ok"
            recmod.save_routine(routine)
            self.q.put(("log", f"✨ IA nomeou a rotina: {res['nome']}"))
            if res.get("descricao"):
                self.q.put(("log", f"   {res['descricao']}"))
            self.q.put(("status", "Rotina salva e nomeada pela IA."))
        except SystemExit as exc:
            self.q.put(("log", f"! {exc}"))
            self.q.put(("log", "→ Configure a chave em ⚙ Configurações."))
        except Exception as exc:
            self.q.put(("log", f"! IA não conseguiu nomear a rotina: {exc}"))
        finally:
            self.q.put(("rot_refresh",))

    def _rot_analyze(self) -> None:
        """🧠 Manda todas as gravações para a IA montar a Rotina do Dia."""
        if self._analyzing or self.recorder.recording or self._playing():
            return
        routines = [r for r in recmod.list_routines()
                    if r.get("kind", "rotina") == "rotina"]
        if not routines:
            self.status_var.set("Grave ao menos uma rotina antes de analisar.")
            self._log("🧠 Analisar tudo precisa de ao menos 1 rotina gravada.")
            return
        self._analyzing = True
        self.analyze_btn.configure(state="disabled")
        self._log(f"🧠 IA analisando {len(routines)} rotinas para montar "
                  "a Rotina do Dia…")
        threading.Thread(target=self._rot_analyze_work, args=(routines,),
                         daemon=True).start()

    def _rot_analyze_work(self, routines: list) -> None:
        """Thread: chama a IA e salva o combo (📚 Rotina do Dia)."""
        import uuid
        from datetime import datetime

        try:
            cfg = Config.from_env()
            res = rotinas_ai.analyze_routines(cfg, routines)
            now = datetime.now()
            combo = {
                "id": f"c_{now.strftime('%Y%m%d_%H%M%S')}_"
                      f"{uuid.uuid4().hex[:4]}",
                "kind": "combo",
                "name": res["nome"],
                "description": " · ".join(res["insights"][:3]),
                "steps": res["passos"],
                "created": now.isoformat(timespec="seconds"),
                "ai": "ok",
                "events": [],
                "screen": [0, 0],
                "duration_ms": 0,
            }
            recmod.save_routine(combo)
            self.q.put(("log", f"📚 Rotina do Dia criada: {combo['name']} "
                               f"({len(combo['steps'])} etapas)"))
            for insight in res["insights"][:3]:
                self.q.put(("log", f"💡 {insight}"))
            self.q.put(("status", "Rotina do Dia criada — repita com ▶."))
        except SystemExit as exc:
            self.q.put(("log", f"! {exc}"))
            self.q.put(("log", "→ Configure a chave em ⚙ Configurações."))
        except Exception as exc:
            self.q.put(("log", f"! análise da IA: {exc}"))
        finally:
            self.q.put(("rot_done",))

    def _rot_play(self, rid: str, times: int | None = None) -> None:
        """▶ Repete a rotina (ou combo) N vezes em um thread separado.

        ``times`` vem do chat quando o usuário pediu "rotina X N vezes";
        sem ele (▶ manual da linha), lê o seletor "quantas vezes".
        """
        if self._playing():
            return
        if self.recorder.recording:
            self.status_var.set("Pare a gravação antes de repetir.")
            return
        if self._busy():
            self.status_var.set(
                "Aguarde a tarefa atual (ou pare com ■) para repetir.")
            return
        try:
            routine = recmod.load_routine(rid)
            if times is None:
                spin = self._rot_times.get(rid)
                times = max(1, min(999, int(float(spin.get())))) \
                    if spin is not None else 1
            else:
                times = max(1, min(999, int(times)))
        except Exception as exc:
            self._log(f"! erro ao carregar a rotina: {exc}")
            return
        self._play_stop = threading.Event()
        self.play_stop_btn.configure(state="normal")
        self.play_status.configure(text="▶ Iniciando…")
        self.status_var.set(f"Repetindo '{routine.get('name')}' ({times}x)…")
        self._log(f"▶ Repetindo '{routine.get('name')}' {times}x — "
                  "■ Parar ou mouse no canto interrompe.")
        self._play_worker = threading.Thread(
            target=self._rot_play_work, args=(routine, times), daemon=True)
        self._play_worker.start()

    def _rot_play_work(self, routine: dict, times: int) -> None:
        """Thread: roda o replay e devolve a mensagem final pela fila."""

        def status(msg: str) -> None:
            self.q.put(("play_status", msg))

        try:
            if routine.get("kind") == "combo":
                msg = playmod.play_combo(
                    routine, times, stop_event=self._play_stop,
                    on_status=status, resolve=recmod.load_routine)
            else:
                msg = playmod.play(
                    routine, times, stop_event=self._play_stop,
                    on_status=status)
        except Exception as exc:
            msg = f"Falhou na repetição: {exc}"
            self.q.put(("log", f"! {msg}"))
        self.q.put(("play_done", msg))

    def _rot_stop_play(self) -> None:
        """■ Parar repetição: sinaliza a thread do replay."""
        if not self._playing():
            return
        if self._play_stop is not None:
            self._play_stop.set()
        self.play_status.configure(text="⏹ Parando após o evento atual…")
        self._log("■ Repetição: parada solicitada.")

    def _rot_delete(self, rid: str) -> None:
        """❌ Exclui a rotina (com confirmação)."""
        if self._playing():
            self.status_var.set("Pare a repetição antes de excluir.")
            return
        from tkinter import messagebox

        if not messagebox.askyesno(
                "Excluir rotina",
                "Excluir esta rotina gravada? (não dá para desfazer)"):
            return
        try:
            recmod.delete_routine(rid)
        except Exception as exc:
            self._log(f"! erro ao excluir: {exc}")
            return
        self._log(f"❌ Rotina excluída: {rid}")
        self._rot_refresh()

    def _play_stop_quiet(self) -> None:
        if self._play_stop is not None:
            self._play_stop.set()

    def _stop_recording_quiet(self) -> None:
        try:
            if self.recorder.recording:
                self.recorder.stop()
        except Exception:
            pass

    # ----------------------------------------------------------- configurações

    def _show_help(self) -> None:
        """Instruções de uso e requisitos — vão junto com o aplicativo."""
        win = ctk.CTkToplevel(self)
        win.title("Como usar")
        win.geometry("740x640")
        win.transient(self)
        win.grab_set()

        head = ctk.CTkFrame(win, fg_color="transparent")
        head.pack(fill="x", padx=22, pady=(16, 8))
        ctk.CTkLabel(
            head, text="\u2753  Como usar",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            head, text="guia r\u00e1pido \u2014 do zero \u00e0 primeira tarefa",
            text_color="gray60", font=ctk.CTkFont(size=13),
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        scroll = ctk.CTkScrollableFrame(win, corner_radius=10)
        scroll.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        scroll.grid_columnconfigure(0, weight=1)

        secoes = [
            ("\U0001f680  Primeiros passos",
             '1. \u2699 Configura\u00e7\u00f5es \u2192 cole sua chave da API (GRATUITA em\n'
             '   https://aistudio.google.com/apikey) \u2192 Salvar; a chave fica\n'
             '   guardada num .env ao lado do .exe.\n'
             '2. Escreva a tarefa em portugu\u00eas no campo Tarefa (ex.: "abra o\n'
             '   navegador e pesquise por clima em S\u00e3o Paulo").\n'
             '3. \u25b6 Executar (ou Ctrl+Enter): na abertura o programa varre o\n'
             '   computador e o agente abre qualquer aplicativo pedido.\n'
             '4. Tudo aparece na aba Atividade em tempo real: cada passo traz\n'
             '   "\U0001f9e0 ..." = o que a IA identificou na tela (janela, bot\u00e3o,\n'
             '   visor); ela executa todos os comandos e s\u00f3 conclui quando v\u00ea o\n'
             '   resultado. \u25a0 Parar encerra ap\u00f3s o passo atual. Feche a janela\n'
             '   para sair (pede confirma\u00e7\u00e3o se houver tarefa rodando).'),
            ("\U0001f5a5\ufe0f  Tela, preview e monitores",
             '\u2022 "O que o agente v\u00ea" mostra a tela (1 quadro/s parado; ao vivo\n'
             '  durante a tarefa) \u2014 \u00e9 o que o modelo enxerga.\n'
             '\u2022 V\u00e1rios monitores? Use o seletor \U0001f5a5 Monitor NO TOPO da janela\n'
             '  (ou \u2699 Configura\u00e7\u00f5es) para escolher qual tela a IA l\u00ea e clica \u2014\n'
             '  a troca vale na hora e fica salva no .env (CUA_MONITOR).\n'
             '\u2022 A janela pode ficar em qualquer monitor: a posi\u00e7\u00e3o \u00e9 lembrada\n'
             '  na pr\u00f3xima abertura (apague CUA_WIN_GEOMETRY para centralizar).'),
            ("\u26a1  Confirmar cada a\u00e7\u00e3o ou rodar sozinho",
             '\u2022 Por padr\u00e3o as a\u00e7\u00f5es executam SOZINHAS (o switch "Executar sem\n'
             '  confirmar cada a\u00e7\u00e3o" j\u00e1 vem ligado). Desligue para aprovar\n'
             '  cada uma \u2014 a\u00e7\u00f5es destrutivas (excluir, apagar, desligar) sempre\n'
             '  pedem confirma\u00e7\u00e3o.\n'
             '\u2022 N\u00e3o use mouse/teclado enquanto o agente roda: ele controla os\n'
             '  perif\u00e9ricos f\u00edsicos. Mouse no canto superior esquerdo aborta\n'
             '  (failsafe). Limite o consumo em \u2699 Configura\u00e7\u00f5es \u2192 M\u00e1x. passos.'),
            ("\U0001f4a1  Assistente (l\u00ea a tela e sugere)",
             '\u2022 Inicia DESLIGADO \u2014 nada roda sozinho: ligue "Ler a tela e\n'
             '  sugerir" quando quiser. Entrega CONTEXTO do cliente, RESPOSTA\n'
             '  PRONTA (\U0001f4cb Copiar) e PR\u00d3XIMO PASSO (\u2197 Usar como tarefa).\n'
             '\u2022 Tela parada = s\u00f3 o rel\u00f3gio gira (SEM chamar a IA \u2014 zero\n'
             '  tokens); mudou, consultou. Recusa da fila? Espera crescente\n'
             '  (10, 20, 40, 60 s) e mant\u00e9m a \u00faltima an\u00e1lise na tela. Pausa\n'
             '  sozinho enquanto uma tarefa executa.\n'
             '\u2022 Cada leitura consome tokens: 30 s/60 s gastam bem menos; o\n'
             '  status mostra a dura\u00e7\u00e3o (ex.: "\u2714 Leitura 14:02 (38 s)").\n'
             '  Em fila ele insiste at\u00e9 10 vezes; chave/modelo errado desliga\n'
             '  na hora.\n'
             '\u2022 \U0001f4e8 Mandar pro cliente PERGUNTA antes (janela de destino +\n'
             '  texto) e, no SIM, traz o chat pra frente, cola (Ctrl+V) e\n'
             '  aperta Enter sozinho (~2 s \u2014 cursor piscando na caixa).'),
            ("\u23fa\ufe0f  Rotinas do dia",
             '\u2022 \u23fa Gravar / \u23f9 Parar guardam cliques, teclas, rolagens e as\n'
             '  janelas/sites que voc\u00ea acessa; a IA nomeia e descreve cada\n'
             '  grava\u00e7\u00e3o.\n'
             '\u2022 \U0001f9e0 Analisar tudo monta sua ROTINA DO DIA (\U0001f4da); repita N vezes\n'
             '  com \u25b6 \u2014 \u25a0 Parar ou mouse no canto interrompe. Ficam na pasta\n'
             '  rotinas/ ao lado do programa.\n'
             '\u2022 Digitou o NOME de uma rotina na Tarefa e apertou \u25b6? Roda NA\n'
             '  HORA, sem IA \u2014 ex.: "Rotina X 5 vezes" (aceita "5x", "duas\n'
             '  vezes"; sem n\u00famero = 1 vez).'),
            ("\U0001f511  O que \u00e9 necess\u00e1rio",
             '\u2022 Windows 10/11, 64 bits \u2014 sem instalar nada al\u00e9m; ~100 MB.\n'
             '\u2022 Internet \u2014 as capturas v\u00e3o para a API configurada.\n'
             '\u2022 Chave: a padr\u00e3o \u00e9 o Gemini (Google AI Studio), n\u00edvel GRATUITO\n'
             '  (conta Google): https://aistudio.google.com/apikey. Modelo\n'
             '  padr\u00e3o gemini-3.5-flash-lite (o mais r\u00e1pido testado); cada\n'
             '  passo consome tokens. Bateu o limite di\u00e1rio? Espere a cota\n'
             '  renovar ou troque o modelo em \u2699 Configura\u00e7\u00f5es.\n'
             '\u2022 1\u00aa abertura: SmartScreen pode alertar \u2192 Mais informa\u00e7\u00f5es \u2192\n'
             '  Executar assim mesmo. GPU \u00e9 opcional (sem ela o preview usa\n'
             '  o modo mais lento GDI e tudo funciona).'),
            ("\U0001f6e0\ufe0f  Dicas extras",
             '\u2022 Outra API compat\u00edvel com OpenAI (MiMo, OpenRouter, Groq...)?\n'
             '  Edite CUA_BASE_URL e CUA_MODEL no .env.\n'
             '• Chave Groq (grátis, console.groq.com/keys) no ⚙ = Groq\n'
             '  vira o principal (visão e texto) e o Gemini fica de plano B\n'
             '  — se o Groq falhar, quem responde é o Gemini, na hora.\n'
             '\u2022 Leitura mais leve: CUA_WATCH_MODEL=gemini-3.5-flash-lite.\n'
             '\u2022 Detalhes t\u00e9cnicos no README.md do projeto (vers\u00e3o fonte).'),
        ]
        for i, (titulo, corpo) in enumerate(secoes):
            card = ctk.CTkFrame(scroll, corner_radius=10)
            card.grid(row=i, column=0, sticky="ew", pady=(0, 10))
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card, text=titulo, anchor="w",
                font=ctk.CTkFont(size=15, weight="bold"),
                text_color=("#1f7a44", "#7bd88f"),
            ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 2))
            ctk.CTkLabel(
                card, text=corpo, anchor="w", justify="left",
                wraplength=640, font=ctk.CTkFont(size=13),
            ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        ctk.CTkButton(
            win, text="Entendi  \u2713", width=150,
            fg_color="#2f8f4e", hover_color="#37a35a",
            command=win.destroy,
        ).pack(pady=(6, 16))

    def _open_settings(self) -> None:
        win = ctk.CTkToplevel(self)
        win.title("Configurações")
        win.geometry("540x505")
        win.transient(self)
        win.grab_set()

        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(padx=24, pady=18, fill="both", expand=True)
        body.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(body, text="Chave da API (CUA_API_KEY — gratuita)").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        api_entry = ctk.CTkEntry(
            body, show="•", placeholder_text="cole sua chave aqui",
        )
        api_entry.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        api_entry.insert(0, os.getenv("CUA_API_KEY", ""))

        ctk.CTkLabel(
            body,
            text="Chave Groq (CUA_GROQ_API_KEY — opcional, acelera tudo)",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 2))
        groq_entry = ctk.CTkEntry(
            body, show="•",
            placeholder_text="console.groq.com/keys — vazio = só Gemini",
        )
        groq_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        groq_entry.insert(0, os.getenv("CUA_GROQ_API_KEY", ""))

        ctk.CTkLabel(body, text="Modelo (CUA_MODEL)").grid(
            row=4, column=0, sticky="w", pady=(0, 2)
        )
        model_entry = ctk.CTkEntry(body)
        model_entry.grid(row=5, column=0, sticky="ew", padx=(0, 8), pady=(0, 12))
        model_entry.insert(0, os.getenv("CUA_MODEL", Config.model))

        ctk.CTkLabel(body, text="Máx. passos (CUA_MAX_STEPS)").grid(
            row=4, column=1, sticky="w", pady=(0, 2)
        )
        steps_entry = ctk.CTkEntry(body, width=110)
        steps_entry.grid(row=5, column=1, sticky="w", pady=(0, 12))
        steps_entry.insert(0, os.getenv("CUA_MAX_STEPS", str(Config.max_steps)))

        ctk.CTkLabel(body, text="Monitor para ler as tarefas (CUA_MONITOR)").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        monitores = self.screen.list_monitors()
        mon_labels = [m["label"] for m in monitores] or ["Monitor 1 — principal"]
        mon_idx = [m["index"] for m in monitores] or [1]
        mon_menu = ctk.CTkOptionMenu(body, values=mon_labels, width=320)
        mon_menu.grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 12))
        try:
            mon_menu.set(mon_labels[mon_idx.index(self.screen.monitor)])
        except ValueError:
            mon_menu.set(mon_labels[0])

        groq_bruto = os.getenv("CUA_GROQ_API_KEY", "").strip()
        tem_groq = bool(groq_bruto) and not groq_bruto.startswith("COLE_")
        ctk.CTkLabel(
            body,
            text=("Rota ativa: 🟢 Groq (visão + texto) → Gemini plano B"
                  if tem_groq else
                  "Rota: Gemini — cole a chave Groq para ligar o time"),
            text_color=(("#1f7a44", "#7bd88f") if tem_groq else "gray60"),
            font=ctk.CTkFont(size=12), anchor="w",
        ).grid(row=8, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ctk.CTkLabel(
            body, text=f"Salvo em: {env_path()}", text_color="gray60",
            font=ctk.CTkFont(size=12),
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(2, 14))

        def save() -> None:
            updates: dict = {}
            api = api_entry.get().strip()
            groq = groq_entry.get().strip()
            model = model_entry.get().strip()
            steps = steps_entry.get().strip()
            if api:
                updates["CUA_API_KEY"] = api
            if groq:
                updates["CUA_GROQ_API_KEY"] = groq
            if model:
                updates["CUA_MODEL"] = model
            if steps.isdigit() and int(steps) > 0:
                updates["CUA_MAX_STEPS"] = steps
            sel_mon = mon_menu.get()
            if sel_mon in mon_labels:
                updates["CUA_MONITOR"] = str(mon_idx[mon_labels.index(sel_mon)])
            try:
                path = write_env(updates)
            except Exception as exc:
                self._log(f"! erro ao salvar configurações: {exc}")
                return
            os.environ.update(updates)  # efeito imediato nesta sessão
            if "CUA_MONITOR" in updates:
                try:
                    novo = int(updates["CUA_MONITOR"])
                    self.screen.set_monitor(novo)
                    try:
                        self.mon_menu.set(
                            self._mon_labels[self._mon_idx.index(novo)])
                    except ValueError:
                        pass
                    self._log(f"🖥 Agora a IA lê o Monitor {novo}")
                except Exception as exc:
                    self._log(f"! não foi possível trocar o monitor: {exc}")
            self._log(f"✔ Configurações salvas em {path}")
            if updates.get("CUA_GROQ_API_KEY"):
                self._log(
                    "🚀 Time Groq + Gemini ligado — Groq vira o principal "
                    "(visão e texto) e Gemini fica de plano B.")
            win.destroy()

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=10, column=0, columnspan=2, sticky="e")
        ctk.CTkButton(row, text="Salvar", width=120, command=save).pack(
            side="right", padx=(8, 0)
        )
        ctk.CTkButton(
            row, text="Cancelar", width=110, fg_color="gray30",
            hover_color="gray42", command=win.destroy,
        ).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._center(win)
        api_entry.focus_force()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
