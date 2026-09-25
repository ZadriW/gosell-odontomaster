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
| **Importação** | Adicionar produtos por SKU/ID ou importar planilha `.xls` (com consulta à Wake quando necessário). |
| **Vendedores** | Cadastro, edição, vínculo a eventos, histórico de transações. |
| **Promoções** | Por evento, com regras: desconto %, desconto fixo, compre X leve Y, **A partir de** (pacote mínimo), **Na compra de** (pacote exato). |
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

Promoções são aplicadas na cotação do carrinho (`POST /api/carrinho/cotacao`) e na persistência da transação, garantindo o mesmo valor exibido e cobrado.

### Integração Wake Commerce

Consulta **on-demand** à Storefront API (GraphQL) por SKU ao cadastrar ou importar produtos que ainda não existem no catálogo local. Token via variável de ambiente `WAKE_TOKEN`. Não há sincronização em massa automática do catálogo.

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
| Integração | Wake Commerce Storefront API |

---

## Estrutura do projeto

```
Totem/
├── app.py                  # Aplicação Flask (rotas, auth, APIs)
├── main.py                 # Entrada WSGI (gunicorn main:app)
├── totem_env.py            # Carrega .env / totem.env
├── wake_api.py             # Cliente Wake Commerce (GraphQL)
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
| `WAKE_TOKEN` | Para importação Wake | Token TCS-Access-Token da Storefront API |
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
| `/admin/eventos/<id>/promocoes` | Promoções |
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
- **transactions** / **transaction_items** — pedidos com snapshot de preços e promoções.
- **stock_movements** — auditoria de alterações de estoque (global e por evento).

---

## Licença e uso

Projeto interno Odonto Master. Configure credenciais e `TOTEM_SECRET_KEY` antes de expor o sistema à internet.
