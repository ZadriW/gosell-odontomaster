# Totem Odonto Master

Plataforma web de **atendimento comercial** para a Odonto Master: vendedores registram vendas assistidas em **balcões (PC)**, **tablets** e **celulares**, com painel administrativo para gestão de eventos, estoque, promoções e financeiro.

Não há totens físicos nem autoatendimento público. Toda venda exige **login de vendedor** vinculado a um evento ativo.

---

## Visão geral

O sistema foi pensado para **feiras, congressos e ações comerciais odontológicas**, onde:

- O **administrador** prepara eventos, produtos, estoque, vendedores e promoções.
- O **vendedor** consulta o catálogo do evento, monta o carrinho, conduz o pagamento (cartão ou PIX) e confirma a venda com o código **AUT** da maquininha.
- Estoque, preços promocionais e transações são **calculados e auditados no servidor**.

A interface é responsiva e funciona em navegadores modernos em desktop, tablet e mobile.

---

## Funcionalidades

### Painel do vendedor (`/vendedor`)

| Área | Descrição |
|------|-----------|
| **Login** | Acesso por e-mail e senha; vendedor vinculado a um evento ativo. |
| **Venda / catálogo** | Busca, carrinho lateral, estoque ao vivo e promoções recalculadas (~15 s). |
| **Pagamento** | Resumo do pedido, dados do cliente (nome, CRO, forma de pagamento, parcelas). |
| **Confirmação AUT** | Pedido fica **pendente** até o vendedor informar o AUT; só então o estoque é baixado. |
| **Restauração de checkout** | Retomar pedido pendente interrompido (sessão expirada, navegador fechado etc.). |
| **Dashboard** | Visão rápida de vendas e pedidos recentes do vendedor. |
| **Estoque** | Consulta somente leitura do estoque do evento, com indicadores de situação. |
| **Movimentações** | Histórico de entradas, saídas e ajustes (leitura). |
| **Transações** | Listagem de vendas e pendências, com detalhes de itens e promoções aplicadas. |

### Painel administrativo (`/admin`)

| Área | Descrição |
|------|-----------|
| **Dashboard** | Indicadores gerais e atalhos. |
| **Eventos** | Criar, editar, arquivar e restaurar eventos; cor de identificação (badge). |
| **Estoque por evento** | Produtos do evento, entradas, saídas, ajustes, estoque mínimo, remoção. |
| **Importação** | Adicionar produtos por SKU/ID ou importar planilha `.xls` (com consulta ao Sankhya quando necessário). |
| **Vendedores** | Cadastro, edição, vínculo a eventos, histórico de transações. |
| **Promoções** | Por evento, com regras: desconto %, desconto fixo, compre X leve Y, **A partir de** (pacote mínimo), **Na compra de** (pacote exato). A chave **Descontos / Brindes** troca a página para os brindes. O brinde é um produto do estoque do evento ou um **brinde avulso** (cadastrado só no Go Sell, sem CODPROD: caneca, squeeze...). Regras: N unidades dos participantes (uma vez por pedido), cada kit de N unidades do mesmo produto (limite opcional por pedido) ou **valor do pedido** a partir de R$ X (pedido inteiro ou só os participantes; usa o valor cobrado, com desconto manual). |
| **Movimentações** | Histórico por evento, com exportação CSV. |
| **Transações** | Vendas por evento, estornos, detalhes com preço original e promoção. |
| **Financeiro** | Relatório por período (vendas, itens, totais) e exportação PDF. |
| **Biblioteca de produtos** | Catálogo global (SKU, preço, categoria, ativar/inativar). |
| **Estoque global** | Visão consolidada fora do contexto de um evento. |
| **Reiniciar sistema** | Restaura estado inicial (uso controlado). |

### Venda e pagamento

1. Vendedor adiciona itens ao carrinho (preços e promoções calculados no cliente e validados no servidor).
2. Checkout com dados do cliente e forma de pagamento.
3. Sistema cria **transação pendente** (estoque ainda não alterado).
4. Vendedor informa o **AUT** retornado pela maquininha.
5. Confirmação baixa estoque do evento e registra a venda.

