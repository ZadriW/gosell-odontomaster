(() => {
    'use strict';

    const PRODUCT_POLL_MS = 5000;
    const LIST_POLL_MS = 15000;
    const PROMO_ICON_FALLBACK_TITLE = 'Produto com promoção ativa neste evento';

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[char]));
    }

    const fetchJsonOpts = {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
    };

    function mergeHeadersForMethod(method, headers) {
        var T = window.TotemApiErrors;
        if (T && typeof T.csrfFetchHeaders === 'function') {
            return T.csrfFetchHeaders(method, Object.assign({}, headers || {}));
        }
        return Object.assign({}, headers || {});
    }

    function badge(status) {
        return `<span class="admin-badge admin-badge--${escapeHtml(status.kind)}">${escapeHtml(status.label)}</span>`;
    }

    function updateStockStatusCell(row, product) {
        const statusCell = row.querySelector('[data-stock-status]');
        if (!statusCell || !product.status) return;
        const statusBadge = statusCell.querySelector('[data-stock-status-badge]');
        if (statusBadge) {
            statusBadge.className = `admin-badge admin-badge--${product.status.kind}`;
            statusBadge.textContent = product.status.label;
        }
        const promoBadge = statusCell.querySelector('[data-stock-promo-badge]');
        const promoBadgeText = statusCell.querySelector('[data-stock-promo-badge-text]');
        if (promoBadge && promoBadgeText && Object.prototype.hasOwnProperty.call(product, 'active_promo')) {
            const promoName = String(product.promo_name ?? '').trim();
            if (product.active_promo && promoName) {
                promoBadge.removeAttribute('hidden');
                promoBadgeText.textContent = promoName;
            } else {
                promoBadge.setAttribute('hidden', '');
                promoBadgeText.textContent = '';
            }
        }
    }

    function flash(message, category = 'success') {
        const main = document.querySelector('.admin-main');
        if (!main || !message) return;
        const wrap = main.querySelector('.admin-flashes') || document.createElement('div');
        wrap.className = 'admin-flashes';
        if (!wrap.parentElement) main.prepend(wrap);
        const icon = category === 'success' ? 'fa-circle-check' : 'fa-triangle-exclamation';
        const el = document.createElement('div');
        el.className = `admin-flash admin-flash--${category}`;
        el.setAttribute('role', 'status');
        el.innerHTML = `<i class="fa-solid ${icon}" aria-hidden="true"></i><span>${escapeHtml(message)}</span>`;
        wrap.appendChild(el);
        setTimeout(() => el.remove(), 4500);
    }

    function eventBadgeInline(movement) {
        const bg = movement.event_badge_bg;
        const fg = movement.event_badge_fg;
        if (!bg || !fg) return '';
        return ` style="background-color:${escapeHtml(bg)};color:${escapeHtml(fg)};"`;
    }

    function eventCell(movement) {
        const eid = movement.event_id;
        const ename = String(movement.event_name ?? '').trim();
        const hasId = eid !== undefined && eid !== null && eid !== '';
        const attrStyle = eventBadgeInline(movement);
        if (hasId && ename) {
            return `<span class="admin-mov__event-cell"><span class="admin-nav__event-badge admin-mov__event-badge"${attrStyle} title="Evento #${escapeHtml(eid)}">${escapeHtml(ename)}</span></span>`;
        }
        if (hasId) {
            return `<span class="admin-mov__event-cell"><span class="admin-mov__event-unknown" title="Evento #${escapeHtml(eid)} (cadastro indisponível)">#${escapeHtml(eid)}</span></span>`;
        }
        return '<span class="admin-mov__event-cell"><span class="admin-mov__event-none" title="Movimentação no estoque global do catálogo (sem evento)">—</span></span>';
    }

    function renderProductMovementRow(movement, { withEvent = false } = {}) {
        const reasonParts = [];
        const ref = String(movement.reference || '').trim();
        const txUrl = String(movement.tx_url || '').trim();
        if (ref) {
            if (txUrl) {
                reasonParts.push(
                    `<a href="${escapeHtml(txUrl)}" class="admin-mov__order-link"`
                    + ` title="Abrir pedido ${escapeHtml(ref)} no histórico de transações"`
                    + ` aria-label="Abrir pedido ${escapeHtml(ref)} no histórico de transações">`
                    + `<code>${escapeHtml(ref)}</code></a>`
                );
            } else {
                reasonParts.push(`<code>${escapeHtml(ref)}</code>`);
            }
        }
        reasonParts.push(escapeHtml(movement.reason || '-'));
        const rowClass = withEvent ? 'admin-mov__row admin-mov__row--with-event' : 'admin-mov__row';
        const pendingAttr = movement.is_pending_delivery || movement.movement_type === 'pendente'
            ? ' data-pending-delivery'
            : '';

        return `
            <div class="admin-mov__wrapper" data-movement-id="${escapeHtml(movement.id)}"${pendingAttr}>
                <div class="${rowClass}" role="row">
                    <span>${escapeHtml(movement.created_at_display)}</span>
                    <span class="admin-mov__type-cell">
                        <span class="admin-badge admin-mov__badge admin-mov__badge--${escapeHtml(movement.movement_type)}">
                            ${escapeHtml(movement.movement_label)}
                        </span>
                    </span>
                    ${withEvent ? eventCell(movement) : ''}
                    <span class="admin-table__col--num admin-mov__delta admin-mov__delta--${escapeHtml(movement.delta_kind)}">
                        ${escapeHtml(movement.delta_display)}
                    </span>
                    <span class="admin-table__col--num"><strong>${escapeHtml(movement.balance_after)}</strong></span>
                    <span class="admin-mov__reason">${reasonParts.join(' ')}</span>
                    <span class="admin-mov__user-cell">
                        ${escapeHtml(movement.created_by_display || movement.created_by || '-')}
                    </span>
                </div>
            </div>
        `;
    }

    function syncPendingDeliveryMovements(table, movements) {
        if (!table) return;
        const head = table.querySelector('.admin-table__head');
        if (!head) return;
        const pending = (movements || []).filter(
            m => m && (m.is_pending_delivery || m.movement_type === 'pendente'),
        );
        const ids = new Set(pending.map(m => String(m.id)));
        table.querySelectorAll('.admin-mov__wrapper[data-pending-delivery]').forEach(el => {
            if (!ids.has(String(el.dataset.movementId))) el.remove();
        });
        const withEvent = movementsTableWithEvent(table);
        const toAdd = pending.filter(m => {
            const id = String(m.id).replace(/"/g, '');
            return !table.querySelector(`.admin-mov__wrapper[data-movement-id="${id}"]`);
        });
        if (!toAdd.length) return;
        table.querySelector('.admin-empty')?.remove();
        head.insertAdjacentHTML(
            'afterend',
            toAdd.map(movement => renderProductMovementRow(movement, { withEvent })).join(''),
        );
    }

    function readLatestMovementIdFromDom(table) {
        let max = 0;
        if (!table) return max;
        table.querySelectorAll('[data-movement-id]').forEach(el => {
            const id = Number(el.dataset.movementId);
            if (Number.isFinite(id) && id > max) max = id;
        });
        return max;
    }

    const productMovementsLatestByTable = new WeakMap();

    function movementsTableWithEvent(table) {
        const head = table.querySelector('.admin-table__head');
        return Boolean(
            table.hasAttribute('data-movements-with-event')
            || head?.classList.contains('admin-mov__row--with-event'),
        );
    }

    function movementsTableCap(table) {
        const raw = Number(table.dataset.movementsCap);
        if (Number.isFinite(raw) && raw > 0) return raw;
        return 120;
    }

    function mergeProductMovements(table, movements) {
        if (!table) return;

        const head = table.querySelector('.admin-table__head');
        if (!head) return;

        let prevLatest = productMovementsLatestByTable.get(table);
        if (prevLatest === undefined) {
            prevLatest = readLatestMovementIdFromDom(table);
            productMovementsLatestByTable.set(table, prevLatest);
        }

        const list = movements || [];
        const nextLatest = list.reduce(
            (max, movement) => Math.max(max, Number(movement.id) || 0),
            prevLatest,
        );

        if (nextLatest <= prevLatest) return;

        const withEvent = movementsTableWithEvent(table);
        const newItems = [];
        for (let i = 0; i < list.length; i += 1) {
            const movement = list[i];
            const mid = Number(movement.id);
            if (!Number.isFinite(mid) || mid <= prevLatest) break;
            if (table.querySelector(`.admin-mov__wrapper[data-movement-id="${mid}"]`)) continue;
            newItems.push(movement);
        }

        productMovementsLatestByTable.set(table, nextLatest);
        if (!newItems.length) return;

        table.querySelector('.admin-empty')?.remove();
        head.insertAdjacentHTML(
            'afterend',
            newItems.map(movement => renderProductMovementRow(movement, { withEvent })).join(''),
        );

        const trimCap = movementsTableCap(table);
        while (table.querySelectorAll('.admin-mov__wrapper').length > trimCap) {
            const wrappers = table.querySelectorAll('.admin-mov__wrapper');
            wrappers[wrappers.length - 1].remove();
        }

        bumpMovementsSummary(table, newItems.length);
    }

    function bumpMovementsSummary(table, addedCount) {
        if (!addedCount) return;
        const section = table.closest('section') || table.parentElement;
        if (!section) return;
        const summary = section.querySelector('.admin-pagination__summary');
        if (!summary) return;
        const strongs = summary.querySelectorAll('strong');
        if (strongs.length < 2) return;
        const rangeEl = strongs[0];
        const totalEl = strongs[1];
        const total = Number(String(totalEl.textContent || '').replace(/\D/g, ''));
        if (!Number.isFinite(total)) return;
        const nextTotal = total + addedCount;
        totalEl.textContent = String(nextTotal);
        const rangeMatch = String(rangeEl.textContent || '').match(/(\d+)\s*[–-]\s*(\d+)/);
        if (!rangeMatch) return;
        const from = Number(rangeMatch[1]);
        let to = Number(rangeMatch[2]) + addedCount;
        const cap = movementsTableCap(table);
        if (Number.isFinite(cap)) to = Math.min(to, cap, nextTotal);
        rangeEl.textContent = `${from}–${to}`;
    }

    function isEditingLiveStockForm(root) {
        const el = document.activeElement;
        if (!el || !root.contains(el)) return false;
        const form = el.closest('[data-live-stock-form]');
        if (!form || !root.contains(form)) return false;
        return el.matches('input:not([type="hidden"]), textarea, select');
    }

    function updateProductView(payload, options = {}) {
        const product = payload.product;
        const root = document.querySelector('[data-admin-product]');
        if (!root || !product) return;

        const forceInputSync = options.forceInputSync === true;
        const skipInputSync = !forceInputSync && isEditingLiveStockForm(root);

        const status = root.querySelector('[data-product-status]');
        const promoNameEl = root.querySelector('[data-product-promo-name]');
        const stock = root.querySelector('[data-product-stock]');
        const minStock = root.querySelector('[data-product-min-stock]');
        const backorderLimit = root.querySelector('[data-product-backorder-limit]');
        const stockValue = root.querySelector('[data-product-stock-value]');
        const minInput = root.querySelector('input[name="min_stock"]');
        const backorderLimitInput = root.querySelector('input[name="backorder_limit"]');
        const exitInput = root.querySelector('input[name="quantity"][max]');
        const exitButton = root.querySelector('.admin-card--saida button[type="submit"]');
        const adjustInput = root.querySelector('input[name="new_stock"]');
        const activeForm = root.querySelector('.admin-product__toggle');
        const movementsTable =
            root.querySelector('[data-product-movements]') ||
            root.parentElement.querySelector('[data-product-movements]');

        if (status) status.innerHTML = badge(product.status);
        if (promoNameEl && Object.prototype.hasOwnProperty.call(product, 'em_promocao')) {
            const promoNome = String(product.promo_nome ?? '').trim();
            if (product.em_promocao && promoNome) {
                promoNameEl.textContent = promoNome;
                promoNameEl.removeAttribute('hidden');
            } else {
                promoNameEl.textContent = '';
                promoNameEl.setAttribute('hidden', '');
            }
        }
        if (stock) stock.innerHTML = `<strong>${escapeHtml(product.estoque)}</strong> un.`;
        if (minStock) minStock.textContent = `${product.estoque_minimo} un.`;
        if (backorderLimit && Object.prototype.hasOwnProperty.call(product, 'backorder_limit')) {
            const limitVal = Number(product.backorder_limit);
            if (limitVal === 0) {
                backorderLimit.textContent = 'Bloqueado (0 un.)';
            } else if (Number.isFinite(limitVal) && limitVal > 0) {
                backorderLimit.textContent = `${limitVal} un.`;
            } else {
                backorderLimit.textContent = 'Sem limite';
            }
        }
        if (stockValue) stockValue.textContent = product.stock_value_display;
        if (!skipInputSync && minInput) {
            minInput.value = product.estoque_minimo;
        }
        if (!skipInputSync && backorderLimitInput) {
            const limitVal = Number(product.backorder_limit);
            backorderLimitInput.value = Number.isFinite(limitVal) && limitVal > 0 ? limitVal : 0;
        }
        /* Inventário: não sobrescrever no poll (evita apagar rascunho); só alinhar ao servidor após POST com forceInputSync. */
        if (forceInputSync && adjustInput) {
            adjustInput.value = product.estoque;
        }
        if (exitInput) exitInput.max = product.estoque;
        if (exitButton) exitButton.disabled = product.estoque <= 0;
        if (activeForm) {
            const hidden = activeForm.querySelector('input[name="active"]');
            const button = activeForm.querySelector('button[type="submit"]');
            if (hidden) hidden.value = product.ativo ? '0' : '1';
            if (button) {
                button.className = product.ativo
                    ? 'admin-btn admin-btn--ghost admin-btn--danger'
                    : 'admin-btn admin-btn--ghost';
                button.innerHTML = product.ativo
                    ? '<i class="fa-solid fa-eye-slash" aria-hidden="true"></i> Desativar produto'
                    : '<i class="fa-solid fa-eye" aria-hidden="true"></i> Ativar produto';
                if (product.ativo) {
                    button.setAttribute('data-confirm', 'Desativar este produto? Ele deixará de aparecer no totem.');
                } else {
                    button.setAttribute('data-confirm', 'Ativar este produto no totem?');
                }
            }
        }
        if (movementsTable && movementsTable.hasAttribute('data-movements-live')) {
            mergeProductMovements(movementsTable, payload.movements || []);
            syncPendingDeliveryMovements(movementsTable, payload.movements || []);
        } else if (movementsTable && forceInputSync) {
            /* Após POST local, atualiza o histórico mesmo sem poll contínuo. */
            mergeProductMovements(movementsTable, payload.movements || []);
            syncPendingDeliveryMovements(movementsTable, payload.movements || []);
        }
    }

    async function refreshProduct() {
        const root = document.querySelector('[data-admin-product]');
        if (!root) return;
        const response = await fetch(root.dataset.apiUrl, fetchJsonOpts);
        if (!response.ok) return;
        updateProductView(await response.json());
    }

    function setupProductForms() {
        const root = document.querySelector('[data-admin-product]');
        if (!root) return;

        root.querySelectorAll('form[data-live-stock-form]').forEach(form => {
            form.addEventListener('submit', async event => {
                if (event.defaultPrevented) return;
                event.preventDefault();
                const submitButtons = Array.from(form.querySelectorAll('button[type="submit"]'));
                const submitter = event.submitter || submitButtons[0] || null;
                const originalDisabled = submitButtons.map(btn => btn.disabled);
                let succeeded = false;
                submitButtons.forEach(btn => { btn.disabled = true; });
                try {
                    const formData = new FormData(form);
                    if (submitter && submitter.name) formData.set(submitter.name, submitter.value);
                    const response = await fetch(form.action, {
                        method: 'POST',
                        body: formData,
                        headers: mergeHeadersForMethod('POST', {
                            Accept: 'application/json',
                            'X-Requested-With': 'fetch',
                        }),
                    });
                    const TAE = window.TotemApiErrors;
                    const data = TAE
                        ? await TAE.parseJsonSafe(response)
                        : await response.json().catch(() => ({}));
                    if (!response.ok) {
                        throw new Error(
                            TAE
                                ? TAE.messageFromBadResponse(response, data)
                                : (data.error || 'Não foi possível atualizar o estoque.'),
                        );
                    }
                    updateProductView(data, { forceInputSync: true });
                    succeeded = true;
                    if (form.closest('.admin-card--entrada') || form.closest('.admin-card--saida')) {
                        form.reset();
                    }
                    flash(data.message || 'Estoque atualizado.');
                } catch (err) {
                    const TAE = window.TotemApiErrors;
                    flash(
                        TAE ? TAE.formatCatchMessage(err) : (err.message || 'Não foi possível atualizar o estoque.'),
                        'error',
                    );
                } finally {
                    if (!succeeded || !form.closest('.admin-card--saida')) {
                        submitButtons.forEach((btn, i) => { btn.disabled = originalDisabled[i]; });
                    }
                }
            });
        });

        setInterval(refreshProduct, PRODUCT_POLL_MS);
    }

    async function refreshStockList() {
        const tables = document.querySelectorAll(
            '[data-admin-stock-list], [data-seller-stock-list], [data-admin-event-stock]',
        );
        if (!tables.length) return;
        for (const table of tables) {
            const url = table.dataset.apiUrl;
            if (!url) continue;
            const response = await fetch(url, fetchJsonOpts);
            if (!response.ok) continue;
            const data = await response.json();
            const byId = new Map((data.products || []).map(product => [String(product.id), product]));
            table.querySelectorAll('[data-product-id]').forEach(row => {
                const product = byId.get(String(row.dataset.productId));
                if (!product) return;
                const qty = row.querySelector('[data-stock-qty]');
                const sales = row.querySelector('[data-stock-sales]');
                const promoWrap = row.querySelector('[data-stock-promo-wrap]');
                const promoIcon = row.querySelector('[data-stock-promo-icon]');
                const promoTipPanel = row.querySelector('[data-stock-promo-tooltip-panel]');
                if (qty) qty.innerHTML = `<strong>${escapeHtml(product.estoque)}</strong>`;
                if (sales && Object.prototype.hasOwnProperty.call(product, 'units_sold')) {
                    sales.textContent = String(Number(product.units_sold) || 0);
                }
                updateStockStatusCell(row, product);
                const deliveryWrap = row.querySelector('[data-stock-delivery-wrap]');
                const deliveryCount = row.querySelector('[data-stock-delivery-count]');
                if (deliveryWrap && Object.prototype.hasOwnProperty.call(product, 'pending_delivery_units')) {
                    const pendingN = Number(product.pending_delivery_units) || 0;
                    if (pendingN > 0) {
                        deliveryWrap.removeAttribute('hidden');
                        deliveryWrap.title = `${pendingN} un. aguardando retirada`;
                        if (deliveryCount) deliveryCount.textContent = String(pendingN);
                    } else {
                        deliveryWrap.setAttribute('hidden', '');
                        if (deliveryCount) deliveryCount.textContent = '0';
                    }
                }
                const promoRoot = promoWrap || promoIcon;
                if (promoRoot && promoIcon && Object.prototype.hasOwnProperty.call(product, 'active_promo')) {
                    if (product.active_promo) {
                        if (promoWrap) promoWrap.removeAttribute('hidden');
                        else promoIcon.removeAttribute('hidden');
                        const tip = String(product.promo_tooltip ?? '').trim();
                        if (promoTipPanel) {
                            promoTipPanel.textContent = tip;
                            promoTipPanel.classList.toggle('admin-stock__promo-tooltip-panel--empty', !tip);
                        }
                        promoIcon.title = tip || PROMO_ICON_FALLBACK_TITLE;
                        if (tip) promoIcon.setAttribute('aria-label', `Promoções: ${tip}`);
                        else promoIcon.setAttribute('aria-label', 'Em promoção');
                    } else {
                        if (promoWrap) promoWrap.setAttribute('hidden', '');
                        else promoIcon.setAttribute('hidden', '');
                        if (promoTipPanel) {
                            promoTipPanel.textContent = '';
                            promoTipPanel.classList.add('admin-stock__promo-tooltip-panel--empty');
                        }
                        promoIcon.removeAttribute('title');
                        promoIcon.setAttribute('aria-label', 'Em promoção');
                    }
                }
            });
            const subtitle = document.querySelector('[data-seller-stock-subtitle]');
            if (subtitle && data.stock) {
                const s = data.stock;
                subtitle.innerHTML =
                    `${escapeHtml(s.products_count)} produtos cadastrados &middot; `
                    + `${escapeHtml(s.units_in_stock)} unidades &middot; `
                    + `<strong>${escapeHtml(s.below_min)}</strong> abaixo do mínimo. `
                    + 'Visualização somente leitura.';
            }
        }
    }

    function setupStockList() {
        if (!document.querySelector('[data-admin-stock-list], [data-seller-stock-list], [data-admin-event-stock]')) return;
        setInterval(refreshStockList, LIST_POLL_MS);
    }

    setupProductForms();
    setupStockList();
})();
