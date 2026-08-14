# Pipeline A — Robô de Monitoramento de Documentos de FII

Arquitetura e condições de contorno para implementação. **Revisão 3.**

> **Como usar este documento.** Ele define **o quê** e **por quê**, não **como**. Escolhas de linguagem, bibliotecas, estrutura de módulos, agendamento, empacotamento, logging e testes são do agente implementador. Seções marcadas **Invariante** são requisitos que não podem ser violados.
>
> **Sobre a seção 2:** tudo ali foi verificado empiricamente contra o sistema real. Trate como fato observado, não como suposição — mas note que a fonte não tem contrato de API e pode mudar sem aviso.
>
> **Sobre a seção 9:** lista o que permanece não verificado. O agente **deve** confirmar esses pontos empiricamente durante a implementação, e é livre para propor ajustes na arquitetura conforme o que encontrar.

---

## 0. O que mudou na revisão 3

| Área | Mudança | Motivo |
|---|---|---|
| **Descoberta** | Consulta **por entidade** (`idFundo`) volta a ser o caminho primário; a listagem global vira auditoria periódica | A listagem global não traz `cnpjFundo` nem `idFundo`, o que forçava roteamento por igualdade de texto — modo de falha silencioso, agravado pelas classes da RCVM 175 |
| **Escopo monitorado** | Deixa de ser "um fundo = um CNPJ = um id" e passa a ser um **conjunto de entidades** (fundo e/ou classes) | Confirmado: desde 1º/7/2025 alguns documentos de FII são enviados pelas classes, com CNPJ próprio |
| **Janela de consulta** | Toda execução consulta a **janela de retenção inteira**, não um intervalo incremental | Unifica descoberta, recuperação de offline e reconciliação de status mutável em uma só consulta; torna o watermark quase dispensável |
| **Estado do download** | Máquina de estados `descoberto → baixando → disponivel` com reconciliação na inicialização | Filesystem e SQLite não formam transação atômica |
| **YAML** | Verificação de hash antes do `rename` | Escrita atômica evita corrupção, não evita sobrescrever edição humana concorrente |
| **Layout em disco** | Separação explícita entre **raiz de dados** (privada) e **raiz de documentos** (compartilhável) | Só a pasta de documentos será exposta por SMB |
| **Portabilidade** | Seção 7 nova: nada no desenho pode depender de Docker, TrueNAS, systemd ou de caminhos pessoais | O empacotamento vem depois da validação, e o projeto pode ser aberto |
| **Novos** | Índice `_inbox`, validação anti-HTML no parse XML, nome de arquivo como snapshot, identidade de publicação × correlação lógica | Revisão cruzada |

---

## 1. Objetivo

Robô que baixa diariamente documentos de fundos imobiliários publicados no Fundos.NET e os organiza em diretórios por dia, para **leitura humana ao longo do dia**.

**Não é** arquivo permanente. É uma janela deslizante de N dias: o usuário abre o diretório do dia, vê o que há de novo, e diretórios antigos são apagados automaticamente.

### Fora de escopo

- Parsing de conteúdo dos documentos (é do Pipeline B)
- Preservação de longo prazo
- Resolução de ticker de negociação B3 — **decisão fechada**: não existe fonte nativa em CVM ou Fundos.NET (verificado no FCA, nos CSVs de informe mensal do Portal de Dados Abertos e no XML do Informe Mensal Estruturado, que traz apenas `CodigoISIN`). Não introduzir dependência externa para isso
- Correlação histórica entre um documento e sua reapresentação, quando chegam com `id` distintos (ver 6.1)

### Relação com o Pipeline B

Pipeline B (ETL de Informe Mensal Estruturado → banco → planilha evolutiva) **reaproveita o módulo de download**, mas com ciclo de vida próprio.

**Invariante:** o downloader é infraestrutura compartilhada; os pipelines são consumidores independentes.

```
               ┌── Pipeline A
Fundos.NET ─── downloader
               └── Pipeline B
```

E **não**: `Fundos.NET → Pipeline A → arquivos → Pipeline B`.

**Invariante:** o Pipeline B não lê arquivos gravados pelo A, e a purga do A nunca depende do progresso do B. Se B precisa de um XML, baixa o seu próprio.

> **Contraponto considerado e rejeitado:** cache compartilhado em disco entre A e B. Rejeitado por custo/benefício — B consome apenas documentos "Estruturado" (≈1 por entidade/mês), enquanto A baixa tudo diariamente; a economia de rede é marginal e o preço seria reintroduzir o acoplamento que este desenho evita. Se o volume um dia justificar, a forma correta é cache HTTP na camada de transporte, não B lendo o diretório de A.

---

## 2. Fonte de dados — comportamento verificado

Host: `fnet.bmfbovespa.com.br`. `idTipoFundo=1` / `tipoFundo=1` = Fundo Imobiliário.

### 2.1 Endpoints

| Endpoint | Uso |
|---|---|
| `GET /fnet/publico/listarFundos?term={termo}&page=1&idTipoFundo=1&idAdm=0&paraCerts=false` | Resolve nome → `id` interno. Retorna `{results: [{id, text}], more: bool}` |
| `GET /fnet/publico/listarTodasCategoriaPorTipoFundo?idTipoFundo=1` | Vocabulário de categorias: `{id, descricao, sigla}` |
| `GET /fnet/publico/pesquisarGerenciadorDocumentosDados?...` | Lista documentos (ver 2.2) |
| `GET /fnet/publico/downloadDocumento?id={idDocumento}` | Baixa o arquivo |