Promoções são aplicadas na cotação do carrinho (`POST /api/carrinho/cotacao`) e na persistência da transação, garantindo o mesmo valor exibido e cobrado. Brindes (`database/gifts.py`) entram depois das promoções como linha a R$ 0,00 com `transaction_items.gift_rule_id`; o servidor descarta o brinde enviado pelo navegador e recalcula. O brinde de produto sai do estoque do evento (sem saldo, fica para retirada; se o produto não aceita venda futura, o brinde se limita ao saldo) e vai ao Sankhya com 100% de desconto. O brinde avulso grava `transaction_items.gift_item_id` sem `product_id`, é entregue na confirmação, não vai como item ao Sankhya (só na `OBSINTERNA`, "Brindes avulsos entregues, fora do Sankhya") e fica fora dos rankings de produto; o saldo é `gift_items.stock` menos o que saiu em vendas confirmadas, então estorno devolve sozinho. Os dois aparecem como "(BRINDE)" na nota.

### Integração Sankhya (ERP)

Acesso pelo gateway da 4R Tech (`sankhya_api.py`), com credenciais só no `.env`. Painel em **Admin → Integração ERP**, com o botão **Testar conexão** (faz um login e mostra o motivo exato de uma falha).

- **Catálogo:** o gateway expõe filas de alterações (manual FTAPI/Odontomaster): `GET /products/price` e `GET /products` devolvem lotes de 20 itens "Aguard. Integração", e `POST` no mesmo caminho com `{"códigos": [...]}` marca o lote como integrado. A sincronização esvazia primeiro a fila de preços (guardados em `erp_prices`) e depois a de cadastro (`DESCRPROD`, grupo, marca, unidade, imagem), confirmando cada lote só depois de gravá-lo. A fila de estoque (`/products/stock`) só alimenta `erp_stock` (lotes por local, para o pedido); o estoque do evento continua sendo do Totem. Cada fila roda mesmo que outra falhe. O vínculo é `products.erp_codprod`; o `id` local, estoque, eventos, vendas e promoções não mudam. Produto que some da lista continua ativo; sem preço, mantém o último; sem imagem, mantém a foto. Produto **novo** sem preço entra inativo e é ativado na sincronização que trouxer o preço.
- **Tipos de negociação:** `GET orders/payment` devolve a lista completa (não é fila) dos CODTIPVENDA liberados no portal: `{CODTIPVENDA, DESCRTIPVENDA, DHALTER}`. Fica em `erp_tipvenda` (código que some da lista vira inativo) e é atualizada junto com a sincronização do catálogo ou pelo botão "Atualizar do Sankhya" no painel. Com a lista consultada, a grade forma de pagamento → CODTIPVENDA (`erp_payment_types`) vira seleção só entre os liberados, com a descrição do Sankhya e "Preencher vazios pelos nomes"; pedido cujo código deixou de ser liberado para na fila antes do envio.
- **Pedidos:** toda venda confirmada entra na fila `erp_outbox` e é enviada em segundo plano (cliente via `clientes/create-update-geral`, com o CRO só com o número no campo `CRO`, que o gateway grava em `AD_CRO`; depois o pedido como orçamento). Endpoint `orders/create-order`. O cabeçalho leva `IDPEDIDO` = sequencial inteiro da venda (o campo no Sankhya é numérico; `transactions.erp_idpedido`, contador em `erp_counters` que o reset não volta; `TOTEM_IDPEDIDO_BASE` muda a faixa em bancos de teste) e `NUMAUT` = AUT registrado na confirmação do pagamento (texto; dinheiro e faturado levam o AUT interno `DINHEIRO`/`FATURADO`); cada item leva `SEQITEMPED` ("1", "2"...), `CODLOCALORIG` (local configurado no painel) e `CONTROLE` (lote, da fila `/products/stock`; `" "` para produto sem controle de lote). Como no Sankhya, cada unidade sai de um lote com saldo no local: o item é dividido em uma linha por lote (começando pelo de maior saldo, desconto repartido pela quantidade) e, sem saldo suficiente, o pedido fica com erro. Venda com retirada pendente leva no cabeçalho `OBSINTERNA` (observação interna) com uma linha por produto: `Itens com retirada pendente (Go Sell, pedido OM...):` + `CODPROD - nome - N un.`, com a situação no momento do envio. O que cada pedido enviado tirou de cada lote fica em `erp_stock_allocations` e é abatido até a sincronização seguinte trazer o saldo novo; cancelamento ou descarte devolve. Sem internet, a fila espera; recusas do ERP ficam com erro para o admin reenviar ou descartar. Login recusado ou `SANKHYA_API_URL` errado pausam a fila inteira por 5 minutos, sem gastar as tentativas das vendas ("Enviar agora" no painel tira a pausa). Estorno de venda já enviada gera um cancelamento na fila.
- **Vendas anteriores à integração** ficam como "não enviadas" e só vão ao Sankhya por ação no painel.
- A migração do banco faz um backup automático em `database/backups/` antes de rodar.

