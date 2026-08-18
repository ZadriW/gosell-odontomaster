(() => {
    'use strict';

    const grid = document.getElementById('productGrid');
    if (!grid) return;
    const cards = Array.from(grid.querySelectorAll('.product-card'));
    const searchInput = document.getElementById('searchInput');
    const categoryChips = document.querySelectorAll('.category-chip[data-category]');
    const emptyState = document.getElementById('emptyState');
    const resultsInfo = document.getElementById('resultsInfo');
    const cartCountEl = document.getElementById('cartCount');
    const openCartBtn = document.getElementById('openCartBtn');

    // Dicionário id -> produto (injetado via Jinja no painel do vendedor).
    const FLOW = window.__TOTEM_FLOW__ || {};
    const STOCK_API = typeof window.__CATALOG_STOCK_API__ === 'string'
        ? window.__CATALOG_STOCK_API__
        : '';
    const PROMO_REFRESH_API = typeof window.__CATALOG_PROMO_REFRESH_API__ === 'string'
        ? window.__CATALOG_PROMO_REFRESH_API__
        : '';
    const productsById = new Map();
    (window.__PRODUCTS__ || []).forEach(p => {
        productsById.set(String(p.id), p);
    });

    const Cart = window.Cart;

    const state = {
        category: 'todos',
        query: '',
    };

    let searchTimer;

    let stockStaleNotice = false;
    let promoStaleNotice = false;

    /* -------------------------------------------------------------------- */
    /* Utilidades do catálogo                                               */
    /* -------------------------------------------------------------------- */

    function normalize(text) {
        return text
            .toString()
            .toLowerCase()
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '');
    }

    function getStock(card) {
        const n = parseInt(card.dataset.estoque, 10);
        return Number.isFinite(n) && n >= 0 ? n : 999;
    }

    function getBackorderLimitForCard(card) {
        const raw = card.dataset.backorderLimit;
        const n = parseInt(raw, 10);
        if (Number.isFinite(n)) return n;
        const p = productsById.get(String(card.dataset.id));
        return p ? (Number(p.backorder_limit) ?? -1) : -1;
    }

    function clampQty(card, raw) {
        const stock = getStock(card);
        const bl = getBackorderLimitForCard(card);
        let n = parseInt(String(raw), 10);
        if (!Number.isFinite(n)) n = 1;
        n = Math.max(1, n);
        if (window.__SELLER_BACKORDER__) {
            if (bl === 0) {
                if (stock <= 0) return 1;
                return Math.min(n, stock);
            }
            return n;
        }
        if (stock > 0) n = Math.min(n, stock);
        return n;
    }

    function formatStockLabel(n) {
        return `${n} no estoque.`;
    }

    const STOCK_TONES = ['product-card__stock--ok', 'product-card__stock--low', 'product-card__stock--empty'];

    function stockToneFromLevels(estoque, minStock) {
        const n = Math.max(0, Math.floor(Number(estoque)) || 0);
        const min = Math.max(0, Math.floor(Number(minStock)) || 0);
        if (n <= 0) return 'product-card__stock--empty';
        if (min > 0 && n < min) return 'product-card__stock--low';
        return 'product-card__stock--ok';
    }

    function syncStockDisplayTone(card) {
        const label = card.querySelector('[data-stock-display]');
        if (!label) return;
        const minRaw = parseInt(card.dataset.estoqueMin, 10);
        const minSafe = Number.isFinite(minRaw) && minRaw >= 0 ? minRaw : 0;
        const tone = stockToneFromLevels(getStock(card), minSafe);
        STOCK_TONES.forEach(c => label.classList.remove(c));
        label.classList.add(tone);
    }

    function syncBackorderInfo(card, backorderLimit) {
        const el = card.querySelector('[data-backorder-info]');
        if (!el) return;
        const limit = Number.isFinite(backorderLimit) ? backorderLimit : -1;
        card.dataset.backorderLimit = String(limit);

        if (limit >= 0) {
            el.classList.toggle('product-card__backorder-info--blocked', limit === 0);
            const icon = el.querySelector('i');
            if (icon) {
                icon.className = limit === 0
                    ? 'fa-solid fa-ban'
                    : 'fa-solid fa-truck-clock';
            }
            const msgEl = el.querySelector('[data-backorder-msg]');
            if (msgEl) {
                if (limit === 0) {
                    msgEl.textContent = 'Vendas futuras bloqueadas';
                } else {
                    msgEl.innerHTML = `<span data-backorder-limit-value>${limit}</span> vendas futuras disp.`;
                }
            }
            el.removeAttribute('hidden');
        } else {
            el.setAttribute('hidden', '');
        }
        syncBackorderBlockedUI(card, limit);
    }

    function syncBackorderBlockedUI(card, backorderLimit) {
        const limit = Number.isFinite(backorderLimit)
            ? backorderLimit
            : getBackorderLimitForCard(card);
        const stock = getStock(card);
        const blocked = window.__SELLER_BACKORDER__ && limit === 0 && stock <= 0;
        card.classList.toggle('product-card--backorder-blocked', blocked);

        const btn = card.querySelector('.product-card__button');
        if (btn) btn.disabled = blocked;

        const input = card.querySelector('.product-card__counter-input');
        if (input && window.__SELLER_BACKORDER__ && limit === 0) {
            const maxStock = Math.max(0, stock);
            input.setAttribute('max', String(maxStock > 0 ? maxStock : 1));
            input.disabled = maxStock <= 0;
        } else if (input) {
            input.disabled = false;
        }
    }

    function applyStockToCard(card, estoqueRaw, backorderLimitRaw) {
        const n = Math.max(0, Math.floor(Number(estoqueRaw)) || 0);
        card.dataset.estoque = String(n);
        const label = card.querySelector('[data-stock-display]');
        if (label) label.textContent = formatStockLabel(n);
        syncStockDisplayTone(card);
        const input = card.querySelector('.product-card__counter-input');
        if (input) {
            input.setAttribute('max', String(n));
            input.value = String(clampQty(card, input.value));
        }
        const p = productsById.get(String(card.dataset.id));
        if (p) p.estoque = n;
        if (backorderLimitRaw !== undefined) {
            const bl = Number(backorderLimitRaw);
            if (p) p.backorder_limit = Number.isFinite(bl) ? bl : -1;
            syncBackorderInfo(card, Number.isFinite(bl) ? bl : -1);
        } else {
            syncBackorderInfo(card, p ? (p.backorder_limit ?? -1) : -1);
        }
    }

    function escapeCatalogHtml(value) {
        const d = document.createElement('div');
        d.textContent = value == null ? '' : String(value);
        return d.innerHTML;
    }

    function formatCatalogPriceBRL(n) {
        const x = Number(n);
        if (!Number.isFinite(x)) return 'R$ 0,00';
        return `R$ ${x.toFixed(2).replace('.', ',')}`;

    }

    function renderPromoBadgeMarkup(product) {
        if (!product.em_promocao || !product.promo_badge) return '';
        return `<div class="product-card__promo-badge"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${escapeCatalogHtml(product.promo_badge)}</div>`;
    }

    function renderDeliveryBadgeMarkup(product) {
        const pendingN = Math.max(0, Math.floor(Number(product.pending_delivery_units)) || 0);
        if (pendingN <= 0) return '';
        return `
            <div class="product-card__delivery-badge" title="${pendingN} un. aguardando retirada">
                <span class="admin-badge admin-badge--warn admin-tx__delivery-badge" aria-label="Retirada pendente">
                    <i class="fa-solid fa-box-open" aria-hidden="true"></i>
                </span>
                <span class="admin-stock__delivery-count">${pendingN}</span>
            </div>
        `;
    }

    function syncCatalogBadges(card, product) {
        const promoRoot = card.querySelector('[data-promo-badge-root]');
        if (promoRoot) promoRoot.innerHTML = renderPromoBadgeMarkup(product);

        const deliveryRoot = card.querySelector('[data-delivery-badge-root]');
        if (deliveryRoot) deliveryRoot.innerHTML = renderDeliveryBadgeMarkup(product);
    }

    function renderPricingBlockMarkup(product) {
        const em = !!product.em_promocao;
        const po = Number(product.preco_original);
        const pp = Number(product.preco);
        const tipo = product.promo_tipo || '';

        // percent / fixed: preço unitário já reduzido — exibe riscado + novo preço.
        if (em && (tipo === 'percent' || tipo === 'fixed') && Number.isFinite(po) && po > pp + 0.001) {
            return `<div class="product-card__price-wrap"><span class="product-card__price-original">${formatCatalogPriceBRL(po)}</span><p class="product-card__price product-card__price--promo">${formatCatalogPriceBRL(pp)}</p></div>`;
        }

        // bogo / min_bundle / exact_bundle: desconto depende da quantidade — exibe preço
        // de lista + nome da promoção. O desconto só aparece quando a regra é atingida no carrinho.
        if (em && (tipo === 'bogo' || tipo === 'min_bundle' || tipo === 'exact_bundle')) {
            const nome = escapeCatalogHtml(product.promo_nome || '');
            return `<div class="product-card__price-wrap"><p class="product-card__price">${formatCatalogPriceBRL(pp)}</p>${nome ? `<span class="product-card__promo-name">${nome}</span>` : ''}</div>`;
        }

        return `<p class="product-card__price">${formatCatalogPriceBRL(pp)}</p>`;
    }

    function applyCatalogSnapshotToCard(card, p) {
        productsById.set(String(p.id), { ...p });

        const minRaw = Math.max(0, Math.floor(Number(p.estoque_minimo)) || 0);
        card.dataset.estoqueMin = String(minRaw);

        applyStockToCard(card, p.estoque, p.backorder_limit);

        card.dataset.preco = String(Number(p.preco) || 0);

        card.classList.toggle('product-card--promo', !!p.em_promocao);

        syncCatalogBadges(card, p);

        const priceRoot = card.querySelector('[data-catalog-pricing]');
        if (priceRoot) priceRoot.innerHTML = renderPricingBlockMarkup(p);
    }

    async function fetchCatalogPromoRefresh() {
        if (!PROMO_REFRESH_API) return;
        try {
            const res = await fetch(PROMO_REFRESH_API, { credentials: 'same-origin' });
            const T = window.TotemApiErrors;
            const data = T ? await T.parseJsonSafe(res) : await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(
                    T ? T.messageFromBadResponse(res, data) : 'Não foi possível atualizar preços e promoções.',
                );
            }
            const list = data.products;
            if (!Array.isArray(list)) return;
            const byId = new Map(list.map(row => [String(row.id), row]));
            cards.forEach(card => {
                const row = byId.get(String(card.dataset.id));
                if (!row) return;
                applyCatalogSnapshotToCard(card, row);
            });
            if (Cart && typeof Cart.syncPricesFromProductMap === 'function') {
                Cart.syncPricesFromProductMap(productsById);
            }
        } catch (err) {
            const T = window.TotemApiErrors;
            const msg = T ? T.formatCatchMessage(err) : String(err.message || err);
            if (!promoStaleNotice && resultsInfo && T) {
                promoStaleNotice = true;
                const hint = document.createElement('span');
                hint.className = 'catalog-stock-sync-warning';
                hint.textContent = ' • Preços/promoções ao vivo indisponíveis';
                hint.title = msg.replace(/\n\n/g, ' ');
                resultsInfo.appendChild(hint);
            }
        }
    }

    async function fetchCatalogStock() {
        if (!STOCK_API) return;
        try {
            const res = await fetch(STOCK_API, { credentials: 'same-origin' });
            const T = window.TotemApiErrors;
            const data = T ? await T.parseJsonSafe(res) : await res.json().catch(() => ({}));
            if (!res.ok) {
                throw new Error(
                    T ? T.messageFromBadResponse(res, data) : 'Não foi possível atualizar o estoque ao vivo.',
                );
            }
            const list = data.products;
            if (!Array.isArray(list)) return;
            const byId = new Map(list.map(row => [String(row.id), row]));
            cards.forEach(card => {
                const id = card.dataset.id;
                const row = byId.get(id);
                if (!row) return;
                applyStockToCard(card, row.estoque, row.backorder_limit);
                if (Object.prototype.hasOwnProperty.call(row, 'pending_delivery_units')) {
                    const p = productsById.get(id);
                    if (p) p.pending_delivery_units = row.pending_delivery_units;
                    if (p) syncCatalogBadges(card, p);
                }
            });
            if (Cart && typeof Cart.syncPricesFromProductMap === 'function') {
                Cart.syncPricesFromProductMap(productsById);
            }
        } catch (err) {
            const T = window.TotemApiErrors;
            const msg = T ? T.formatCatchMessage(err) : String(err.message || err);
            if (!stockStaleNotice && resultsInfo && T) {
                stockStaleNotice = true;
                const hint = document.createElement('span');
                hint.className = 'catalog-stock-sync-warning';
                hint.textContent = ' • Estoque ao vivo indisponível';
                hint.title = msg.replace(/\n\n/g, ' ');
                resultsInfo.appendChild(hint);
            }
        }
    }

    function applyFilters() {
        const query = normalize(state.query.trim());
        let visible = 0;

        cards.forEach(card => {
            const matchesCategory =
                state.category === 'todos'
                || card.dataset.category === state.category;
            const sku = card.dataset.sku || '';
            const matchesQuery =
                !query
                || normalize(card.dataset.name).includes(query)
                || (sku && normalize(sku).includes(query));
            const show = matchesCategory && matchesQuery;
            card.style.display = show ? '' : 'none';
            if (show) visible += 1;
        });

        emptyState.hidden = visible !== 0;
        resultsInfo.textContent =
            visible === 1
                ? '1 produto disponível'
                : `${visible} produtos disponíveis`;

        categoryChips.forEach(c => {
            c.classList.toggle('is-active', c.dataset.category === state.category);
        });
    }

    function updateCartBadge() {
        const total = Cart ? Cart.count() : 0;
        cartCountEl.textContent = total;
        cartCountEl.classList.toggle('is-visible', total > 0);
    }

    /* -------------------------------------------------------------------- */
    /* Filtros e busca                                                      */
    /* -------------------------------------------------------------------- */

    categoryChips.forEach(chip => {
        chip.addEventListener('click', e => {
            e.stopPropagation();
            state.category = chip.dataset.category;
            applyFilters();
        });
    });

    if (searchInput) {
        searchInput.addEventListener('input', event => {
            clearTimeout(searchTimer);
            const value = event.target.value;
            searchTimer = setTimeout(() => {
                state.query = value;
                applyFilters();
            }, 120);
        });
    }

    /* -------------------------------------------------------------------- */
    /* Toast — produto adicionado (canto superior direito)                   */
    /* -------------------------------------------------------------------- */

    const addToast = document.getElementById('catalogAddToast');
    let addToastTimer = null;

    function showAddToCartToast(message, variant = 'success') {
        if (!addToast) return;
        const textEl = addToast.querySelector('.catalog-toast__text');
        const iconEl = addToast.querySelector('.catalog-toast__icon i');
        if (textEl && message) textEl.textContent = message;
        if (iconEl) {
            iconEl.className = variant === 'warn'
                ? 'fa-solid fa-triangle-exclamation'
                : 'fa-solid fa-circle-check';
        }
        addToast.classList.toggle('catalog-toast--warn', variant === 'warn');
        addToast.classList.add('is-visible');
        addToast.setAttribute('aria-hidden', 'false');
        clearTimeout(addToastTimer);
        addToastTimer = setTimeout(() => {
            addToast.classList.remove('is-visible', 'catalog-toast--warn');
            addToast.setAttribute('aria-hidden', 'true');
            if (textEl) textEl.textContent = 'Produto adicionado ao carrinho!';
            if (iconEl) iconEl.className = 'fa-solid fa-circle-check';
        }, variant === 'warn' ? 4200 : 3200);
    }

    /* -------------------------------------------------------------------- */
    /* Contador no card e adição ao carrinho                                */
    /* -------------------------------------------------------------------- */

    grid.addEventListener('click', event => {
        const counterBtn = event.target.closest('.product-card__counter-btn');
        if (counterBtn) {
            const card = counterBtn.closest('.product-card');
            const input = card.querySelector('.product-card__counter-input');
            const action = counterBtn.dataset.action;
            const stock = getStock(card);
            let val = clampQty(card, input.value);

            if (action === 'inc') {
                const bl = getBackorderLimitForCard(card);
                if (window.__SELLER_BACKORDER__ && bl !== 0) {
                    val = val + 1;
                } else if (stock > 0) {
                    val = Math.min(val + 1, stock);
                }
            } else if (action === 'dec') {
                val = Math.max(1, val - 1);
            }
            input.value = String(val);
            return;
        }

        const button = event.target.closest('.product-card__button');
        if (!button || button.disabled) return;

        const card = button.closest('.product-card');
        const input = card.querySelector('.product-card__counter-input');
        const qty = clampQty(card, input.value);

        const id = button.dataset.id;
        const product = productsById.get(String(id));
        if (Cart && product) {
            const check = typeof Cart.canAdd === 'function'
                ? Cart.canAdd(product, qty)
                : { ok: true };
            if (!check.ok) {
                showAddToCartToast(check.reason || 'Não foi possível adicionar ao carrinho.', 'warn');
                return;
            }
            const added = Cart.add(product, qty);
            if (added) {
                showAddToCartToast();
            } else {
                showAddToCartToast(
                    'Não foi possível adicionar. Verifique o estoque disponível.',
                    'warn',
                );
                return;
            }
        }

        input.value = '1';

        button.classList.add('is-added');
        clearTimeout(button._addedTimer);
        button._addedTimer = setTimeout(() => {
            button.classList.remove('is-added');
        }, 900);
    });

    grid.addEventListener('change', event => {
        const input = event.target;
        if (!input.classList.contains('product-card__counter-input')) return;
        const card = input.closest('.product-card');
        input.value = String(clampQty(card, input.value));
    });

    grid.addEventListener('blur', event => {
        const input = event.target;
        if (!input.classList.contains('product-card__counter-input')) return;
        const card = input.closest('.product-card');
        input.value = String(clampQty(card, input.value));
    }, true);

    /* -------------------------------------------------------------------- */
    /* Drawer do carrinho                                                   */
    /* -------------------------------------------------------------------- */

    const drawer = document.getElementById('cartDrawer');
    const drawerBackdrop = document.getElementById('cartBackdrop');
    const drawerClose = document.getElementById('cartDrawerClose');
    const drawerItems = document.getElementById('cartDrawerItems');
    const drawerEmpty = document.getElementById('cartDrawerEmpty');
    const drawerCount = document.getElementById('cartDrawerCount');
    const drawerTotal = document.getElementById('cartDrawerTotal');
    const drawerCheckout = document.getElementById('cartDrawerCheckout');
    const drawerClear = document.getElementById('cartDrawerClear');
    const drawerBackorderWarning = document.getElementById('cartDrawerBackorderWarning');

    function openDrawer() {
        if (!drawer) return;
        drawer.classList.add('is-open');
        drawerBackdrop.classList.add('is-open');
        document.body.classList.add('cart-open');
        drawer.setAttribute('aria-hidden', 'false');
    }

    function closeDrawer() {
        if (!drawer) return;
        drawer.classList.remove('is-open');
        drawerBackdrop.classList.remove('is-open');
        document.body.classList.remove('cart-open');
        drawer.setAttribute('aria-hidden', 'true');
    }

    function renderCartItem(item) {
        const bundleMeta = window.PromoPricing && typeof window.PromoPricing.formatBundleQtyMeta === 'function'
            ? window.PromoPricing.formatBundleQtyMeta(item, Cart.formatBRL.bind(Cart))
            : '';
        const subtotal = Cart.formatBRL(item.subtotal != null ? item.subtotal : item.preco * item.quantidade);
        const unit = Cart.formatBRL(item.preco);
        const listUnit = Number(item.preco_lista) || Number(item.preco) || 0;
        const showOriginal = !bundleMeta && item.promo_aplicada && listUnit > Number(item.preco) + 0.001;
        const unitLine = showOriginal
            ? `<span class="line-item__price-original">${Cart.formatBRL(listUnit)}</span> ${unit}`
            : unit;
        const promoHint = item.promo_aplicada && item.promo_nome
            ? `<p class="line-item__promo"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${item.promo_nome}</p>`
            : '';
        const stock = Number(item.estoque);
        const missing = Number.isFinite(stock) ? item.quantidade - Math.max(0, stock) : 0;
        const bl = Number(item.backorder_limit);
        const backorderBlocked = window.__SELLER_BACKORDER__
            && Number.isFinite(bl)
            && bl === 0
            && missing > 0;
        const backorderHint = backorderBlocked
            ? `<p class="cart-item__backorder cart-item__backorder--blocked"><i class="fa-solid fa-ban" aria-hidden="true"></i> Vendas futuras bloqueadas — remova ou reduza a quantidade ao estoque (${Math.max(0, stock)} un.)</p>`
            : ((window.__SELLER_BACKORDER__ && missing > 0)
                ? `<p class="cart-item__backorder"><i class="fa-solid fa-box-open" aria-hidden="true"></i> ${missing} un. sem estoque — retirada posterior pelo cliente</p>`
                : '');
        return `
            <article class="cart-item" data-id="${item.id}">
                <div class="cart-item__image">
                    <img src="${item.imagem}" alt="${item.nome}" loading="lazy">
                </div>
                <div class="cart-item__info">
                    <span class="cart-item__category">${item.categoria || ''}</span>
                    <h3 class="cart-item__name">${item.nome}</h3>
                    ${item.sku ? `<p class="cart-item__sku">SKU ${item.sku}</p>` : ''}
                    <p class="cart-item__price">
                        ${bundleMeta
                            ? `${bundleMeta} &middot; <strong>${subtotal}</strong>`
                            : `${unitLine} un. &middot; <strong>${subtotal}</strong>`}
                    </p>
                    ${promoHint}
                    ${backorderHint}
                    <div class="cart-item__counter" role="group" aria-label="Quantidade">
                        <button type="button" class="cart-item__counter-btn" data-cart-action="dec" aria-label="Diminuir">
                            <i class="fa-solid fa-minus" aria-hidden="true"></i>
                        </button>
                        <span class="cart-item__counter-qty">${item.quantidade}</span>
                        <button type="button" class="cart-item__counter-btn" data-cart-action="inc" aria-label="Aumentar">
                            <i class="fa-solid fa-plus" aria-hidden="true"></i>
                        </button>
                    </div>
                </div>
                <div class="cart-item__side">
                    <button type="button" class="cart-item__remove" data-cart-action="remove" aria-label="Remover">
                        <i class="fa-solid fa-trash" aria-hidden="true"></i>
                    </button>
                </div>
            </article>
        `;
    }

    function renderDrawer() {
        if (!drawer) return;
        const totals = Cart.getTotals();
        const items = totals.items;

        drawerCount.textContent = totals.count;
        drawerTotal.textContent = Cart.formatBRL(totals.total);
        const violations = typeof Cart.getBackorderViolations === 'function'
            ? Cart.getBackorderViolations(items)
            : [];
        const hasViolations = violations.length > 0;
        if (drawerBackorderWarning) drawerBackorderWarning.hidden = !hasViolations;
        drawerCheckout.disabled = items.length === 0 || hasViolations;
        if (drawerClear) drawerClear.disabled = items.length === 0;

        if (items.length === 0) {
            drawerItems.innerHTML = '';
            drawerItems.hidden = true;
            drawerEmpty.hidden = false;
        } else {
            drawerEmpty.hidden = true;
            drawerItems.hidden = false;
            drawerItems.innerHTML = items.map(renderCartItem).join('');
        }
    }

    if (openCartBtn) {
        openCartBtn.addEventListener('click', () => {
            renderDrawer();
            openDrawer();
        });
    }
    if (drawerClose) drawerClose.addEventListener('click', closeDrawer);
    if (drawerBackdrop) drawerBackdrop.addEventListener('click', closeDrawer);

    if (drawerItems) {
        drawerItems.addEventListener('click', event => {
            const btn = event.target.closest('[data-cart-action]');
            if (!btn) return;
            const itemEl = btn.closest('.cart-item');
            const id = itemEl.dataset.id;
            const action = btn.dataset.cartAction;
            if (action === 'inc') Cart.increment(id);
            else if (action === 'dec') Cart.decrement(id);
            else if (action === 'remove') Cart.remove(id);
        });
    }

    if (drawerCheckout) {
        drawerCheckout.addEventListener('click', () => {
            if (Cart.isEmpty()) return;
            if (typeof Cart.hasBackorderViolations === 'function' && Cart.hasBackorderViolations()) {
                showAddToCartToast(
                    'Remova os produtos com vendas futuras bloqueadas para finalizar a compra.',
                    'warn',
                );
                return;
            }
            const payUrl = FLOW.payment || '/vendedor/pagamento';
            window.location.assign(payUrl);
        });
    }

    if (drawerClear) {
        drawerClear.addEventListener('click', () => {
            if (Cart.isEmpty()) return;
            if (!window.confirm('Remover todos os itens do carrinho?')) return;
            Cart.clear();
        });
    }

    /* -------------------------------------------------------------------- */
    /* Inicialização e listener global                                       */
    /* -------------------------------------------------------------------- */

    if (Cart) {
        if (typeof Cart.syncPricesFromProductMap === 'function') {
            Cart.syncPricesFromProductMap(productsById);
        }
        Cart.subscribe(() => {
            updateCartBadge();
            renderDrawer();
        });
    }

    const CATALOG_POLL_MS = 15000;

    applyFilters();
    updateCartBadge();
    cards.forEach(card => syncBackorderBlockedUI(card, getBackorderLimitForCard(card)));

    if (PROMO_REFRESH_API) {
        fetchCatalogPromoRefresh();
        setInterval(fetchCatalogPromoRefresh, CATALOG_POLL_MS);
    } else if (STOCK_API) {
        fetchCatalogStock();
        setInterval(fetchCatalogStock, CATALOG_POLL_MS);
    }
})();