Nenhum exigiu captcha ou sessão autenticada, apesar do captcha visível na interface HTML.

### 2.2 Busca de documentos — parâmetros

```
pesquisarGerenciadorDocumentosDados
  ?d={draw}&s={start}&l={length}
  &o[0][{campo}]={asc|desc}
  &tipoFundo=1
  &idFundo={id}                      ← opcional; ver abaixo
  &dataInicial=dd/MM/yyyy
  &dataFinal=dd/MM/yyyy
  &idCategoriaDocumento=0&idTipoDocumento=0&idEspecieDocumento=0
  &isSession=false
```

**Descobertas centrais:**

| Comportamento | Status |
|---|---|
| `dataInicial` / `dataFinal` filtram por **`dataEntrega`**, não por `dataReferencia` | **Verificado.** Com intervalo 12–14/08, todos os `dataEntrega` caíram dentro, e os `dataReferencia` ficaram fora (26/08, 27/08, 28/08, 14/09) |
| Ambos os extremos são inclusivos | **Verificado** (12/08 e 14/08 presentes) |
| **Omitir `idFundo`** retorna todos os fundos do período | **Verificado** — 1616 documentos em 12–14/08 |
| `idFundo=0` retorna **vazio** | **Verificado.** `0` não significa "todos"; é id inexistente |
| Filtro por `cnpj` **não funciona** nesta busca | **Verificado** — parâmetro ignorado |
| `s`/`l`/`d` são paginação DataTables (start/length/draw); resposta traz `recordsTotal` e `recordsFiltered` | **Verificado** |

**Volume observado:** ≈540 documentos/dia para todo o mercado de FII.

### 2.3 Schema da resposta

Campos relevantes de cada item em `data[]`:

| Campo | Exemplo | Nota |
|---|---|---|
| `id` | `1290363` | Id do documento, usado em `downloadDocumento` |
| `versao` | `1`, `2` | **Parte da identidade.** Ver 2.4 |
| `descricaoFundo` | `HEDGE BRASIL SHOPPING FUNDO...` | Nome completo, sem CNPJ. Único vínculo textual com a entidade na listagem global |
| `categoriaDocumento` | `Informes Periódicos` | Texto |
| `tipoDocumento` | `AGO`, `Rendimentos e Amortizações`, `""` | **Inconsistente** — código em Assembleia, texto no estruturado, vazio em outros. Pode vir com espaços residuais |
| `especieDocumento` | `Carta Consulta`, `Edital de Convocação` | Em Assembleia, é este campo que carrega o sentido real, não `tipoDocumento` |
| `dataEntrega` | `13/08/2026 19:34` | `dd/MM/yyyy HH:mm`. **Eixo de descoberta e de arquivamento** |
| `dataReferencia` | `07/2026`, `11/08/2026`, `14/09/2026 23:59` | **Três formatos** — ver `formatoDataReferencia`. Pode ser **futura** |
| `formatoDataReferencia` | `2`, `3`, `4` | `2` = competência (MM/yyyy); `3` = data; `4` = data com hora |
| `descricaoModalidade` | `Apresentação`, `Reapresentação Espontânea`, `Reapresentação por Exigência` | |
| `modalidade` | `AP`, `RE` | Código do anterior |
| `descricaoStatus` / `status` / `situacaoDocumento` | `Ativo com visualização` / `AC` / `A` | Situação na UI: Ativo, Inativo, Cancelado. **Mutável após a entrega** |
| `fundoOuClasse` | `Classe` | Estrutura pós-RCVM 175. Ver 2.7 |

**Campos que vêm sempre `null` ou vazios — armadilhas confirmadas:**

| Campo | Comportamento |
|---|---|
| `cnpjFundo`, `idFundo`, `nomeAdministrador` | `null` em **todas** as linhas, inclusive quando a consulta filtra por `idFundo` |
| `nomePregao` | Vazio (`""`) em boa parte dos fundos. Quando presente, é apelido interno (`FII HEDGEBS`), **não o ticker B3** (`HGBS11`). Não usar como identificador |
| `arquivoEstruturado` | Vem `" "` mesmo em documentos que são XML. Não é flag confiável |

**Consequência central de desenho:** a resposta **não identifica a entidade emissora**. Quem consulta com `idFundo` sabe a quem o documento pertence pelo parâmetro que enviou; quem consulta a listagem global, não. Isso é o que determina a seção 4.

### 2.4 Reapresentação

Observado em dado real: `V2 RENDA IMOBILIÁRIA`, `id 1286620`, `versao: 2`, `modalidade: "RE"` (Reapresentação Espontânea).

**Invariante — identidade de publicação:** a chave de deduplicação e de idempotência é `(id_documento, versao)`, nunca `id_documento` sozinho.

**Distinção que precisa ficar explícita:**

| Conceito | Definição | Uso |
|---|---|---|
| Identidade de publicação | `(id_documento, versao)` | Dedupe, idempotência, nome de arquivo |
| Correlação lógica | Documento original ↔ sua reapresentação, quando chegam com `id` distintos | **Fora de escopo do Pipeline A** |
| Hash de conteúdo | SHA do arquivo baixado | Integridade e auditoria. **Nunca** chave primária de dedupe |

O comportamento exato da listagem (se acumula versões ou exibe só a vigente) está em aberto — ver 9.1.

### 2.5 Formato do arquivo baixado

`downloadDocumento` serve **PDF ou XML pelo mesmo endpoint**.