### Nota de retirada

Link público assinado (`/nota/<pedido>`) para o cliente visualizar o comprovante da compra (token na URL).

### Outros

- **Modo escuro** independente por login (admin e vendedor).
- **CSRF** em formulários; sessões com cookies HTTP-only.
- **Polling** de estoque e promoções nas telas de venda e listagens.
- **Timeout de inatividade** nas telas de checkout.

---

## Stack técnica

| Camada | Tecnologia |
|--------|------------|
| Back-end | Python 3, Flask 3, Flask-WTF (CSRF) |
| Banco | SQLite (`database/totem.sqlite3`) |
| Front-end | HTML (Jinja2), CSS, JavaScript (sem framework) |
| Servidor WSGI | Gunicorn (produção) |
| Integração | Sankhya (gateway 4R Tech) |

---

## Estrutura do projeto

```
Totem/
├── app.py                  # Aplicação Flask (rotas, auth, APIs)
├── main.py                 # Entrada WSGI (gunicorn main:app)
├── totem_env.py            # Carrega .env / totem.env
├── sankhya_api.py          # Cliente HTTP do Sankhya (gateway 4R Tech)
├── erp_sync.py             # Sincronização do catálogo e fila de pedidos do ERP
├── receipt_tokens.py       # Tokens assinados da nota de retirada
├── requirements.txt
├── database/               # Camada SQLite (schema, CRUD, promoções, transações)
├── data/                   # Dados auxiliares (ex.: categorias)
├── static/
│   ├── css/
│   ├── js/
│   └── images/
└── templates/
    ├── admin/              # Painel administrativo
    ├── seller/             # Painel do vendedor
    ├── partials/           # Fragmentos reutilizáveis
    └── macros/             # Macros Jinja (transações, badges etc.)
```

---

## Como executar (desenvolvimento)

Requisitos: **Python 3.11+** (ou versão compatível com o ambiente do projeto).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copie `.env.example` para `.env` e preencha as variáveis necessárias (veja abaixo).

```powershell
python app.py
```

Abra no navegador:

| URL | Descrição |
|-----|-----------|
| http://localhost:5000/ | Página inicial (links para login admin/vendedor) |
| http://localhost:5000/admin | Painel administrativo |
| http://localhost:5000/vendedor | Painel do vendedor |

Rotas antigas de catálogo/pagamento público (`/catalogo`, `/pagamento`) redirecionam para a home — a venda ocorre apenas com vendedor autenticado.

### Produção (exemplo)

```bash
gunicorn -w 1 -b 0.0.0.0:8000 main:app
```

Com SQLite, use **um worker** (`-w 1`) para evitar conflitos de escrita. Para múltiplos workers, migre o banco para PostgreSQL.

