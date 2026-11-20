# Descoberta automática de serviços no catálogo

> Status: fases 1 e 2 implementadas (itens 1 a 5 da ordem de entrega). As tags da AWS (item 6) ficam para depois.

Hoje cada serviço do catálogo é cadastrado à mão, com regras de labels. Este plano reduz esse trabalho em duas frentes:

1. **Criação automática pela label `service`**: um alerta com `service=<valor>` cria o serviço técnico no catálogo, se ele ainda não existir.
2. **Sugestões de serviços**: alertas sem `service` são agrupados por labels que identificam recursos, e o Alertally sugere novos serviços ou regras para um serviço existente.

O cadastro manual continua valendo. A descoberta só cria entradas e propõe regras. Ela nunca altera o que alguém editou.

## Fase 1: criação automática pela label `service`

### Configuração

| Variável | Padrão | Uso |
|---|---|---|
| `CATALOG_SERVICE_LABELS` | `service` | Labels que identificam o serviço, separadas por vírgula e em ordem de prioridade (ex.: `service,resource`). Vale a primeira presente no alerta. Nomes inválidos para o Prometheus são ignorados. |
| `CATALOG_AUTO_CREATE` | `true` | Desliga a criação automática sem desligar as sugestões. |
| `CATALOG_STALE_DAYS` | `30` | Dias sem alertas para marcar um serviço como "sem atividade". |

### Modelo de dados (`db.catalog`)

Campos novos nas entradas do tipo `service`:

- `origin`: `manual` (padrão para as entradas existentes) ou `auto`.
- `auto_key`: valor da label que originou a entrada, por exemplo `orders-api`. Fica só nas entradas automáticas.
- `last_alert_at`: data do alerta mais recente associado pela label. É atualizado com `$max`.
- `archived`: `true` quando o time arquiva a entrada. Uma entrada arquivada não é recriada.

Índice novo: `auto_key` único e parcial (só em documentos que têm o campo). Ele resolve a concorrência entre entregas simultâneas do mesmo serviço.

### Fluxo

O ponto comum de gravação é `store_alert()` em `utils/alert_history.py`. GCP, Alertmanager e AWS passam por ele. Depois da gravação do alerta, `ensure_service(labels, at)` faz o seguinte:

1. Lê a primeira label de `CATALOG_SERVICE_LABELS` presente no alerta, em ordem de prioridade. Assim, um alerta cria no máximo uma entrada. Ignora valores vazios, com quebra de linha ou com mais de 120 caracteres (o limite do nome). Esses alertas seguem para as sugestões da fase 2.
2. Se alguma entrada já tem uma regra que cobre `service=<valor>` (`{'rules.<label>': valor}`), só atualiza `last_alert_at` e encerra. Assim, um cadastro manual feito antes não é duplicado.
3. Senão, faz um upsert por `auto_key` com `$setOnInsert`: `kind=service`, `name=<valor>`, `origin=auto`, `rules=[{<label>: <valor>}]`, campos de texto vazios. Também aplica `$max: {last_alert_at}`.
4. Trata `DuplicateKeyError` como sucesso, porque outra entrega criou a entrada.

O `$setOnInsert` garante que nada criado automaticamente sobrescreve uma edição posterior. Nome, dono, links e regras ficam por conta de quem editar. Uma falha nesse passo é registrada em log e não pode impedir o webhook de gravar o alerta.

Custo: uma consulta indexada e um upsert por alerta recebido. Se o volume de webhooks pedir, dá para manter um cache em memória dos valores já vistos, com expiração curta.

### Ambientes

Um mesmo `service=orders-api` em produção e homologação vira **uma** entrada, com a regra só por `service`. A página do serviço já mostra a label `environment` de cada alerta. Separar por ambiente na criação automática (`service`+`environment`) fica como opção futura, caso o time prefira entradas distintas. Quem precisar disso pode editar a regra da entrada automática.

### Fontes de alerta

- **Alertmanager**: já envia as labels do alerta. Basta o time incluir `service` nas regras do Prometheus ou nos `external_labels`. Documentar em `docs/alertmanager.md`.
- **GCP Monitoring**: hoje só `incident.metric.labels` vira `labels`. Juntar também `incident.resource.labels` e `incident.policy_user_labels`, nesta ordem de precedência: recurso < métrica < labels da política. Assim, uma label `service` declarada na política de alerta é respeitada. Confirmar os nomes dos campos com um payload real antes de implementar.
- **AWS CloudWatch**: a notificação SNS não traz as tags do recurso nem do alarme. Na fase 1, alertas da AWS entram pelas sugestões. Uma fase 3 opcional pode consultar `ListTagsForResource` do alarme (exige credenciais e a permissão `cloudwatch:ListTagsForResource`).

### Interface

