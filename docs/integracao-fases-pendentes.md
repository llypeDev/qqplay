# Integração com escolas: o que falta

Este documento registra o que ainda não foi feito na integração do QQplay com
sistemas de escola: a **fase 6**, que não começou, e as **pendências** que
ficaram das fases já entregues. Ele descreve o estado do código em 16 de
novembro de 2026, depois do
[llypeDev/qqplay#22](https://github.com/llypeDev/qqplay/pull/22).

O que já funciona está no [README](../README.md#integração-com-a-sua-escola).

## Onde estamos

O plano tem seis fases. Cinco estão no ar.

| Fase | Entrega | Situação |
| --- | --- | --- |
| 1 | Correção dos defeitos achados na revisão do código | feita ([#19](https://github.com/llypeDev/qqplay/pull/19)) |
| 2 | Embed que funciona, ou se explica, em qualquer site | feita ([#19](https://github.com/llypeDev/qqplay/pull/19)) |
| 3 | Escola sem servidor próprio: matrículas, código por e-mail, link da aula, webhooks de compra | feita ([#20](https://github.com/llypeDev/qqplay/pull/20)) |
| 4 | Moodle, Canvas e outros LMS, por LTI 1.3 | feita ([#21](https://github.com/llypeDev/qqplay/pull/21)) |
| 5 | Plugin de WordPress e oEmbed | feita ([#22](https://github.com/llypeDev/qqplay/pull/22)) |
| 6 | Conclusão de aula | **pendente** |

Das nove formas de integrar levantadas na pesquisa, sete estão prontas. Duas
não:

| Forma de integrar | Situação |
| --- | --- |
| Script com token assinado, iframe puro, identidade declarada pela plataforma, código por e-mail, LTI 1.3, link da aula, plugin de WordPress e oEmbed | prontas |
| App de celular (WebView) | **parcial**: a abertura direta existe; faltam o guia para quem programa o app e um teste em uma WebView de verdade ([pendências](#fases-1-e-2-embed)) |
| Pacote SCORM ou cmi5 | **pendente**, opcional ([6.6](#66-pacote-scorm-ou-cmi5-só-sob-demanda)) |

## Fase 6: conclusão de aula

### O problema

Hoje o QQplay sabe quem assistiu a uma **live** e por quantos minutos (a
presença, no estúdio e na API). Para **vídeos gravados** não há registro
nenhum, e a página da escola não é avisada de nada do que acontece no player.
Na prática:

- a escola não consegue marcar uma aula como concluída sozinha;
- um LMS não recebe nota nem conclusão;
- não há como saber, depois, quem viu qual vídeo.

### O que a fase entrega

Seis partes. A primeira é a base das outras; as três primeiras formam o
núcleo e fazem sentido juntas. Os tamanhos são estimativa minha, não medida.

#### 6.1 Registro do que cada aluno assistiu

*Tamanho: médio. Tudo o mais depende disto.*

- **O que é:** para cada pessoa e vídeo, quanto ela assistiu, até onde chegou
  e se concluiu.
- **Como fazer:** seguir o que a presença das lives já faz
  (`LiveAttendance` em `apps/live/models.py` e `apps/live/attendance.py`).
  O player do quadro avisa o portal de tempos em tempos, pela API do quadro
  (`/embed/api/media/<id>/…`), com a sessão que ele já tem. O registro é
  apagado junto com a pessoa (`identities.erase`), como o resto.
- **Onde aparece:** uma página por vídeo no estúdio, como a de presença, e
  `GET /api/integrations/v1/videos/<id>/progress/` para o servidor da escola.
- **Como testar:** testes de unidade para a contagem, e um passo novo em
  `tools/e2e/integration.mjs` com um aluno assistindo a um vídeo até o fim.

#### 6.2 Avisos para a página da escola

*Tamanho: pequeno.*

- **O que é:** o elemento da aula recebe eventos novos do script de
  incorporação: progresso e conclusão do vídeo. Hoje ele recebe só
  `qqplay:signed-in`, `qqplay:signed-out`, `qqplay:status` e `qqplay:error`.
- **Como fazer:** os quadros já mandam mensagens à página (`static/js/live.js`
  e `static/js/embed-comments.js`), e o script (`templates/integrations/sdk.js`)
  já as repassa como eventos. Falta o player mandar as novas.
- **Cuidado:** um evento no navegador pode ser forjado pelo próprio aluno.
  Serve para atualizar a tela; para liberar certificado ou nota, a escola deve
  confiar no webhook (6.3) ou na API (6.1).

#### 6.3 Webhooks de conclusão

*Tamanho: pequeno, depois da 6.1.*

- **O que é:** o servidor da escola é avisado quando um aluno conclui um
  vídeo, e possivelmente quando cumpre a presença mínima em uma live.
- **Como fazer:** acrescentar os eventos aos que já existem (`live.started`,
  `live.ended`, `recording.ready`, em `apps/integrations/webhooks.py`). A
  assinatura, as novas tentativas e o reenvio pela página já estão prontos.

#### 6.4 Nota e conclusão para o LMS (LTI)

*Tamanho: médio.*

- **O que é:** o Moodle ou o Canvas recebe a conclusão da atividade no diário
  de notas (serviço AGS do LTI). Opcionalmente, o portal lê a lista de alunos
  do curso (NRPS).
- **O que já existe:** a chave da ferramenta e o endereço de token do LMS
  (`auth_token_url`, guardado no cadastro do LMS).
- **O que falta:** pedir as permissões no cadastro (hoje o cadastro
  automático pede nenhuma), obter o token de acesso assinando com a chave da
  ferramenta, e enviar a nota para a linha que o LMS informa na abertura.
- **Como testar:** no Moodle real em Docker, como na fase 4.

#### 6.5 Concluir a aula no WordPress

*Tamanho: médio. Depende de cada plugin de curso.*

- **O que é:** quando o aluno conclui o vídeo, a aula do LearnDash, do Tutor
  LMS ou do LifterLMS é marcada como concluída.
- **Como fazer:** o plugin recebe o webhook (6.3) ou consulta a API (6.1) e
  chama a função de conclusão de cada plugin de curso. Pelo motivo da 6.2,
  não deve confiar só no aviso do navegador.
- **Limite conhecido:** o LearnDash é pago e não pôde ser testado na fase 5.

#### 6.6 Pacote SCORM ou cmi5 (só sob demanda)

*Tamanho: médio. Não recomendo fazer sem um cliente pedindo.*

- **O que é:** um arquivo pequeno, gerado pelo portal para um vídeo, que um
  LMS corporativo importa. Ele abre o player e repassa tempo e conclusão ao
  LMS.
- **Limite:** o SCORM não assina quem é o aluno, então o pacote não serve
  para identificar ninguém. A identidade teria de vir de outra forma (código
  por e-mail, por exemplo).

### Decisões que são suas

Antes de começar a fase, estas escolhas mudam o que é construído:

1. **O que é "concluir" um vídeo?** Uma porcentagem assistida (90%, por
   exemplo), chegar ao fim, ou um valor que cada escola escolhe.
2. **Pular conta?** Se só o tempo realmente assistido vale, o aluno que
   arrasta a barra até o fim não conclui. É mais justo e um pouco mais
   trabalhoso.
3. **Live conta como aula concluída?** Se sim, com quantos minutos de
   presença.
4. **O que vai para o LMS:** concluído ou não, ou uma porcentagem.
5. **Por quanto tempo guardar** o registro de quem assistiu.

## Pendências das fases entregues

Nada aqui impede o uso. São coisas não testadas, ou complementos que ficaram
de fora de propósito.

### Valem para todas as fases

| Pendência | Por que importa | Como fechar |
| --- | --- | --- |
| Testes de navegador só no Chromium | Safari e Firefox tratam quadros de outros sites de forma mais restrita. O desenho não depende de cookies de terceiros, mas isso não foi visto rodando neles | Instalar o Firefox e o WebKit do Playwright e rodar `tools/e2e/integration.mjs` neles (o roteiro hoje abre sempre o Chromium) |
| Stack de produção em Docker não rodada desde antes da fase 1 | As fases 1 a 5 só rodaram no servidor de desenvolvimento. Caddy, PostgreSQL, Redis e o serviço `events` não viram este código | Subir `docker-compose.prod.yml` na máquina e rodar o teste de integração contra ele (o README explica como) |

### Fases 1 e 2 (embed)

| Pendência | Por que importa | Como fechar |
| --- | --- | --- |
| Guia para app de celular e teste em WebView | A abertura direta existe, mas uma WebView vem com recursos desligados (no Android, JavaScript e armazenamento; no iPhone, vídeo dentro da página). Quem programa o app precisa saber o que ligar | Escrever o guia no README e abrir uma aula em uma WebView de verdade |
| Três suspeitas da revisão não conferidas | A revisão levantou, sem confirmar: mensagem de chat pulada sob concorrência no PostgreSQL; presença com menos de 30 s pendentes nunca gravada; foto reanexada se o download termina depois da exclusão da pessoa | Tentar reproduzir cada uma; corrigir ou descartar |
| Sessão guardada no navegador dentro de um quadro, no Safari | O login por código guarda a sessão para não pedir o código a cada aula. O Safari pode não manter esse armazenamento em quadros de outros sites; o aluno teria de pedir o código de novo | Testar no Safari (primeira linha da tabela anterior) |

### Fase 3 (escola sem servidor)

| Pendência | Por que importa | Como fechar |
| --- | --- | --- |
| Webhooks de compra feitos pela documentação, sem uma entrega real | Hotmart, Kiwify, Eduzz, Greenn, Ticto, Memberkit, Cademí e Monetizze foram lidos da documentação de cada uma. Um campo com outro nome faria a matrícula não ser criada | Com uma conta em cada plataforma, usar o botão de teste dela e conferir no painel da integração o que chegou |
| Kiwify: nomes dos campos do comprador | Não estão na documentação dela | O mesmo teste acima |
| Kirvano e Braip sem leitura própria | Caem na leitura genérica, que procura o e-mail e a situação onde costumam estar | Acrescentar a leitura de cada uma se houver cliente |
| Envio de e-mail | O login por código só funciona com o envio de e-mails configurado (`EMAIL_HOST` e companhia). É configuração, não código | Configurar no servidor de produção |

### Fase 4 (LTI)

| Pendência | Por que importa | Como fechar |
| --- | --- | --- |
| Só o Moodle 5 foi testado | Canvas, Blackboard, Brightspace, Schoology, Sakai e Open edX seguem o mesmo padrão, mas cada um tem particularidades. O Moodle real já mostrou uma (o domínio com porta) | Testar no Canvas, que tem imagem Docker pública, e nos demais conforme houver cliente |
| LMS em outro domínio | No teste, o Moodle e o portal estavam na mesma máquina. A entrada vinda de outro domínio não foi exercitada com um LMS real | Testar com o LMS e o portal em domínios diferentes, com HTTPS |
| Notas e lista de alunos (AGS e NRPS) | Não existem. É a parte 6.4 | Fase 6 |

### Fase 5 (WordPress e oEmbed)

| Pendência | Por que importa | Como fechar |
| --- | --- | --- |
| LearnDash não testado | É pago. O plugin chama as funções da documentação pública dele; se uma mudou, as turmas dos alunos do LearnDash vêm vazias (o plugin não quebra a página) | Testar com uma licença |
| Plugins de cache não testados | A proteção é o token nunca estar na página, e isso foi conferido. Não foi visto com WP Rocket, LiteSpeed e parecidos ligados | Instalar um e repetir `tools/e2e/wordpress.mjs` |
| MasterStudy, LearnPress e Sensei sem turmas | Com eles, todo aluno logado fica sem turma, e uma live restrita a um curso não abre para ninguém além de quem modera | Acrescentar a leitura de cada um, ou usar o filtro `qqplay_user_classes` |
| Atualização do plugin | Não há aviso de versão nova no WordPress: a escola baixa o arquivo de novo no portal | Um canal de atualização próprio, ou publicar no diretório do WordPress.org |
| Bloco do editor | Só há shortcode e link colado. Um bloco próprio deixaria escolher a live em uma lista | Opcional |
| oEmbed com Iframely e parecidos | As páginas anunciam o oEmbed, mas nenhum serviço desses foi testado | Testar com um deles |

## O que não depende do QQplay

Três limites vêm de fora e nenhuma fase resolve:

- **Plataformas que só aceitam links de uma lista fixa de players** (Skool,
  Ticto, Astron, HeroSpark e o campo de mídia da Cademí): o QQplay só aparece
  dentro da aula se a plataforma o incluir na lista. Até lá, o caminho é o
  link da aula.
- **Google Classroom e Microsoft Teams** não têm LTI. O caminho é o link da
  aula.
- **Sistemas de gestão escolar brasileiros** (TOTVS Educacional, Sponte,
  Lyceum, Agenda Edu, ClassApp): não há documentação pública sobre embutir
  conteúdo externo. Depende de cada fornecedor.

## Ordem sugerida

1. **Safari, Firefox e a stack de produção.** São testes, não código novo, e
   dizem se o que já foi entregue aguenta o uso real. Faria antes de
   construir mais.
2. **Fase 6, partes 6.1 a 6.3.** O núcleo: registro, avisos e webhooks.
3. **Partes 6.4 e 6.5**, conforme as escolas atendidas usem LMS ou WordPress.
4. **Webhooks de compra com entregas reais**, conforme houver conta em cada
   plataforma.
5. **Parte 6.6 e os complementos opcionais**, só com um cliente pedindo.
