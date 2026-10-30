# 🖥️ Helpdesk-OpenCode — Computer-Using Agent (Python + API gratuita)

Agente de IA que **controla o seu computador de verdade** — captura a tela, decide o
próximo passo com uma IA e executa cliques, digitação e atalhos de teclado até a
tarefa terminar.

Este é o **projeto duplicado do Helpdesk**, adaptado para usar uma **API gratuita e
compatível com OpenAI** (padrão: nível gratuito do Gemini / Google AI Studio) em vez
da API da Anthropic. O projeto original (`Default Project`) continua intacto, com o
Claude.

Camada de segurança local: confirmação de ações, failsafe do mouse e limite de passos.

## Arquitetura

```
┌────────────┐   screenshot    ┌──────────────────────────┐
│  mss       │ ──────────────► │                          │
│ (captura)  │                 │  Modelo (API OpenAI-     │   JSON: left_click, ...
└────────────┘                 │  compatível: Gemini,     │ ─────────────────────┐
       ▲                       │  MiMo, OpenRouter...)    │                      │
       │        feedback       │                          │                      ▼
       │  ◄────────────────────┤   (responde JSON de ação)├──────────────┐ ┌────────────────────┐
       │                       └──────────────────────────┘              │ │  safety.py         │
       ▼                                                                 │ │  (confirmação)     │
┌────────────┐                                                           └─│        │           │
│ nova tela  │◄──────── resultado ◄──────────────────────────────────────────▼────────▼──────────┐
└────────────┘                                                              │  actions.py        │
                                                                           │  (PyAutoGUI)       │
                                                                           └────────────────────┘
```

**Loop:** tela → modelo devolve **um objeto JSON** com a ação → confirmação →
execução → nova tela → repete até o modelo responder **sem JSON** (tarefa concluída).

O protocolo JSON (em vez das ferramentas nativas de cada provedor) faz o mesmo
código funcionar com **qualquer** API compatível com OpenAI que tenha visão.

## Estrutura

| Arquivo | Papel |
|---|---|
| `main.py` | CLI: parse de argumentos, modo interativo, tratamento de erros |
| `main_gui.py` | Interface gráfica (CustomTkinter): preview da tela, log, diálogos |
| `build.bat` | Gera o executável Windows com PyInstaller |
| `cuagent/agent.py` | Loop de conversa com a API (JSON de ação) e montagem do histórico |
| `cuagent/actions.py` | Traduz a ação JSON para PyAutoGUI (mouse/teclado/tela) |
| `cuagent/screen.py` | Captura de tela, escala de imagem e conversão de coordenadas |
| `cuagent/safety.py` | Descreve/confirma cada ação antes de executar |
| `cuagent/watcher.py` | 💡 Assistente: leitura periódica da tela (somente sugestão) |
| `cuagent/recorder.py` | ⏺ Gravação de rotinas: hooks globais (ctypes) + pasta `rotinas/` |
| `cuagent/player.py` | ▶ Repetição de rotinas (escala de tela, foco de janela, N vezes) |
| `cuagent/rotinas_ai.py` | 🧠 IA nomeia gravações e monta a Rotina do Dia |
| `cuagent/config.py` | Configuração via variáveis de ambiente |

## Instalação

