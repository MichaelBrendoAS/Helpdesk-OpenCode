"""cuagent — motor do Helpdesk-OpenCode (Computer-Using Agent em Python, API compatível com OpenAI)."""

__version__ = "0.1.0"


def _dpi_per_monitor() -> None:
    """Per-monitor DPI v2 ANTES de qualquer outra biblioteca.

    O Windows só aceita definir o modo de DPI uma vez por processo. No .exe
    (PyInstaller) o pyautogui/Tk chegavam antes com o modo "system aware" e
    um monitor com escala 125% passava a ser visto reduzido (1920x1080 ->
    1536x864), com cliques e capturas na escala errada.
    """
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


_dpi_per_monitor()
