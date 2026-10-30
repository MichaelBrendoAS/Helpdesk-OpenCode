# Helpdesk-OpenCode

Versão que roda **somente pelo OpenCode**, sem chave de API. O app entrega a
tarefa ao `opencode run`. O OpenCode decide os passos com os modelos
gratuitos do Zen e age no PC pelas ferramentas de tela (servidor MCP embutido
no próprio .exe).

## Requisitos
- Windows 10/11 e internet (na primeira abertura). Só isso — o app monta
  tudo sozinho ao abrir, nesta ordem:
  1. **OpenCode**: baixa a versão oficial testada (1.18.32, ~60 MB) para
     `tools\opencode\`. Sempre usa ESSA versão, mesmo se o PC já tiver outro
     OpenCode instalado (versões diferentes mudam a API e quebravam o app).
     Não precisa de Node, npm nem administrador.
  2. **Perfil isolado** em `.opencode_home\`: não herda plugins nem
     configurações de outro OpenCode do PC.
  3. **Servidor do OpenCode** (`opencode serve`, só em 127.0.0.1, com senha
     aleatória): fica ligado em segundo plano enquanto o app estiver aberto e
     é encerrado ao fechar.
  4. **Ferramentas de tela** (o próprio .exe com `--mcp`), conectadas ao
     servidor. Antes de cada tarefa o app confere a conexão e reinicia tudo
     se preciso.
- Problemas nas ferramentas de tela ficam registrados em `logs\mcp.log`.
- Na 1ª abertura o Windows SmartScreen pode avisar (exe sem assinatura):
  "Mais informações" → "Executar assim mesmo".

## Como usar
1. Abra `Helpdesk-OpenCode.exe`.
2. Escreva a tarefa e clique em Executar. O log mostra o modelo em uso e
   cada ação, por exemplo `> click(x=500, y=300)`.
3. O botão ■ Parar encerra o OpenCode na hora.

Ao executar, o app grava `opencode.json` e `AGENTS.md` ao lado do .exe.
Esses arquivos são gerados de novo a cada execução.

## Modelos (alternados automaticamente)
Ordem: Space Bunny → Muse Spark 1.2 → Muse Spark 1.3 → MiMo V2.6 Flash.
Se um modelo recusar ou falhar ANTES de agir na tela, o app passa para o
próximo. Para mudar a ordem, defina no `.env`:
`CUA_OPENCODE_MODELS=space-bunny-free,mimo-v2.6-flash-free`

Nemotron, Big Pickle e Ling não entram na lista porque são só de texto e
não enxergam a tela. Em 25/09/2026, só o Space Bunny aceitou o uso com as
ferramentas de tela. Os outros o Zen recusou ("free tier can only be used
from within OpenCode") ou deram erro no servidor.

## Ferramentas de tela
screenshot, click, type_text, press_key, scroll, drag, open_app e wait.
Coordenadas de 0 a 1000. Cada ação já devolve a tela atualizada. Ações
destrutivas pedem confirmação numa caixa do Windows. Programas abertos são
trazidos para o monitor escolhido.