Documentos cuja categoria ou tipo contém **"Estruturado"** retornam XML. Casos observados: Informe Mensal Estruturado, Informe Trimestral Estruturado, Aviso aos Cotistas - Estruturado / Rendimentos e Amortizações, Formulário de Subscrição de Cotas (Estruturado).

**Invariante:** a heurística "Estruturado" serve para *roteamento antecipado* (decidir o que interessa antes de baixar). **A extensão gravada em disco é determinada pela resposta real**, em ordem de confiança:

1. Assinatura do conteúdo — **decisiva**
2. `Content-Disposition`
3. `Content-Type` — **o menos confiável**; sistemas legados respondem `application/octet-stream` para tudo

**Invariante — validação de conteúdo, e por que "parse válido" não basta.** Uma página de erro em HTML/XHTML pode ser XML bem-formado. A validação precisa, no mínimo:

- reconhecer PDF pela assinatura `%PDF-`;
- reconhecer XML pelo parse **e** por uma raiz plausível para o documento esperado;
- **rejeitar explicitamente** raiz `html` e corpo de página de erro, mesmo que o parse tenha sucesso;
- parsear **sem resolução de entidades externas** (a fonte é externa e não confiável);
- aplicar limite de tamanho de resposta;
- tratar conteúdo não reconhecido como **falha ruidosa**, nunca gravar em silêncio.

**Invariante:** conflito relevante entre a extensão indicada e o conteúdo real é falha ruidosa.

### 2.6 `Content-Disposition` — metadado gratuito

```
08431747000106-IFP13082026V01-001290363.xml
└─CNPJ────────┘ └┬┘└──┬───┘└┬┘ └───┬────┘
                 │    │     │      └─ id do documento, 9 dígitos com padding
                 │    │     └──────── versão (V01)
                 │    └────────────── data de entrega (ddMMyyyy)
                 └─────────────────── sigla da categoria (IFP = Informes Periódicos)
```

Isso resolve a ausência de `cnpjFundo` na listagem: **o CNPJ da entidade emissora chega no nome do arquivo servido**.

**Invariante:** o parse desse nome é *best-effort*, não caminho crítico. Se o formato mudar e o parse falhar, registrar erro visível e prosseguir usando o CNPJ que originou a busca — nunca interromper o pipeline por causa disso.

### 2.7 Fundo × classe (RCVM 175) — fato confirmado, não hipótese

A revisão 2 tratava isto como ponto a verificar. Está confirmado por duas vias independentes:

- **Regulatória:** a CVM adaptou o Fundos.NET à RCVM 175; documentos passam a ser enviados com o **CNPJ específico de cada entidade**, e a partir de 1º/7/2025 alguns documentos de FII (Informes Mensal, Trimestral e Anual) são enviados **pelas classes**. Fonte: comunicado da CVM sobre a atualização do Fundos.NET e o Ofício-Circular CVM/SSE 5/2024.
- **Empírica:** os registros já coletados trazem `fundoOuClasse: "Classe"`.

**Atenuante que calibra a severidade:** em **fundos monoclasse — a estrutura da maioria dos FIIs listados — o CNPJ do fundo e o da classe são o mesmo**. O desenho precisa *tolerar* N entidades por escopo monitorado; não deve assumir que N > 1 é o caso comum, nem exigir resolução completa da árvore para funcionar no caso simples.

**Consequência de desenho:** o objeto monitorado deixa de ser "um fundo" e passa a ser um **escopo** (seção 3).

---

## 3. Escopo monitorado

### 3.1 Modelo

**Invariante:** a unidade de configuração é o **escopo monitorado**, identificado pelo CNPJ de referência informado pelo usuário. Um escopo contém **uma ou mais entidades**, cada uma com seu próprio `id_fundosnet` e CNPJ.

| Situação | Entidades no escopo |
|---|---|
| Fundo monoclasse | 1 (fundo e classe compartilham o CNPJ) |
| Fundo multiclasse, CNPJ de fundo informado | O fundo e suas classes ativas |
| CNPJ de classe informado | Somente aquela classe |

**Invariante:** o usuário informa **um** CNPJ. A expansão em entidades é trabalho do robô, gravado no YAML para inspeção. O usuário nunca é obrigado a conhecer a estrutura de classes para cadastrar um fundo.

**Invariante — degradação graciosa:** se a expansão de classes falhar ou não encontrar nada, o escopo opera com a entidade única resolvida e registra `expansao: parcial`. Um fundo monoclasse jamais deve ficar bloqueado por causa de maquinaria de multiclasse.

### 3.2 Cadeia de resolução

Não existe fórmula que derive o `id` do Fundos.NET a partir de CNPJ ou Código CVM — é identificador interno opaco. Precisa ser resolvido por busca textual e **cacheado**.

```
CNPJ de referência (input)
  → cadastro CVM (registro_fundo.csv; registro_classe.csv quando necessário)
        → denominação social, código CVM, administrador, situação
  → listarFundos?term={denominação}
        → candidatos {id, text}  ← pode retornar fundo E classes (ver 9.3)
  → consulta por idFundo (l=1) em cada candidato
        → confirma existência e captura descricaoFundo exato
  → grava as entidades no YAML
```

**Invariante — ordem de preferência das fontes.** A expansão em entidades tenta primeiro o próprio `listarFundos`; só recorre a `registro_classe.csv` se o Fundos.NET não expuser as classes de forma utilizável. Cada dependência externa adicional é custo permanente de manutenção — introduza apenas se a via interna não resolver.

