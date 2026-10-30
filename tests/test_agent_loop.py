"""Testes do loop do agente com modelo e ações simulados (não mexe no mouse).

Rodar:  .venv/Scripts/python -m pytest tests -q
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cuagent import actions, agent as A, safety  # noqa: E402
from cuagent.config import Config  # noqa: E402


class FakeScreen:
    def model_size(self):
        return (1280, 720)

    def capture_b64(self):
        return "AAAA"


def make_agent(replies, *, approver=None, stop=None, max_steps=10, logs=None):
    cfg = Config(api_key="x", max_steps=max_steps)
    ag = A.ComputerAgent(
        cfg, FakeScreen(),
        approver=approver or (lambda a: "ok"),
        on_log=(logs.append if logs is not None else (lambda m: None)),
        should_stop=stop or (lambda: False),
    )
    ag.messages = [{"role": "system", "content": "s"}]   # pula o inventário
    it = iter(replies)

    def fake_chat(client, model, esforco, messages=None):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r()
        return r

    ag._chat = fake_chat
    return ag


@pytest.fixture
def executed(monkeypatch):
    done = []

    def fake_execute(action, screen, should_stop=None):
        done.append(action)
        return [], f"{action['action']} ({action.get('coordinate', ['-', '-'])[0]}, " \
                   f"{action.get('coordinate', ['-', '-'])[1]}) | cursor em (1, 1)"

    monkeypatch.setattr(actions, "execute", fake_execute)
    monkeypatch.setattr(A.ComputerAgent, "_pause", lambda self, s: None)
    A._SESSION.update(good=None, dead=set(), busy_until={})
    return done


def test_texto_final_encerra(executed):
    ag = make_agent(['{"action":"left_click","coordinate":[500,500]}', "Pronto."])
    assert ag.run("t") == "Tarefa concluída."
    assert len(executed) == 1


def test_coordenada_normalizada_vira_pixel(executed):
    ag = make_agent(['{"action":"left_click","coordinate":[500,250]}', "ok"])
    ag.run("t")
    assert executed[0]["coordinate"] == [640, 180]      # 1280x720


def test_coordenada_fora_da_faixa_e_limitada(executed):
    ag = make_agent(['{"action":"left_click","coordinate":[1500,-20]}', "ok"])
    ag.run("t")
    assert executed[0]["coordinate"] == [1279, 0]


def test_feedback_em_escala_do_modelo(executed):
    ag = make_agent(['{"action":"left_click","coordinate":[500,250]}', "ok"])
    ag.run("t")
    textos = [b.get("text", "") for m in ag.messages if m["role"] == "user"
              for b in (m["content"] if isinstance(m["content"], list) else [])]
    fb = [t for t in textos if t.startswith("Executado")][0]
    assert "(500, 250)" in fb and "cursor" not in fb


def test_lote_executa_todas(executed):
    ag = make_agent(['{"actions":[{"action":"left_click","coordinate":[1,1]},'
                     '{"action":"type","text":"oi"}]}', "fim"])
    ag.run("t")
    assert [a["action"] for a in executed] == ["left_click", "type"]


def test_finish_json_encerra(executed):
    ag = make_agent(['{"action":"done","text":"feito"}'])
    assert ag.run("t") == "Tarefa concluída."
    assert executed == []


def test_json_invalido_pede_de_novo_e_desiste(executed):
    ag = make_agent(["{quebrado"] * 10)
    msg = ag.run("t")
    assert "inválidas" in msg


def test_json_invalido_depois_recupera(executed):
    ag = make_agent(["{quebrado", '{"action":"key","text":"enter"}', "ok"])
    assert ag.run("t") == "Tarefa concluída."
    assert executed[0]["action"] == "key"


def test_limite_de_passos(executed):
    ag = make_agent(['{"action":"wait","duration":0}'] * 5, max_steps=3)
    assert "limite" in ag.run("t")


def test_pular_nao_encerra_tarefa(executed):
    decisoes = iter(["skip", "ok"])
    ag = make_agent(['{"action":"left_click","coordinate":[1,1]}',
                     '{"action":"left_click","coordinate":[2,2]}', "fim"],
                    approver=lambda a: next(decisoes))
    assert ag.run("t") == "Tarefa concluída."
    assert len(executed) == 1


def test_encerrar_no_dialogo_aborta(executed):
    def approver(a):
        raise safety.Abort("Execução encerrada pelo usuário.")
    ag = make_agent(['{"action":"left_click","coordinate":[1,1]}'], approver=approver)
    with pytest.raises(safety.Abort):
        ag.run("t")


def test_parar_durante_chamada_lenta_da_api(executed):
    stop = threading.Event()

    def lenta():
        time.sleep(5)
        return "fim"

    ag = make_agent([lenta], stop=stop.is_set)
    threading.Timer(0.3, stop.set).start()
    t0 = time.monotonic()
    with pytest.raises(safety.Abort):
        ag.run("t")
    assert time.monotonic() - t0 < 1.5


def test_cota_do_dia_pula_modelo_sem_esperar(executed):
    logs: list[str] = []
    cota = RuntimeError("429 quotaId GenerateRequestsPerDayPerProjectPerModel")
    ag = make_agent([cota, "fim"], logs=logs)
    t0 = time.monotonic()
    assert ag.run("t") == "Tarefa concluída."
    assert time.monotonic() - t0 < 1
    assert any("cota" in m for m in logs)
    assert ag.cfg.model in A._SESSION["dead"]


def test_503_troca_de_modelo_sem_insistir(executed):
    logs: list[str] = []
    busy = RuntimeError("Error code: 503 high demand")
    ag = make_agent([busy, "fim"], logs=logs)
    assert ag.run("t") == "Tarefa concluída."
    trocas = [m for m in logs if m.startswith("↪")]
    assert len(trocas) == 1 and "ocupado" in trocas[0]


def test_lembra_modelo_bom_entre_tarefas(executed):
    usados = []
    busy = RuntimeError("Error code: 503 high demand")

    def chat_factory(respostas):
        it = iter(respostas)

        def chat(client, model, esforco, messages=None):
            usados.append(model)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        return chat

    ag1 = make_agent([])
    ag1._chat = chat_factory([busy, "fim"])
    ag1.run("t1")
    ag2 = make_agent([])                      # tarefa nova, mesma sessão
    ag2._chat = chat_factory(["fim"])
    ag2.run("t2")
    assert usados[-1] == usados[1]            # começou direto pelo que funcionou


def test_erro_permanente_nao_fica_tentando(executed):
    ag = make_agent([RuntimeError("401 invalid api key")])
    with pytest.raises(RuntimeError):
        ag.run("t")


def test_think_do_qwen_e_ignorado():
    txt = A._THINK_RE.sub("", '<think>vou {clicar}</think>{"action":"left_click","coordinate":[1,2]}')
    acts, _ = A.extract_actions(txt)
    assert acts[0]["coordinate"] == [1, 2]


def test_historico_limitado():
    ag = make_agent([])
    ag.messages += [{"role": "user" if i % 2 == 0 else "assistant", "content": "x"}
                    for i in range(60)]
    ag._trim_history()
    assert len(ag.messages) <= A._MAX_HISTORY + 1
    assert ag.messages[1]["role"] == "user"


@pytest.mark.parametrize("combo,esperado", [
    ("Enter", ["enter"]), ("ctrl+S", ["ctrl", "s"]), ("ArrowDown", ["down"]),
    ("Page_Down", ["pagedown"]), ("Escape", ["esc"]), ("win", ["win"]),
    ("ctrl+shift+Escape", ["ctrl", "shift", "esc"]), ("+", ["+"]),
])
def test_teclas_validas(combo, esperado):
    assert actions._keys(combo) == esperado


def test_tecla_invalida_vira_erro():
    with pytest.raises(ValueError):
        actions._keys("teclamagica")


def test_done_encerra_sem_nova_chamada(executed):
    chamadas = []

    def resposta():
        chamadas.append(1)
        return '{"action":"left_click","coordinate":[21,348],"done":true,"observation":"Gallery aberta"}'

    ag = make_agent([resposta, resposta])
    assert ag.run("entra na galeria") == "Tarefa concluída."
    assert len(chamadas) == 1 and len(executed) == 1


def test_done_no_lote(executed):
    ag = make_agent(['{"actions":[{"action":"left_click","coordinate":[1,1]},'
                     '{"action":"type","text":"oi"}],"done":true}'])
    assert ag.run("t") == "Tarefa concluída."
    assert len(executed) == 2


def test_done_com_erro_nao_encerra(executed, monkeypatch):
    def falha(action, screen, should_stop=None):
        raise RuntimeError("boom")
    monkeypatch.setattr(actions, "execute", falha)
    ag = make_agent(['{"action":"left_click","coordinate":[1,1],"done":true}', "desisti"])
    assert ag.run("t") == "Tarefa concluída."   # só termina pela 2ª resposta
