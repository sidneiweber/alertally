# Catálogo de serviços e incidentes do Alertally

O catálogo ajuda o time de plataforma/SRE a identificar os serviços relacionados a um alerta. A página de incidentes reúne a investigação e o impacto confirmado.

## Catálogo

- Cadastre serviços técnicos com time responsável, ambiente e links para dashboard, runbook e repositório.
- Associe serviços a alertas pelas labels recebidas. Uma regra de uma linha, como `resource=orders-db` ou `resource="orders-db"`, já pode identificar um recurso. Acrescente `environment=production` na mesma regra para restringir a associação a produção. Separe regras alternativas com uma linha em branco.
- Use as combinações observadas nos alertas recentes ou informe as labels manualmente. Inclua o ambiente e o cluster na regra quando o nome do recurso se repetir. O campo de ambiente do cadastro descreve o serviço e a comparação usa as labels da regra.
- Alertas com a label `service` criam o serviço técnico automaticamente, com a regra `service=<valor>`. A entrada é marcada como automática e pode ser editada ou arquivada. Uma entrada arquivada não é recriada. A página de sugestões agrupa os alertas recentes que ainda não pertencem a nenhum serviço e permite criar o serviço, adicionar a regra a um serviço existente ou ignorar a sugestão. Ela também mostra a cobertura do catálogo nos últimos 7 dias. Veja o [plano de descoberta automática](2026-11-19-catalogo-descoberta-automatica.md).
- Agrupamentos reúnem serviços técnicos. Um serviço pode participar de vários agrupamentos. A visão do agrupamento mostra os alertas dos membros, contando cada ocorrência uma vez.
- As associações são avaliadas a partir das regras atuais, tanto para alertas ativos quanto para ocorrências anteriores. O painel mostra as ocorrências mais recentes do histórico ou até 100 alertas ativos por página de serviço ou agrupamento.
- Um serviço com alertas ativos tem um sinal para investigação. A indicação de impacto confirmado aparece nas entradas selecionadas em incidentes ativos. Um serviço sem alertas ativos tem apenas ausência de sinais recebidos.

## Incidentes

1. Abra ou crie um incidente pela ação em um alerta. A pessoa que cria assume a investigação. Repetir a ação no alerta inicial abre o mesmo incidente.
2. Adicione alertas relacionados e, quando houver evidência, descreva o impacto e selecione os serviços e agrupamentos afetados.
3. Use a linha do tempo para registrar hipóteses, decisões ou ações. Ela também mostra mudanças de responsável e estado, vínculos de alertas e as transições registradas pelos webhooks. Alertas antigos podem ter histórico parcial. Uma notificação de recuperação atua no alerta associado.
4. O responsável pode marcar o incidente como mitigado ou resolvê-lo assim que confirmar o fim do impacto. A lista de incidentes tem uma visão para os resolvidos.

A interface usa o login e a proteção CSRF do Alertally. Qualquer usuário autorizado pode cadastrar serviços e colaborar em incidentes. Um usuário pode assumir a investigação, e as mudanças de estado ficam com o responsável. Catálogo e incidentes usam coleções próprias no MongoDB, com histórico de transições gravado nos documentos dos alertas e dos incidentes.