**Invariante:** `id_fundosnet` é estável, o nome não é. Toda decisão operacional se apoia no `id`; o nome é campo de exibição e de auditoria.

### 3.3 Validação da resolução via CNPJ do `Content-Disposition`

O salto `denominação → id_fundosnet` é textual. O CNPJ que volta no download fecha o circuito:

```
CNPJ da entidade → id_fundosnet → primeiro download
                                        ↓
                    CNPJ do Content-Disposition
                                        ↓
                        esperado == recebido ?
```

**Invariante:** a comparação é feita contra o CNPJ **da entidade consultada**, não contra o CNPJ de referência do escopo. Comparar o CNPJ de uma classe contra o CNPJ do fundo guarda-chuva produz falso positivo de divergência em fundos multiclasse.

**Invariante:** divergência confirmada (o CNPJ recebido não corresponde a nenhuma entidade do escopo) **não consolida** a resolução — é falha grave, com alerta explícito.

**Invariante:** essa validação aproveita o primeiro download que aconteceria de qualquer forma — não dispare download extra só para validar. Entidade recém-cadastrada sem nenhum documento fica `pendente de confirmação`, não bloqueada.

**Não** aplicar o mesmo rigor a `dataEntrega`: discrepância ali é ruído de formatação, registre sem bloquear.

### 3.4 Duas vias de entrada, com regras diferentes

| Via | Aceita | Desambiguação |
|---|---|---|
| CLI | CNPJ **ou** nome parcial | Interativa — lista candidatos, usuário escolhe |
| Edição manual do YAML | **Somente CNPJ** | Não há |

**Invariante:** a diferença existe porque o robô roda desatendido e nomes de FII colidem com frequência. Fuzzy match exige humano confirmando.

**Comportamento ao carregar o arquivo:**

- Entrada só com `cnpj` → resolve, expande entidades e **preenche no próprio arquivo**
- Entrada sem CNPJ e sem campos resolvidos → log de erro visível, escopo ignorado, demais seguem
- `situacao_cvm` revalidada a cada sync, para detectar fundos cancelados/liquidados

### 3.5 Normalização de CNPJ

**Invariante:** aceitar dígitos puros (`08431747000106`) e com máscara (`08.431.747/0001-06`). Preservar no arquivo a forma digitada pelo usuário; normalizar para dígitos puros em toda comparação e lookup. Nunca comparar CNPJ como string bruta entre fontes diferentes.

### 3.6 Arquivo único em YAML

**Decisão tomada:** um único arquivo, editável e auditável pelo humano, reescrito pelo robô com os dados resolvidos.

> **Alternativa considerada e rejeitada:** separar *intento* (só CNPJs, escrito pelo humano) de *estado* (cache interno, escrito pelo robô). Tecnicamente mais limpo, mas rejeitada: com dezenas de CNPJs sem os dados do fundo ao lado, a manutenção e a auditoria manual — propósito do arquivo — ficam inviáveis.

Consequências obrigatórias:

**Invariante — escrita atômica:** arquivo temporário + `rename` no mesmo filesystem.

**Invariante — proteção contra perda de edição humana.** Escrita atômica evita YAML truncado, mas não evita a corrida: o robô lê, o usuário edita e salva, o robô grava por cima da versão antiga, a edição some. Antes do `rename`, o robô compara o **hash do conteúdo atual em disco** com o hash do que carregou. Se mudou, **não substitui**: registra conflito visível e mantém a edição do usuário. `mtime` não é suficiente. Manter cópia rotativa do último YAML válido.

**Invariante:** a reescrita **não apaga comentários nem reordena arbitrariamente** o que o usuário escreveu. Em Python, `PyYAML` descarta comentários no dump; `ruamel.yaml` os preserva. A ferramenta é livre, a propriedade não é.

**Invariante:** CNPJ é tratado como **string** em todo o ciclo (load, comparação, dump). Sem aspas, parsers YAML podem comer o zero à esquerda.

**Invariante — autoridade sobre os campos:**

| Campo | Autoridade |
|---|---|
| `cnpj` (referência do escopo), `escopo`, `ticker` | Usuário |
| `entidades[]` e todos os campos resolvidos | Robô — cache inspecionável, sobrescrito no sync |

Forma ilustrativa, **não normativa**:

```yaml
escopos:
  # cadastro mínimo — o robô completa o resto
  - cnpj: "08.431.747/0001-06"

  # já resolvido (fundo monoclasse — uma entidade)
  - cnpj: "08431747000106"
    escopo: fundo_e_classes        # ou: somente_esta_entidade
    ticker: "HGBS11"               # anotação manual — o robô nunca preenche nem valida
    codigo_cvm: "..."
    denominacao_social: "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO..."
    situacao_cvm: "..."
    data_cadastro: "2026-08-14"
    entidades:
      - tipo: fundo_ou_classe
        cnpj: "08431747000106"
        id_fundosnet: 21348
        descricao_fundo_fnet: "HEDGE BRASIL SHOPPING FUNDO DE INVESTIMENTO IMOBILIÁRIO DE RESPONSABILIDADE LIMITADA"
        validado_em: "2026-08-14"
```

### 3.7 Fonte cadastral da CVM

**Invariante:** separar o refresh do cadastro CVM da consulta ao Fundos.NET. Baixar os CSVs uma vez por execução (ou por dia) e usá-los como snapshot durante o job — não tratar fonte estável como consulta individual por escopo.