---

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `SANKHYA_API_KEY` | Para o ERP | Chave hexadecimal do caminho do gateway |
| `SANKHYA_LOGIN` / `SANKHYA_PASSWORD` | Para o ERP | Usuário do Sankhya usado pela integração |
| `SANKHYA_API_URL` | Não | Base do gateway (padrão: 4R Tech `portal-repres`), ex.: `https://<host>/ftapi/v1/portal-repres`. Sem `/autenticacao` nem a chave: o sistema acrescenta os dois |
| `SANKHYA_PRODUCT_FILTER_PARAM` | Não | Parâmetro que filtra `/products` por `CODPROD` (busca avulsa) |
| `TOTEM_IDPEDIDO_BASE` | Não | Somado ao sequencial do IDPEDIDO (faixa própria para banco de testes) |
| `TOTEM_ERP_WORKER` | Não | `0` desliga o envio automático da fila (testes) |
| `TOTEM_SECRET_KEY` | **Sim em produção** | Chave de sessão Flask e assinatura de notas |
| `TOTEM_DEBUG` | Não | `1` liga o debugger Werkzeug. **Deixe desligado** em evento/produção. |
| `TOTEM_BIND` | Não | Host do `python app.py` (padrão `0.0.0.0` na LAN). |
| `TOTEM_COOKIE_SECURE` | HTTPS | `1` marca cookies `Secure` (obrigatório atrás de HTTPS). |
| `TOTEM_ADMIN_USER` | Opcional | Usuário do admin (padrão no código — **altere em produção**) |
| `TOTEM_ADMIN_PASS` | Opcional | Senha do admin |
| `TOTEM_SELLER_NAME` | Opcional | Nome da conta vendedor inicial (seed) |
| `TOTEM_SELLER_EMAIL` | Opcional | E-mail da conta vendedor inicial |
| `TOTEM_SELLER_PASS` | Opcional | Senha da conta vendedor inicial |

Arquivos lidos na inicialização (sem sobrescrever variáveis já definidas no sistema): `.env`, `totem.env`.

---

## Rotas principais

### Público

| Rota | Descrição |
|------|-----------|
| `/` | Boas-vindas |
| `/nota/<pedido>` | Comprovante de retirada (token assinado) |

### Vendedor (autenticado)

| Rota | Descrição |
|------|-----------|
| `/vendedor/venda` | Catálogo e carrinho |
| `/vendedor/pagamento` | Resumo e checkout |
| `/vendedor/pagamento/aguardando` | Informar AUT e confirmar |
| `/vendedor/dashboard` | Dashboard |
| `/vendedor/estoque` | Estoque do evento |
| `/vendedor/movimentacoes` | Movimentações |
| `/vendedor/transacoes` | Transações |
| `/api/carrinho/cotacao` | Cotação promocional do carrinho (JSON) |

### Admin (autenticado)

| Rota | Descrição |
|------|-----------|
| `/admin/eventos` | Gestão de eventos |
| `/admin/eventos/<id>/estoque` | Estoque do evento |
| `/admin/eventos/<id>/promocoes` | Promoções (`?modo=brindes` para os brindes) |
| `/admin/eventos/<id>/promocoes/brindes/novo` | Novo brinde (edição em `/brindes/<id>`) |
| `/admin/eventos/<id>/transacoes` | Vendas do evento |
| `/admin/eventos/<id>/movimentacoes` | Movimentações |
| `/admin/financeiro` | Relatório financeiro |
| `/admin/vendedores` | Vendedores |
| `/admin/produtos` | Biblioteca de produtos |

---

## Modelo de dados (resumo)

- **products** — catálogo global (SKU, preço, categoria, imagem, estoque de referência).
- **events** / **event_products** / **event_sellers** — operação por evento (estoque e equipe separados).
- **promotions** / **promotion_products** — regras promocionais por evento.
- **gift_rules** / **gift_rule_products** — brindes por evento (regra `min_qty`/`kit`/`min_total`, produto ou brinde avulso, quantidade, limite por pedido).
- **gift_items** — brindes avulsos por evento (nome, quantidade disponível; vazio = sem limite).
- **transactions** / **transaction_items** — pedidos com snapshot de preços e promoções.
- **stock_movements** — auditoria de alterações de estoque (global e por evento).

---

## Licença e uso

Projeto interno Odonto Master. Configure credenciais e `TOTEM_SECRET_KEY` antes de expor o sistema à internet.
