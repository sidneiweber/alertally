# Enriquecimento de alertas e janelas de manutenção

> Status: implementado, com as decisões da seção "Decisões".

Duas melhorias para o plantão, inspiradas no Keep (keephq.dev), que aproveitam o catálogo de serviços:

1. **Enriquecimento**: a lista de alertas mostra o serviço, o time responsável e os links de runbook e dashboard de cada alerta, sem abrir a página do serviço.
2. **Janelas de manutenção**: durante um deploy ou manutenção planejada, os alertas dos serviços ou labels afetados continuam sendo gravados, mas saem da lista principal, do contador e dos relatórios de ruído.

## Parte 1: enriquecimento

### Como a associação é feita

O catálogo já avalia as associações pelas regras atuais (`_matching_services`). O enriquecimento segue a mesma ideia e é calculado **na leitura**, não gravado no alerta. Assim, uma regra nova ou editada vale na hora para os alertas abertos, sem migração.

Em `alerts_list()` (atualizada a cada 5 s, até 100 alertas):

1. Carregar os serviços do catálogo uma vez por requisição, incluindo os arquivados, porque uma entrada arquivada continua associada aos seus alertas.
2. Para cada alerta, calcular os serviços que casam com as labels.
3. Montar o enriquecimento: nomes dos serviços (com link), dono e links de runbook e dashboard.

Custo: 100 alertas × regras do catálogo, em Python. Com centenas de regras isso continua em milissegundos. Se o catálogo crescer muito, dá para guardar a lista de serviços em memória por alguns segundos.

### Runbook do próprio alerta

Hoje o Alertmanager envia a anotação `runbook_url`, mas o Alertally não a grava. O plano passa a gravá-la no alerta (`runbook_url`), validando que é um link HTTP ou HTTPS. A precedência fica assim:

1. `runbook_url` do próprio alerta, por ser mais específico (um runbook por regra de alerta);
2. o runbook do serviço no catálogo.

O GCP envia a documentação da política no `summary`, e a AWS não tem campo equivalente. Para essas fontes vale só o runbook do catálogo.

### Interface

Na lista de alertas, abaixo do nome:

- os serviços associados, com link (até 2, e "+N" quando houver mais);
- o dono do primeiro serviço que tiver um;
- ícones para o runbook e o dashboard, quando existirem.

Alertas sem serviço associado não mudam. Na página do incidente, a lista de alertas vinculados mostra os mesmos dados (macro `alert_list`).

### Fora do escopo

- Filtros por serviço ou dono na lista de alertas (visões salvas). É o passo natural seguinte e fica para outro plano.
- Gravar o serviço no alerta para usar nos relatórios. Os relatórios já agrupam por `labels.service` e `labels.resource`.

## Parte 2: janelas de manutenção

### Modelo de dados (`db.maintenance_windows`)

| Campo | Descrição |
|---|---|
| `title` | Motivo curto, por exemplo "Deploy do orders-api". |
| `entry_ids` | Serviços ou grupos do catálogo afetados. Um grupo vale para todos os seus serviços. |
| `rules` | Regras de labels no mesmo formato do catálogo, para recursos fora do catálogo. |
| `starts_at`, `ends_at` | Período, em UTC. Pode ser agendado para o futuro. |
| `created_by`, `created_at` | Quem criou e quando. |
| `ended_by`, `ended_at` | Preenchidos quando alguém encerra a janela antes do fim. |

A janela precisa de pelo menos uma entrada do catálogo ou uma regra, o que impede uma janela "silencia tudo". Ela não é editada depois de criada: só pode ser encerrada antes do fim, para que o histórico continue fiel ao que estava valendo.

### Efeito nos alertas

**Na lista, calculado na leitura.** Um alerta aberto que casa com uma janela **ativa agora** sai da lista principal e vai para uma seção recolhida "Em manutenção (N)", com o título e o fim da janela. Quando a janela termina, o alerta volta para a lista principal se ainda estiver aberto. É o mesmo comportamento dos silences do Alertmanager.