**Invariante — indisponibilidade da CVM não para o monitoramento.** Falha ao obter o cadastro impede novos cadastros e revalidações; **escopos já resolvidos continuam operando com o último snapshot válido**. O snapshot registra a data de obtenção. Snapshot inválido ou incompleto **nunca** substitui o último válido.

---

## 4. Descoberta e sincronização

### 4.1 Consulta por entidade é o caminho primário

**Invariante:** a descoberta diária consulta `pesquisarGerenciadorDocumentosDados` **com `idFundo`**, uma vez por entidade monitorada, cobrindo a janela de retenção inteira:

```
para cada entidade:
    dataInicial = hoje - (N - 1)
    dataFinal   = hoje
    idFundo     = entidade.id_fundosnet
```

> **Nota de revisão — reversão consciente.** A revisão 2 adotou a listagem global (sem `idFundo`) porque o custo não cresce com o número de fundos monitorados e a paginação vira operação atômica. A revisão 3 desfaz essa escolha. O motivo: a listagem global **não devolve `idFundo` nem `cnpjFundo`** (2.3), o que obriga a rotear cada documento por igualdade exata de `descricaoFundo`. Isso produz um modo de falha **silencioso** em três situações distintas — renomeação de fundo, colisão de denominação entre entidades, e classes com denominação própria. Três remendos separados para um defeito de origem. Consultar por `idFundo` é determinístico por construção e elimina os três de uma vez.

**Custo comparado**, para F escopos, janela N = 7:

| Estratégia | Requisições/dia | Roteamento | Falha silenciosa |
|---|---|---|---|
| Global por `dataEntrega` | ~6 (≈540 doc/dia × 7 dias, `l` alto) | Texto | Sim |
| Por entidade | 1–2 × entidades | Determinístico | Não |

Com dezenas de entidades, a diferença é irrelevante em termos absolutos e ambas ficam muito abaixo de qualquer volume que pressione a origem. A escolha se justifica por determinismo, não por economia. O ponto de inflexão fica na casa das centenas de entidades monitoradas — fora do caso de uso.

### 4.2 A janela de consulta é sempre a janela de retenção

**Invariante:** toda execução consulta o intervalo `[hoje - (N-1), hoje]` por entidade. Não há intervalo incremental.

Isso resolve, com uma única consulta, quatro problemas que a revisão 2 tratava separadamente:

| Problema | Como some |
|---|---|
| Descoberta do que é novo | Documentos novos aparecem na janela |
| Recuperação após período offline | A janela já cobre todos os dias recuperáveis; além dela, os documentos seriam purgados de qualquer forma |
| Status mutável (documento entregue em 08 e cancelado em 12) | A linha é reencontrada a cada execução e os campos mutáveis são atualizados **sem rebaixar o arquivo** |
| Drift de paginação por offset | Cada consulta por entidade cabe em pouquíssimas páginas |

**Invariante:** documentos reencontrados atualizam `status`, `modalidade` e demais campos mutáveis no manifesto. **Nunca** disparam novo download — a idempotência é por `(id_documento, versao)`.

**Invariante — o que o manifesto promete sobre campos mutáveis:** `status` representa o **último estado observado dentro da janela de retenção**. Documento fora da janela conserva o último valor conhecido, sem garantia de atualização. Isso deve estar explícito para quem for consultar o manifesto.

### 4.3 Watermark

Com a janela fixa da 4.2, o watermark deixa de ser o insumo do cálculo do intervalo e passa a ser **registro de progresso e sinal de alerta**.

**Invariante:** o watermark por entidade registra a última varredura **concluída com sucesso**. Paginação interrompida não avança nada.

**Invariante — atomicidade lógica:** os documentos descobertos são persistidos no manifesto **antes** do avanço do watermark, na mesma transação. Coletar as páginas em memória (poucas dezenas de linhas por entidade) e fazer um único commit contendo os `upsert` e o avanço do watermark satisfaz isso sem manter transação aberta durante requisições HTTP.

**Uso do watermark:** se a distância entre o watermark e hoje exceder N dias, houve buraco não recuperável — registrar `WARNING` explícito, porque documentos daquele período nunca aparecerão. Perder o watermark não causa perda de dados: a próxima execução varre a janela inteira de qualquer forma.

### 4.4 Paginação

**Invariante:** ao paginar, comparar `recordsFiltered` da primeira e da última página. Se mudou durante a varredura, o conjunto se alterou e a entidade é revarrida. Deduplicação por `(id, versao)` cobre linhas repetidas; a revarredura cobre linhas **puladas**, que a deduplicação não detecta.

Dias já fechados têm conjunto estável de `dataEntrega`; o risco de deslocamento de offset se concentra no dia corrente e, com consulta por entidade, em páginas pequenas.

### 4.5 Auditoria pela listagem global

**Invariante:** periodicamente (frequência configurável, diária ou semanal), o robô executa **uma** varredura da listagem global do dia e verifica se há documentos cuja `descricaoFundo` case com escopos monitorados mas que **não** tenham sido capturados pela consulta por entidade.

Objetivo: detectar entidade nova de um escopo (classe criada), `id_fundosnet` obsoleto, ou mudança de comportamento da origem.

**Invariante:** a auditoria é **detectiva, não corretiva**. Ela gera alerta para revalidação do escopo; nunca roteia documento por igualdade de texto para dentro do acervo, nem serve de caminho de descoberta. Falha da auditoria não é falha do job.

### 4.6 Revalidação de nomes

**Invariante:** a defesa principal contra renomeação é a revalidação periódica de `descricao_fundo_fnet` **via `idFundo`** — determinística. "Vários dias sem documento" é sinal secundário, com muitos falsos positivos: há fundos que passam semanas sem publicar nada.