- Na lista do catálogo, as entradas automáticas têm um selo "Automático". Entradas sem alertas há `CATALOG_STALE_DAYS` dias têm o selo "Sem atividade". Nada é apagado.
- Na edição de uma entrada, a ação "Arquivar" marca `archived=true`. A lista esconde as arquivadas por padrão, com um filtro para mostrá-las e reativá-las.
- Editar uma entrada automática não muda a `origin`. O selo só informa de onde ela veio.

## Fase 2: sugestões de serviços

### Como as sugestões são calculadas

Calculadas sob demanda em `GET /catalog/suggestions`, sem coleção própria:

1. Ler os alertas dos últimos 30 dias (índice de `update_at`, limite de 5.000 documentos), só os campos `labels`, `name` e `update_at`.
2. Descartar os alertas que já casam com alguma entrada do catálogo, reaproveitando a lógica de `_matching_services`.
3. Para cada alerta restante, montar uma **chave candidata**:
   - **identificador**: a primeira label presente, em ordem de prioridade: a label de serviço configurada, `app_kubernetes_io_name`, `app`, `resource`, `service_name` (Cloud Run), `DBInstanceIdentifier`, `InstanceId`, `FunctionName`, `LoadBalancer`, `ClusterName` e, por último, `job`. O Prometheus não aceita pontos em nomes de labels, então `app.kubernetes.io/name` chega como `app_kubernetes_io_name`. O `job` fica no fim porque costuma ser genérico (`kubernetes-pods`, `node-exporter`);
   - **qualificadores**, quando presentes: `namespace`, `environment`, `env`, `cluster`, `project`, `account` e `region`, para não misturar recursos de mesmo nome.
4. Agrupar pela chave e exibir as 50 com mais alertas. Cada sugestão mostra a quantidade, o último alerta, até 3 nomes de alerta de exemplo e a regra proposta.

A lista de labels candidatas substitui `RESOURCE_LABELS` como fonte única. O `_observed_rules()` da tela de edição passa a usar as mesmas chaves.

### Ações

- **Criar serviço**: abre `catalog.edit` com o nome e a regra preenchidos. A entrada criada é `origin=manual`, porque passou pela revisão de alguém.
- **Adicionar a um serviço**: escolhe uma entrada existente e acrescenta a regra (respeitando o limite de 30 regras).
- **Ignorar**: grava a chave em `db.catalog_dismissed` (`{rule, actor, at}`, índice único em `rule`). Chaves ignoradas não aparecem mais. Um filtro permite revê-las.

### Cobertura

O topo da página de sugestões mostra a cobertura do catálogo: a porcentagem dos alertas dos últimos 7 dias associados a algum serviço. Esse número mostra se as labels estão sendo adotadas, e o índice do catálogo mostra um contador de sugestões abertas.

## Testes (`tests/test_operations.py` e `tests/test_webhooks.py`)

Fase 1:
- Um webhook do Alertmanager com `service=orders-api` cria uma entrada `auto` com a regra `service=orders-api`, e uma segunda entrega não duplica a entrada.
- Uma entrada manual com regra `service=orders-api` impede a criação automática.
- Editar uma entrada automática e receber um novo alerta mantém a edição e atualiza `last_alert_at`.
- Uma entrada arquivada não é recriada.
- Valores inválidos (vazio, com quebra de linha, longo demais) não criam entrada.
- `CATALOG_AUTO_CREATE=false` e `CATALOG_SERVICE_LABELS=app` são respeitados, e com `service,resource` vale a primeira label presente.
- No GCP, uma label `service` em `policy_user_labels` chega em `labels` e cria a entrada.
- Uma falha no catálogo não impede a gravação do alerta.

Fase 2:
- Alertas já cobertos por uma regra não geram sugestão.
- A prioridade das labels e os qualificadores geram as chaves esperadas.
- Ignorar remove a sugestão. Criar e adicionar a regra fazem o alerta deixar de aparecer.
- As ações exigem login e CSRF.

## Ordem de entrega

1. Configuração, campos, índice e `ensure_service()` em `store_alert()`, com testes.
2. Labels do GCP (resource e policy) e documentação da label `service` para Alertmanager e GCP.
3. Selos "Automático" e "Sem atividade", e a ação de arquivar.
4. Página de sugestões, ações e cobertura.
5. Traduções pt_BR/es das strings novas.
6. (Opcional) Tags da AWS via `ListTagsForResource`.

## Pontos em aberto

- Criar uma entrada por `service` ou por `service`+`environment`? O plano assume a primeira opção.
- A criação automática deve nomear a entrada pelo valor cru (`orders-api`) ou aplicar alguma formatação? O plano assume o valor cru, que pode ser renomeado depois.
- Os 30 dias de janela e o limite de 5.000 alertas das sugestões são suficientes para o volume atual?