Requer Python 3.10+.

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows (no Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
```

## Configuração (chave gratuita)

```bash
copy .env.example .env        # Windows (no Linux/macOS: cp .env.example .env)
```

Edite `.env` e coloque sua chave em `CUA_API_KEY`:

* **Padrão (gratuito):** crie a chave em <https://aistudio.google.com/apikey>
  (conta Google, sem cartão). Modelo padrão: `gemini-3.5-flash-lite`.
* **Outra API compatível com OpenAI** — troque `CUA_BASE_URL` e `CUA_MODEL`:

  | Provedor | `CUA_BASE_URL` | `CUA_MODEL` |
  |---|---|---|
  | Gemini (padrão) | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-3.5-flash-lite` |
  | MiMo (Xiaomi) | `https://api.xiaomimimo.com/v1` | `mimo-v2.6-flash` |
  | OpenRouter | `https://openrouter.ai/api/v1` | `xiaomi/mimo-v2.6-flash` |
  | Groq | `https://api.groq.com/openai/v1` | (ver docs do Groq) |

  **Importante:** o modelo precisa aceitar **imagem** (visão) — as capturas de tela
  são a "visão" do agente.

## Uso

```bash
# Uma tarefa e encerra (pede confirmação para cada ação)
python main.py "abra o navegador e pesquise por clima em São Paulo"

# Modo interativo: várias tarefas compartilhando o mesmo contexto
python main.py

# Execução automática SEM confirmação (cuidado!)
python main.py -y "abra o Bloco de notas e escreva uma lista de compras"

# Ajustes pontuais
python main.py --model gemini-3.5-flash-lite --max-steps 50 "sua tarefa"
```

### Confirmação de ações

Por padrão cada ação de escrita/clique pergunta antes de rodar:

```
? digitar "Responda o formulário"
  [Enter] executar · [p] pular · [q] encerrar >
```

* **Enter** — executa · **p** — pula a ação · **q** — encerra a sessão

### Parada de emergência

* **Ctrl+C** no terminal — interrompe na hora.
* **Mouse no canto superior esquerdo** — o failsafe do PyAutoGUI aborta a ação.

## 🎨 Interface gráfica (GUI)

Além do terminal, o projeto tem uma interface gráfica:

```bash
python main_gui.py
```

Ela reutiliza exatamente o mesmo agente do CLI, mas com:

* **Preview da tela** — 1 quadro/s quando parado e **ao vivo, a cada
  ação**, durante a execução (sem queimar CPU à toa);
* **Log de atividade** — cada passo, ação e resposta do modelo em tempo real;
* **Diálogo de confirmação** — cada ação pede *Executar / Pular / Encerrar*;
* **Botões ▶ Executar e ■ Parar** — a parada acontece após o passo atual;
* **Switch "Executar sem confirmar"** — modo automático (igual ao `-y`);
* **🧠 Identificação da tela** — a cada passo a IA anota no log o que ela
  identifica (janela, botão, visor), usa o aplicativo que você citou e só
  declara sucesso quando vê o resultado na imagem.
* **Anti-duplicação** — se o agente tenta abrir o mesmo programa 2x seguidas, ele
  só espera a janela e mostra a tela nova (nunca Calculadora em dobro).
* **🚀 Team de APIs (Groq + Gemini)** — com a chave Groq opcional
  (grátis, console.groq.com/keys), screenshots e texto das rotinas vão
  para o **Groq** (o mais rápido); se falhar, o **Gemini** responde na
  hora (plano B). Sem chave Groq, tudo segue só no Gemini como sempre.
* **🖥 Multimonitor** — escolha pelo seletor **🖥 Monitor no topo da**
  janela (ou em **⚙ Configurações**) qual monitor a
  IA lê e clica (`CUA_MONITOR`); a janela do programa lembra a posição
  entre monitores (`CUA_WIN_GEOMETRY`) e as coordenadas de mouse são
  globais no Windows (funcionam com o monitor em qualquer posição);
* **Aba ⏺ Rotinas** — grave seus movimentos (⏺ Gravar / ⏹ Parar), a
  IA nomeia cada gravação e o **🧠 Analisar tudo** cria a **📚 Rotina
  do Dia**; repita N vezes com **▶** (■ Parar ou mouse no canto aborta);
* **⚙ Configurações** — chave da API, modelo, máximo de passos e o
  **monitor que a IA lê** (multimonitor), salvos direto no `.env`
  (sem editar arquivo na mão);
* **❓ Como usar** — manual embutido (passo a passo + requisitos), sempre
  disponível dentro do programa;
* Atalho **Ctrl+Enter** executa a tarefa; **Enter** quebra linha.

## ▶️ Como usar o aplicativo (versão .exe)

Passo a passo para rodar em qualquer Windows, sem instalar nada:

1. **Copie** `dist\Helpdesk-OpenCode\Helpdesk-OpenCode.exe` para a máquina de
   destino — ou leve a pasta `dist\Helpdesk-OpenCode` inteira.
2. **Abra com dois cliques.** Na primeira vez o SmartScreen pode aparecer
   dizendo "Windows protegeu o computador": clique em *Mais informações →
   Executar assim mesmo*.
3. **⚙ Configurações** → cole a chave gratuita (de
   <https://aistudio.google.com/apikey>) → **Salvar**. A chave fica gravada
   em um `.env` ao lado do .exe.
4. **Digite a tarefa** em português no campo *Tarefa* (ex.: "abra o
   navegador e pesquise por clima em São Paulo") e clique em **▶ Executar**
   — ou pressione **Ctrl+Enter**.
5. **Aprove cada ação** no diálogo (*Executar / Pular / Encerrar*) — ou
   ative o switch *Executar sem confirmar cada ação* para rodar tudo
   sozinho.
6. Acompanhe o **preview** ("O que o agente vê": vivo durante a tarefa) e o log
   **Atividade**, com passos, respostas e erros em tempo real.
   **■ Parar** encerra após o passo atual; **✕** fecha (pede confirmação
   se houver tarefa rodando).

O mesmo manual está no botão **❓ Como usar** dentro da interface e no
arquivo `INSTRUCOES.txt` — leve-o junto com o .exe se quiser.

## 💡 Assistente em tempo real (lê a tela enquanto você trabalha)

Enquanto você conversa com um cliente, o **💡 Assistente** observa a sua
tela e já entrega, na aba *💡 Assistente* (painel direito):

* **CONTEXTO** — quem é o cliente, o que ele quer e o que está em aberto;
* **RESPOSTA PRONTA** — texto pronto para enviar: **📋 Copiar resposta**
  (cole no chat com Ctrl+V) ou **📨 Mandar pro cliente…** — ele
  **pergunta** (mostra a janela de destino e um trecho do texto) e, no
  **Mandar agora**, traz o chat pra frente, cola a resposta (Ctrl+V,
  acentos ok) e aperta **Enter** sozinho (~2 s, tudo local — sem IA,
  sem cota). Deixe o cursor piscando na caixa de mensagem antes; se o
  seu chat não envia com Enter, o texto fica colado — é só clicar em
  Enviar;
* **PRÓXIMO PASSO** — qual programa/aba abrir e o que fazer (botão
  **↗ Usar como tarefa** joga a instrução no campo *Tarefa* para o agente
  executar).

**Como usar:** o assistente **inicia desligado** — nada roda sozinho;
aba *💡 Assistente* → ligue **Ler a tela e sugerir** → escolha o
intervalo (5 s a 60 s; padrão 15 s, salvo no `.env` como
`CUA_WATCH_INTERVAL`). **Enquanto a tela não muda ele não consulta a
IA**: o status gira a cada intervalo mostrando *tela sem mudanças*
(zero tokens, zero chamadas) e, quando algo muda, a imagem vai
**reduzida a 1280 px** (menos tokens e menos fila). Ele mantém uma
**memória** entre leituras (não perde
o fio da conversa), **pausa sozinho** enquanto uma tarefa executa e se
**desliga na hora com chave/modelo errado** (401/404) e **só após 10 falhas
seguidas de fila** — erro transitório (fila do Google, 503) ele segue
tentando sozinho. O status de cada leitura mostra a duração real (ex.:
*✔ Leitura 14:02 (38 s)*): o intervalo escolhido (5 s…) é o tempo **mínimo**
entre leituras — some a fila da API.

**Custo:** na API gratuita cada leitura consome um pouco da cota diária —
use intervalos maiores (30 s / 60 s) para gastar menos. Dá para usar um
modelo mais leve nas leituras com `CUA_WATCH_MODEL=gemini-3.5-flash-lite`
no `.env`.

## VELOCIDADE E PRECISÃO

O programa foi calibrado para executar rápido e com timing exato:

- **Esforço mínimo de raciocínio:** toda chamada envia
  `reasoning_effort=minimal` (bench: ~2x mais rápido no Gemini free); se a
  API não aceitar o parâmetro, o agente repete sozinho sem ele (1 aviso no
  log e pronto).
- **Rotina pelo chat sem IA:** citar o **nome da rotina** no campo *Tarefa*
  (ex.: `Rotina X 5 vezes`) executa o replay **local** — zero chamadas.
- **Conexão reutilizada:** o cliente HTTP compartilha TLS entre tarefas e o
  inventário usa cache — o começo de cada tarefa não paga tudo de novo.
- **Fila do free tier:** a latência varia com a fila do Gemini (de ~2 s a
  dezenas de segundos nos picos) — por isso agrupar ações (abaixo) encurta
  de verdade o tempo total.
- **Lote de ações:** o agente pode agrupar até 3 ações numa única chamada
  de API (ex.: clicar no campo + já digitar) — menos esperas, tarefas mais
  rápidas. Quando não dá para prever, ele continua agindo 1 ação por vez.
- **Ações locais instantâneas:** pausa do PyAutoGUI reduzida (0,03 s) e
  cliques direto na coordenada, sem movimento animado prévio.
- **Replay milimétrico:** timer de 1 ms no Windows durante a repetição,
  espera de 10 ms e `PAUSE=0` — o ritmo é 100% o do timing gravado.
- **Preview pausado durante tarefa/repetição:** a CPU fica livre para a
  IA; o painel atualiza a cada ação executada.

## ⏺ Rotinas gravadas (gravar → a IA aprende → repetir)

**Rode pelo chat:** escreva o **nome da rotina** no campo *Tarefa*
(ex.: `Rotina X 5 vezes`) e clique **▶ Executar** — ela repete na hora,
sem nenhuma chamada de IA (aceita `5x`, `duas vezes`; sem número = 1
vez; serve para combos 📚 também).

A aba **⏺ Rotinas** grava o que você faz no computador e deixa a IA
aprender com isso:

* **⏺ Gravar / ⏹ Parar** — captura movimentos do mouse, cliques,
  rolagem, teclas e a **janela ativa** (executável + título — inclusive
  o site pesquisado no navegador), com o tempo real de cada evento.
  Os cliques no próprio painel do programa são ignorados (o ⏹ não
  contamina a gravação);
* **💾 Salva sozinho** na pasta `rotinas/` ao lado do programa (JSON);
  em seguida a IA (Gemini) **nomeia e descreve** a gravação;
* **🧠 Analisar tudo com IA** — lê todas as gravações (janelas
  acessadas, sequência de teclas, contagens) e cria a **📚 ROTINA DO
  DIA**: a sequência de rotinas do seu dia a dia com quantas vezes cada
  uma repete, mais insights ("você abre o Chrome toda manhã…");
* **▶ Repetir** — escolha quantas vezes (1 a 999) e clique ▶: o replay
  respeita o ritmo gravado, escala as coordenadas se a resolução mudou,
  foca as janelas certas e solta teclas/botões ao parar;
* **Parada de emergência** — **■ Parar repetição** ou **mouse no canto
  superior esquerdo** (failsafe) interrompem na hora.

## ✅ O que é necessário para executar

| Necessário | Detalhe |
|---|---|
| Sistema | Windows 10/11 **64 bits** — não precisa de Python nem de instalação |
| Internet | Obrigatória: as capturas de tela vão para a API de IA |
| Chave de API | **Gratuita** — Gemini/AI Studio (<https://aistudio.google.com/apikey>) |
| Disco | ~100 MB livres |
| GPU | Opcional — sem DXGI o preview usa o modo GDI (mais lento, funciona) |
| Permissões | Não precisa ser administrador |

**Cota gratuita:** o Gemini free tier tem limite diário de requisições e
tokens. Testes normais ficam dentro da cota; ao bater o limite (erro 429),
espere renovar ou troque o modelo. Cada passo do agente também consome
tokens — limite em **⚙ Configurações → Máx. passos**.

## 📦 Gerando o executável (Windows)

Dê **dois cliques em `build.bat`**. Ele instala as dependências e roda o
PyInstaller. Saída:

```
dist\Helpdesk-OpenCode\Helpdesk-OpenCode.exe   (pasta única — build.bat)
```

Para gerar também o **arquivo único** (mais prático para levar a outras
máquinas):

```bash
python -m PyInstaller --noconfirm --clean --windowed --onefile ^
    --name Helpdesk-OpenCode --collect-all customtkinter ^
    --collect-all dxcam --hidden-import mss.windows main_gui.py
```

* A máquina de destino **não precisa ter Python** — basta copiar o .exe
  (ou a pasta) para onde quiser.
* O executável procura a chave em um `.env` **ao lado do .exe**; ou use o
  botão ⚙ **Configurações** da própria interface (ao salvar, o `.env` é
  criado automaticamente ali).
* O arquivo único abre um pouco mais devagar na primeira vez (extrai os
  arquivos para uma pasta temporária); o formato pasta abre mais rápido.
* Ícone próprio: acrescente `--icon=app.ico` ao comando do PyInstaller.

## Como funciona por dentro

1. A tela é capturada (`mss`), redimensionada para no máximo
   `CUA_MAX_SCREENSHOT_WIDTH` px e enviada em base64 como `image_url`.
2. A resolução da imagem é informada na mensagem de tarefa — as
   coordenadas do modelo são convertidas de volta para a resolução nativa
   (`Screen.from_model`).
3. O modelo responde **somente com um objeto JSON**
   (`{"action": "left_click", "coordinate": [x, y]}`), que passa por
   `safety.approve` e depois por `actions.execute`.
4. O feedback ("Executado: ...") e a **nova captura** voltam como mensagem
   do usuário e o loop continua. Só a imagem mais recente fica no
   histórico (as antigas viram texto) para economizar tokens.
5. Quando o modelo responde **texto simples sem JSON**, a tarefa termina e
   ele envia o resumo. JSON inválido é pedido de novo (não encerra).

## Solução de problemas

| Problema | Solução |
|---|---|
| `Erro: defina CUA_API_KEY` | Abra ⚙ Configurações e cole a chave (gratuita em aistudio.google.com/apikey), ou edite o `.env`. |
| `401 / chave inválida` | A chave está errada ou foi revogada — gere outra em AI Studio. |
| `404 model no longer available` | O Google aposentou o modelo (ex.: `gemini-2.5-flash`). Troque `CUA_MODEL` pelo atual — hoje `gemini-3.5-flash-lite`. |
| `429 / RESOURCE_EXHAUSTED` | Cota gratuita do dia atingida: espere renovar (meia-noite, horário de Brasília) ou troque `CUA_MODEL` por um modelo mais leve. |
| `model: xxx not found` | Passe um ID de modelo válido: `--model <id>` ou `CUA_MODEL` (veja os modelos do provedor). |
| Resposta sem JSON / ação estranha | Confira o log: o prompt de sistema (regras das ações + inventário do PC) precisa ser enviado. Com `gemini-3.5-flash-lite` (padrão) o JSON sai limpo; o loop pede o JSON de novo automaticamente. |
| Cliques deslocados no Windows | O código já ativa DPI awareness; verifique se a resolução do sistema mudou depois de iniciar. |
| Preview lento ou congelado | A captura usa DXGI (`dxcam`); instale com `pip install dxcam` — sem GPU/VM o código cai no GDI automaticamente. |
| Uso de CPU alto | O preview já é econômico (1 quadro/s parado; ao vivo só durante a tarefa). Se ainda pesar, aumente `PREVIEW_IDLE_MS` em `main_gui.py`. |
| `pyperclip` erro no Linux | Instale `xclip` ou `xsel` (`sudo apt install xclip`). |
| Ação muito lenta/rapida | Ajuste `pyautogui.PAUSE` em `cuagent/actions.py`. |

## Cuidados

* O agente controla o mouse e o teclado **físicos** — não use a máquina enquanto
  ele roda.
* `-y` desliga toda confirmação; use apenas em ambientes controlados.
* Cada passo consome tokens da API (imagem + texto). Reduza
  `CUA_MAX_SCREENSHOT_WIDTH` e `CUA_MAX_STEPS` para baixar o consumo.
* Revise as instruções em `DEFAULT_SYSTEM_PROMPT` (`cuagent/config.py`) para
  adaptar o comportamento do agente ao seu caso de uso.
* As capturas de tela são enviadas para o provedor configurado — use a API
  que você confiar.