**No contador** (`/count` e o selo no favicon): não conta alertas em manutenção.

**Nos relatórios, registrado na gravação.** Quando uma ocorrência **abre** durante uma janela ativa que casa com ela, `store_alert()` grava `maintenance_id` no alerta, no mesmo ponto em que hoje chama `ensure_service()`. Os relatórios ganham uma opção "incluir alertas em manutenção", desligada por padrão. Gravar na abertura mantém o relatório correto mesmo depois que a janela acaba ou que as regras do catálogo mudam.

**O que não muda:**

- os webhooks continuam aceitando e gravando tudo;
- recuperações continuam fechando os alertas;
- ACK, fechar e criar incidente continuam disponíveis na seção "Em manutenção";
- um alerta crítico em manutenção continua visível na seção, que mostra a contagem por severidade.

### Permissões e limites

- Só admins (`ADMIN_EMAILS`) podem criar, encerrar ou cancelar uma janela. O nome de quem criou fica visível na lista e na faixa de aviso.
- A duração máxima é configurável (`MAINTENANCE_MAX_HOURS`, padrão 24 h), para evitar uma janela esquecida silenciando um serviço por dias.
- Enquanto houver uma janela ativa, a página de alertas mostra uma faixa: "Manutenção ativa: Deploy do orders-api, até 23:00 (Ana)".

### Interface

- **Sidebar**: item "Manutenção" (`/maintenance`) com as janelas ativas, agendadas e as recentes.
- **Nova janela**: título, início (padrão: agora), duração ou fim, serviços e grupos do catálogo, e regras de labels opcionais. Datas no fuso de `TIMEZONE`.
- **Página do serviço ou grupo**: botão "Agendar manutenção" com a entrada já selecionada.
- **Página da janela**: alertas que abriram durante ela e os que estão em manutenção agora.

### Fora do escopo

- Janelas recorrentes (por exemplo, toda terça das 2h às 3h).
- Criar um silence no Alertmanager a partir da janela. Isso exigiria acesso à API do Alertmanager.
- Notificar o Slack ao abrir ou encerrar uma janela.

## Testes

Enriquecimento:
- Um alerta associado a um serviço mostra o nome, o dono e os links. Um alerta sem serviço não muda.
- O `runbook_url` do alerta tem precedência sobre o do serviço, e um link inválido é ignorado.
- Uma regra criada depois do alerta enriquece o alerta já aberto.

Janelas de manutenção:
- Um alerta que casa com uma janela ativa sai da lista principal e do contador e aparece na seção "Em manutenção". Ele volta para a lista quando a janela acaba ou é encerrada.
- Uma janela agendada para o futuro não afeta os alertas antes do início.
- Uma janela por grupo cobre os serviços membros. Uma janela por regra cobre alertas fora do catálogo.
- `maintenance_id` é gravado só nas ocorrências que abrem durante a janela, e os relatórios as excluem por padrão.
- Validações: janela sem entradas nem regras, fim antes do início e duração acima do máximo.
- Só admins criam e encerram janelas. Login e CSRF são exigidos.

## Ordem de entrega

1. Enriquecimento na lista de alertas e no incidente, com a gravação do `runbook_url` do Alertmanager.
2. Coleção, formulário e lista de janelas de manutenção.
3. Efeito na lista de alertas, no contador e na faixa de aviso.
4. `maintenance_id` na gravação e opção nos relatórios.
5. Botão "Agendar manutenção" nas páginas do catálogo, traduções e documentação.

## Decisões

- Só admins criam e encerram janelas.
- A duração máxima padrão é de 24 h.
- Os alertas em manutenção ficam numa seção recolhida no fim da lista. O estado aberto ou fechado da seção é mantido entre as atualizações automáticas da lista.
- Uma janela agendada que é cancelada antes de começar fica com início e fim iguais ao momento do cancelamento, e aparece entre as encerradas.
