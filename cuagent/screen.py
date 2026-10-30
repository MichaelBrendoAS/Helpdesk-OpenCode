"""Captura de tela e conversão de coordenadas.

O modelo vê a captura reduzida (para economizar tokens) e recebe as dimensões
do "mundo do modelo" via `display_width_px`/`display_height_px`. Todas as
coordenadas que chegam do modelo estão nesse espaço e são convertidas para a
resolução nativa antes de chamar o PyAutoGUI.
"""

from __future__ import annotations

import base64
import io
import os
import re
import sys
import threading

import mss
from PIL import Image

try:
    import dxcam  # captura DXGI na GPU — caminho rápido do preview
except Exception:  # pragma: no cover
    dxcam = None

# linha do dxcam: "Device[0] Output[1]: Res:(1920, 1080) Rot:0 Primary:False"
_DX_OUT_RE = re.compile(
    r"Output\[(\d+)\]:\s*Res:\((\d+),\s*(\d+)\).*?Primary:(\w+)"
)


def _env_monitor() -> int:
    """Monitor do CUA_MONITOR (.env): 1 = principal; inválido => 1."""
    try:
        return int(str(os.getenv("CUA_MONITOR", "1")).strip())
    except ValueError:
        return 1


def enable_dpi_awareness() -> None:
    """No Windows, alinha a captura de tela (física) e o mouse (lógica).

    Sem isso, em telas com escala de DPI (125%, 150%...) os cliques saem
    deslocados porque o mss captura pixels físicos e o PyAutoGUI usa pixels
    lógicos.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _reduce_bgra(data, size: tuple[int, int], max_width: int, max_height: int) -> Image.Image:
    """Reduz um buffer BGRA para caber em max_width x max_height e devolve RGB.

    Bancada: com um ndarray (dxcam) pular pixels com passo inteiro ANTES de
    decodificar custa 6,5 ms contra 12,4 ms decodificando tudo e reduzindo
    depois — o passo (ex.: [::3, ::3]) já deixa a imagem perto do alvo e o
    BOX final só refina.
    """
    width, height = size
    scale = min(max_width / width, max_height / height, 1.0)
    small = (max(1, round(width * scale)), max(1, round(height * scale)))

    # ndarray (dxcam): redução barata por amostragem de passo
    if hasattr(data, "shape"):
        step = max(1, width // small[0])
        if step > 1:
            sub = data[::step, ::step]
            sh, sw = sub.shape[:2]
            img = Image.frombytes("RGB", (sw, sh), bytes(sub), "raw", "BGRX")
            if img.size != small:
                img = img.resize(small, Image.BOX)
            return img

    # bytes (mss) ou passo 1: converte tudo e reduz com BOX
    rgb = _bgra_to_rgb(data, size)
    if rgb.size != small:
        rgb = rgb.resize(small, Image.BOX)
    return rgb


def _bgra_to_rgb(data, size: tuple[int, int]) -> Image.Image:
    """BGRA -> RGB em resolução cheia (cópia — caminho do agente)."""
    try:
        return Image.frombytes("RGB", size, data, "raw", "BGRX")
    except (TypeError, ValueError):
        return Image.frombytes("RGB", size, bytes(data), "raw", "BGRX")


class Screen:
    def __init__(self, max_width: int = 0, monitor: int | None = None):
        enable_dpi_awareness()
        # 0 = resolução nativa do monitor (a IA vê exatamente o que você vê)
        self.max_width = int(max_width or 0)
        self._lock = threading.Lock()  # o mss não é seguro entre threads
        self._sct = mss.mss()
        # monitor mss: 1 = principal, 2 = segundo... (vem do CUA_MONITOR)
        self.monitor = self._resolve_monitor(
            monitor if monitor is not None else _env_monitor()
        )
        self._cam = None        # câmera DXGI (dxcam), criada sob demanda
        self._cam_failed = False
        self._cam_closed = False

    # ---------- dimensões ----------

    def native_size(self) -> tuple[int, int]:
        _, _, width, height = self.monitor_rect()
        return width, height

    def model_size(self) -> tuple[int, int]:
        """Dimensões da imagem enviada ao modelo (largura máxima limitada)."""
        width, height = self.native_size()
        if self.max_width <= 0 or width <= self.max_width:
            return width, height
        scale = self.max_width / width
        return self.max_width, max(1, round(height * scale))

    def monitor_rect(self) -> tuple[int, int, int, int]:
        """(left, top, largura, altura) do monitor escolhido no espaço virtual.

        left/top não são zero em monitores que não são o principal — é daí
        que sai e entra o offset de todas as coordenadas de mouse.
        """
        monitors = self._sct.monitors
        if self.monitor >= len(monitors):   # monitor desconectou no meio do uso
            self.monitor = 1
        mon = monitors[self.monitor]
        return (int(mon["left"]), int(mon["top"]),
                int(mon["width"]), int(mon["height"]))

    def _resolve_monitor(self, monitor) -> int:
        """Valida o índice (1..N); qualquer coisa estranha => principal."""
        try:
            idx = int(monitor)
        except (TypeError, ValueError):
            return 1
        if idx < 1 or idx >= len(self._sct.monitors):
            return 1
        return idx

    def list_monitors(self) -> list[dict]:
        """Monitores conectados, com rótulo pronto para o seletor da GUI."""
        out: list[dict] = []
        devices = self._sct.monitors[1:]    # [0] = todos juntos (mss)
        has_primary = any(m.get("is_primary") for m in devices)
        for i, mon in enumerate(devices, start=1):
            w, h = int(mon["width"]), int(mon["height"])
            principal = bool(mon.get("is_primary")) or (
                not has_primary and i == 1
            )
            out.append({
                "index": i,
                "width": w,
                "height": h,
                "primary": principal,
                "label": f"Monitor {i} — {w}x{h}"
                         + (" (principal)" if principal else ""),
            })
        return out

    def set_monitor(self, monitor: int) -> None:
        """Troca o monitor lido/agido e derruba a câmera DXGI antiga."""
        try:
            idx = int(monitor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"monitor inválido: {monitor!r}") from exc
        total = len(self._sct.monitors)
        if idx < 1 or idx >= total:
            raise ValueError(
                f"monitor {idx} inexistente — {total - 1} conectado(s)"
            )
        if idx == self.monitor:
            return
        self._drop_camera()
        self._cam_failed = False   # nova saída DXGI merece nova chance
        self.monitor = idx

    # ---------- conversão de coordenadas ----------

    def to_model(self, x: float, y: float) -> tuple[int, int]:
        left, top, nw, nh = self.monitor_rect()
        mw, mh = self.model_size()
        return round((x - left) * mw / nw), round((y - top) * mh / nh)

    def from_model(self, x: float, y: float) -> tuple[int, int]:
        left, top, nw, nh = self.monitor_rect()
        mw, mh = self.model_size()
        return round(left + x * nw / mw), round(top + y * nh / mh)

    # ---------- captura ----------

    # ------------------------------------------------------ câmera DXGI (GPU)

    def _camera(self):
        """Câmera DXGI via dxcam; None quando indisponível (cai no GDI)."""
        if dxcam is None or self._cam_failed or self._cam_closed:
            return None
        if self._cam is None:
            if self.monitor >= len(self._sct.monitors):
                self.monitor = 1
            mon = self._sct.monitors[self.monitor]
            try:
                cam = dxcam.create(
                    output_idx=self._dxgi_output(mon),
                    output_color="BGRA",         # sem conversão de cor no dxcam
                    processor_backend="numpy",   # não exige OpenCV
                )
                # SEM cam.start(): a thread de captura contínua (30 fps) copiava
                # a tela o tempo todo mesmo com a prévia a 1 quadro/s. Agora
                # captura só quando pedem (grab sob demanda).
                self._cam = cam
            except Exception:
                self._cam_failed = True
        return self._cam

    def _dxgi_output(self, mon: dict) -> int:
        """Acha a saída DXGI do monitor escolhido (resolução + principal).

        A ordem das saídas da plca não é garantida — casar por resolução e
        bandeira de principal evita ler o monitor errado; se nada casar,
        cai na posição tradicional (monitor - 1), e a guarda de tamanho do
        quadro (capture/capture_preview) ainda protege.
        """
        prefer = max(0, self.monitor - 1)
        if dxcam is None:
            return prefer
        try:
            info = str(dxcam.output_info())
        except Exception:
            return prefer
        outs: dict[int, tuple[int, int, bool]] = {}
        for line in info.splitlines():
            m = _DX_OUT_RE.search(line)
            if m:
                outs[int(m.group(1))] = (
                    int(m.group(2)), int(m.group(3)), m.group(4) == "True",
                )
        if not outs:
            return prefer
        want = (int(mon["width"]), int(mon["height"]))
        want_primary = bool(mon.get("is_primary", self.monitor == 1))

        def hits(candidate: tuple[int, int, bool]) -> bool:
            return candidate[:2] == want and candidate[2] == want_primary

        if prefer in outs and hits(outs[prefer]):
            return prefer
        for idx in sorted(outs):
            if hits(outs[idx]):
                return idx
        for idx in sorted(outs):
            if outs[idx][:2] == want:
                return idx
        return prefer

    @staticmethod
    def _grab(cam):
        """Quadro sob demanda (mesmo se a tela não mudou desde o último)."""
        try:
            return cam.grab(copy=True, new_frame_only=False)
        except TypeError:             # dxcam antigo sem new_frame_only
            return cam.grab()

    def _drop_camera(self) -> None:
        """Para e libera a câmera DXGI atual (sem marcar a tela como fechada)."""
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass
            try:
                cam.release()
            except Exception:
                pass

    def close(self) -> None:
        """Libera a câmera DXGI (chamar ao fechar a interface)."""
        self._cam_closed = True
        self._drop_camera()

    # ---------------------------------------------------------------- captura

    def capture(self) -> Image.Image:
        """Captura cheia na resolução que o modelo vai ver (caminho do agente)."""
        native = None
        cam = self._camera()
        if cam is not None:
            try:
                frame = self._grab(cam)  # cópia estável, sem "tear"
                if frame is not None:
                    height, width = frame.shape[:2]
                    if (width, height) == self.native_size():
                        native = _bgra_to_rgb(frame, (width, height))
                    else:
                        # saída errada da plca: corta a câmera e usa o GDI
                        self._cam_failed = True
                        self._drop_camera()
            except Exception:
                native = None
        if native is None:
            with self._lock:  # GUI e agente capturam em threads diferentes
                mon = self._sct.monitors[self.monitor]
                raw = self._sct.grab(mon)
            native = _bgra_to_rgb(raw.bgra, raw.size)

        size = self.model_size()
        if native.size != size:
            # BILINEAR: ~3x mais rápido que LANCZOS, diferença invisível
            # nesta redução mínima (1680 -> 1600)
            native = native.resize(size, Image.BILINEAR)
        return native

    def capture_preview(self, max_size: tuple[int, int]) -> Image.Image:
        """Caminho rápido (GPU): reduz com passo inteiro antes de decodificar."""
        cam = self._camera()
        if cam is not None:
            try:
                frame = self._grab(cam)
                if frame is not None:
                    height, width = frame.shape[:2]
                    if (width, height) == self.native_size():
                        return _reduce_bgra(
                            frame, (width, height), max_size[0], max_size[1]
                        )
                    self._cam_failed = True   # saída errada: usa o GDI
                    self._drop_camera()
            except Exception:
                pass
        # fallback GDI (mss): funciona sempre, porém mais lento (~15-30 fps)
        with self._lock:
            mon = self._sct.monitors[self.monitor]
            raw = self._sct.grab(mon)
        return _reduce_bgra(raw.bgra, raw.size, max_size[0], max_size[1])

    def capture_b64(self) -> str:
        """Tela cheia em base64 JPEG (a API aceita media_type image/jpeg).

        Bancada: PNG de 1600x1000 ~128 ms; JPEG qualidade 85 ~25 ms —
        segura bem menos a GIL enquanto o loop do agente roda, então o
        preview da GUI não sofre concomitância.
        """
        buffer = io.BytesIO()
        self.capture().save(buffer, format="JPEG", quality=75)
        return base64.b64encode(buffer.getvalue()).decode("ascii")