Como o roteamento não depende mais do nome (4.1), uma renomeação não detectada deixou de causar perda silenciosa de documentos. O nome desatualizado afeta apenas exibição, nomes de arquivo futuros e a auditoria da 4.5.

---

## 5. Armazenamento e ciclo de vida

### 5.1 Duas raízes, com propósitos diferentes

**Invariante:** o desenho separa **raiz de dados** (estado privado do robô) de **raiz de documentos** (acervo destinado a leitura humana, potencialmente exportado por SMB ou equivalente). Ambas configuráveis e independentes.

```
{raiz_dados}/                    ← privada; nunca compartilhada
  fundos.yaml
  fundos.yaml.bak
  manifesto.sqlite
  robo.lock
  cache-cvm/

{raiz_documentos}/               ← esta é a pasta compartilhada
  .tmp/                          ← temporários de download (.part)
  _inbox/
    2026-08-14.md
  2026-08-12/
  2026-08-13/
  2026-08-14/
    HGBS_Fato-Relevante_1277824_V01.pdf
```

**Invariante:** `manifesto.sqlite`, o lock de execução e os temporários de estado ficam em filesystem **local ao processo**. SQLite sobre SMB/NFS tem locking e durabilidade não confiáveis. A raiz de documentos pode estar em compartilhamento; a raiz de dados, não.

**Invariante:** os temporários de download (`.part`) ficam **dentro da raiz de documentos** (`.tmp/`), porque `rename` só é atômico dentro do mesmo filesystem. Não colocá-los na raiz de dados.

**Invariante:** o diretório de trabalho do robô não é assumido em lugar nenhum. Todo caminho vem de configuração; nenhum caminho pessoal ou absoluto é embutido no código.

### 5.2 Diretório por data

**Invariante — formato:** `yyyy-mm-dd`, com zero à esquerda, sempre. Sem variação por locale, sem `dd-mm-yyyy`, sem separador alternativo. Ordenação lexicográfica = ordenação cronológica é propriedade da qual a purga e a leitura humana dependem.

**Invariante — o diretório corresponde à `dataEntrega`**, não à data em que o robô baixou. Se a máquina ficou desligada de 12 a 14:

```
2026-08-12/   documentos publicados em 12
2026-08-13/   documentos publicados em 13
2026-08-14/   documentos publicados em 14
```

e **não** tudo despejado em `2026-08-14/`. Caso contrário a pergunta "o que houve naquele dia?" perde resposta.

### 5.3 Índice do que chegou hoje

O propósito do pipeline é "abro a pasta e vejo o que há de novo". Com arquivamento por `dataEntrega`, depois de um período offline os documentos novos ficam espalhados em datas passadas e o diretório de hoje pode aparecer vazio.

**Invariante:** o robô gera `_inbox/{yyyy-mm-dd}.md` (ou formato equivalente legível), correspondente à **data do download**, com links relativos para os arquivos nos diretórios de `dataEntrega`.

Índice gerado, e não symlinks: funciona em SMB e em Windows sem privilégio de criação de link, não duplica conteúdo, e não deixa links quebrados quando a purga roda. Os arquivos de índice seguem a mesma retenção N.

### 5.4 Nome do arquivo

Convenção: `{prefixo_entidade}_{categoria}_{id}_V{versao}.{ext}`

**Invariante:** o nome não carrega campos mutáveis de *documento* — `status`, `modalidade` e `situacao` vivem **somente no manifesto**. Caso contrário o nome físico passa a mentir e seria preciso renomear arquivos a cada mudança de status.

**Invariante:** `versao` faz parte do nome. Se uma reapresentação mantiver a mesma `id`, sem isso a v2 sobrescreveria a v1.

**Sobre `{prefixo_entidade}`:** o ticker é anotação editável e a denominação pode mudar, então o prefixo **não é identidade** — é uma **representação capturada no momento do download**. Arquivos antigos não são renomeados quando o usuário altera o ticker. A identidade está no manifesto e no par `(id, versao)` presente no próprio nome.

> **Alternativa considerada e rejeitada:** campo `alias_arquivo` atribuído uma vez e congelado, separado do `ticker`. Rejeitado: adiciona um campo que o usuário precisa entender e manter, para resolver uma inconsistência puramente cosmética em um acervo com retenção de dias.

- `{ext}` resolvido em runtime (2.5)
- Ordem de preferência do prefixo: `ticker` anotado → derivado da denominação → CNPJ
- **Invariante:** sanitizar todos os componentes para o filesystem — há acentos, barras, parênteses e espaços residuais nos campos de origem
- Documentos de Assembleia do mesmo fundo e dia podem diferir apenas por `especieDocumento`. Considerar incluí-lo no nome, ou aceitar que só o `id` os distingue

### 5.5 Máquina de estados do download

Filesystem e SQLite **não** formam transação atômica. Se o processo morrer entre o `rename` e o commit, existe arquivo válido sem registro — e a idempotência, sendo por manifesto, não o reconheceria.

**Invariante — protocolo recuperável:**

```
1. registrar (id, versao) como  descoberto
2. baixar para  {raiz_documentos}/.tmp/{id}_V{versao}.part
3. validar conteúdo (2.5) e calcular hash
4. rename para o destino definitivo
5. marcar  disponivel  no manifesto, com path e hash
```

**Invariante — reconciliação na inicialização:** todo registro em estado intermediário é reconciliado antes da varredura:

- destino definitivo existe e valida → consolidar como `disponivel` **sem rebaixar**; se o hash já era conhecido, comparar e alertar em caso de divergência;
- destino ausente → voltar à fila de download;
- `.part` órfão mais antigo que um limiar → remover.

**Invariante:** o download é idempotente. A checagem é por `(id_documento, versao)` no manifesto, complementada pela reconciliação acima — nunca por existência de arquivo isolada.

### 5.6 Purga

**Invariante — semântica de N:** `N` é o **número de datas mantidas, incluindo hoje**.

```
primeira_data_retida = hoje - (N - 1)
```

Para `N = 7` e hoje = 14/08: mantém 08/08 a 14/08. Purga, janela de consulta (4.2) e índice `_inbox` usam a mesma fronteira — caso contrário a descoberta baixa documentos que a purga apaga em seguida.

**Invariante:** o job de purga apaga diretórios anteriores à fronteira e seu conteúdo, sem gate, sem consultar o Pipeline B, sem depender de estado externo. `N` é configurável.

**Invariante:** a purga atualiza `data_purga` no manifesto em vez de apagar a linha. O histórico de que o documento existiu é barato e útil; o arquivo é que não é permanente.

### 5.7 Lock de execução

**Invariante:** duas instâncias do Pipeline A não executam simultaneamente sobre o mesmo estado. Cron pode disparar nova execução com a anterior ainda rodando. Lock em arquivo na raiz de dados, com detecção de lock órfão (processo morto), obrigatório.

### 5.8 Timezone

**Invariante:** `dataEntrega` vem sem timezone. Fixar `America/Sao_Paulo` no contrato operacional e usá-la para "hoje", data do diretório, fronteira de retenção, índice e watermark — **independentemente** da timezone do host ou do container. Isto não é configuração do usuário; é propriedade da fonte.

---

## 6. Manifesto

**Recomendação: SQLite.** O conjunto de necessidades — deduplicação, constraints, retry, consultas por data, watermark, versionamento, marcação de purga, estados intermediários — recria mal em JSONL ou CSV, que exigem varredura completa a cada execução.

**Invariante — separar identidade de histórico operacional.** Três responsabilidades distintas:

```
documentos              → identidade lógica, estado, rastreabilidade física
tentativas_download     → log append-only de tentativas, sucesso e falha
sync_estado             → watermark por entidade e último erro
```

Se `resultado` viver na linha do documento, cada nova tentativa sobrescreve a história anterior. E uma constraint de unicidade que inclua o status impede registrar duas falhas do mesmo documento — o que quebra justamente o fluxo de retry.

**Invariante:** a chave de dedupe em `documentos` é `(id_documento, versao)`.

Campos mínimos em `documentos`: `id_documento`, `versao`, `id_fundosnet`, `cnpj_entidade`, `descricao_fundo`, `categoria`, `tipo`, `especie`, `data_referencia`, `formato_data_referencia`, `data_entrega`, `modalidade`, `status`, `estado_local` (`descoberto` | `baixando` | `disponivel` | `falha` | `purgado`), `path`, `extensao`, `hash_conteudo`, `data_download`, `data_purga`, `visto_em`.

**Invariante:** `cnpj_entidade` e `id_fundosnet` registram a **entidade emissora**, não o escopo. A relação escopo → entidades vive no YAML; o manifesto guarda o fato observado.

O esquema exato, os índices e a estratégia de migração ficam a critério do agente.

---

## 7. Portabilidade e contrato operacional

O robô será executado inicialmente em uma máquina TrueNAS, com empacotamento em contêiner como possibilidade **posterior à validação**, e o projeto pode ser aberto.

**Invariante:** o desenho **não** pressupõe Docker, TrueNAS, systemd, cron, nem qualquer orquestrador. Executar o programa uma vez, do shell, com um arquivo de configuração, tem que funcionar. Todo o resto é empacotamento.

Consequências obrigatórias:

| Requisito | Detalhe |
|---|---|
| **Execução one-shot** | O modo de operação canônico é rodar, fazer o trabalho e sair com código de saída significativo. Um modo daemon/scheduler interno, se existir, é **opcional** e construído sobre o one-shot, nunca o contrário |
| **Configuração externa** | Raízes, `N`, frequência de auditoria, timeouts e limites vêm de arquivo de configuração e/ou variáveis de ambiente. Nada de caminho, CNPJ ou preferência pessoal embutido no código |
| **Logs** | Emitir em `stdout`/`stderr` por padrão (contêiner e systemd capturam), com opção de arquivo. Não exigir diretório de log para funcionar |
| **Sinais** | Encerrar de forma limpa em `SIGTERM`/`SIGINT`: liberar o lock, fechar o banco, deixar `.part` recuperável pela reconciliação (5.5) |
| **Permissões** | Arquivos e diretórios criados na raiz de documentos precisam ser legíveis pelo usuário que acessa o compartilhamento. `umask`/modo de criação configuráveis; nunca depender do default do host |
| **Sem privilégio** | Nada exige root, capabilities especiais ou criação de symlink |
| **Sem segredo** | A origem é pública e não autenticada. O projeto não deve introduzir credencial nenhuma |
| **Estado é o disco** | Toda a informação necessária para retomar está no YAML e no manifesto. Nada de estado vivo em memória entre execuções |

---

## 8. Condições de contorno de operação

**Invariante — isto é scraping de endpoint de UI, não uma API com contrato.** Sem garantia de estabilidade, versionamento ou SLA. Consequências obrigatórias:

- Rate limiting e backoff exponencial. Não paralelizar agressivamente
- `User-Agent` identificável, volume compatível com uso humano
- Retry com limite; falha persistente vai para o manifesto e não derruba o job
- Toda resposta validada antes de ser tratada como sucesso — **HTTP 200 com corpo HTML de erro é modo de falha real** neste tipo de sistema (ver 2.5)

**Invariante — validação de schema em dois níveis.** Campos críticos, cuja ausência ou mudança de tipo invalida o processamento (`id`, `versao`, `dataEntrega`, `dataReferencia`, `formatoDataReferencia`, `categoriaDocumento`, `descricaoFundo`, `status`), falham ruidosamente. Campos acessórios e sabidamente nullable não derrubam o registro. Sem essa distinção, uma mudança irrelevante no endpoint interrompe todas as entidades.

**Invariante — falha isolada não derruba o lote.** Uma entidade com configuração inválida ou erro pontual é registrada e pulada; as demais seguem. Vale para escopos, entidades e documentos.

**Invariante — tolerância a falhas não vira silêncio operacional:**

| Nível | Significado |
|---|---|
| `WARNING` | Falha transitória, retry previsto; buraco de watermark maior que a retenção |
| `ERROR` | Configuração inválida, escopo/entidade pulada, conflito de escrita no YAML — exige ação humana |
| `CRITICAL` | Contrato do Fundos.NET provavelmente mudou; divergência de CNPJ na validação — exige ação humana imediata |

---

## 9. Pontos em aberto — a verificar durante a implementação

O agente deve confirmar estes pontos empiricamente e ajustar o desenho conforme o resultado. Não assumir nenhum deles como resolvido.

### 9.1 Comportamento da listagem em reapresentações

Observado: `V2 RENDA IMOBILIÁRIA`, `id 1286620`, `versao 2`, modalidade `RE`. Apenas **uma** linha desse documento apareceu, o que *sugere* que a listagem exibe só a versão vigente — uma amostra não confirma.

**A verificar:** consultando esse fundo por `idFundo`, aparecem v1 e v2 ou só v2? O `id` da reapresentação é o mesmo do original ou novo?

**Por que importa:** se a v1 desaparece da listagem, o arquivo da v1 já baixado permanece em disco sem ser reencontrado — decidir se isso é aceitável (provavelmente sim, é justamente o histórico que se quer preservar) e como o manifesto marca a v1 como superada.

### 9.2 Limite de `l` (page length)

**A verificar:** o endpoint respeita `l=100`, `l=500`, ou trunca silenciosamente? Define a paginação tanto da consulta por entidade quanto da auditoria global.

**Como verificar sem confiar no valor devolvido:** comparar a contagem de itens retornados com `recordsFiltered` para um intervalo conhecidamente maior que o `l` pedido.

### 9.3 `listarFundos` expõe as classes?

**Decisivo para a seção 3.** Para um fundo comprovadamente multiclasse, `listarFundos?term={denominação}` retorna uma entrada só ou várias — fundo e classes, cada uma com `id` próprio?

- **Se retorna várias:** a expansão em entidades se resolve dentro do Fundos.NET, e `registro_classe.csv` fica dispensável
- **Se retorna uma só:** é preciso descobrir como o Fundos.NET associa a classe ao fundo, e só então recorrer ao cadastro da CVM

Verificar também se o `text` retornado permite distinguir fundo de classe.

### 9.4 CNPJ no `Content-Disposition` em fundo multiclasse

**A verificar:** para um documento enviado por uma classe, o CNPJ do nome do arquivo é o da classe ou o do fundo? Determina a comparação da 3.3.

Em fundo monoclasse a pergunta não se aplica — os CNPJs coincidem.

### 9.5 Ordenação estável na paginação

**A verificar:** o parâmetro `o[0][{campo}]` aceita ordenação por `id` e a mantém estável entre páginas? Ordenação determinística reduz o risco de linha pulada quando o conjunto muda durante a varredura, complementando a checagem de `recordsFiltered` da 4.4.

### 9.6 Estabilidade do `descricaoFundo`

**A verificar:** com que frequência entidades são renomeadas e se a listagem acompanha imediatamente. Determina a frequência da revalidação da 4.6 — que agora é questão de higiene, não de perda de dados.

### 9.7 Custo real da consulta por entidade

**A verificar durante a implementação:** com o número real de entidades monitoradas e `N` configurado, quantas requisições/dia o desenho da 4.1 gera, e se o tempo total de execução é compatível com o rate limiting da seção 8. Se o número de entidades crescer muito além da dezena, reavaliar o balanço entre consulta por entidade e listagem global — com a ressalva de que a global reintroduz o roteamento por texto.

---

## 10. Liberdade do implementador

A critério do agente: linguagem e runtime; estrutura de módulos; biblioteca HTTP, de YAML e de persistência; mecanismo de agendamento e empacotamento; estratégia de logging e observabilidade; framework de CLI; estratégia e cobertura de testes; concorrência, respeitado o rate limiting; esquema exato do banco, índices e migrações; formato exato do índice `_inbox`.

**Não é livre:** os invariantes marcados acima, o vocabulário/comportamento da seção 2 (verificação empírica contra o sistema real) e os requisitos de portabilidade da seção 7.

O agente é encorajado a propor melhorias ao desenho durante a implementação, especialmente ao resolver os pontos da seção 9 — desde que divergências dos invariantes sejam **explicitadas e justificadas, não silenciosas**.
