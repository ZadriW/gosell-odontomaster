(() => {
    'use strict';

    const POLL_MS = 2000;
    const table =
        document.querySelector('[data-admin-movements]') ||
        document.querySelector('.admin-table');
    /** Limite de linhas após inserções ao vivo (página 1): por página + margem. */
    const trimCap = Math.max(
        50,
        Math.max(10, parseInt(table?.dataset.perPage || '25', 10) || 25) + 40,
    );
    const disablePoll = table?.dataset.disablePoll === 'true';

    function readLatestMovementIdFromDom(root) {
        let max = 0;
        if (!root) return max;
        root.querySelectorAll('[data-movement-id]').forEach(el => {
            const id = Number(el.dataset.movementId);
            if (Number.isFinite(id) && id > max) max = id;
        });
        return max;
    }

    let latestId = readLatestMovementIdFromDom(table);

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, char => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;',
        }[char]));
    }

    function eventBadgeInline(movement) {
        const bg = movement.event_badge_bg;
        const fg = movement.event_badge_fg;
        if (!bg || !fg) return '';
        const safeBg = escapeHtml(bg);
        const safeFg = escapeHtml(fg);
        return ` style="background-color:${safeBg};color:${safeFg};"`;
    }

    function eventCell(movement) {
        const eid = movement.event_id;
        const ename = String(movement.event_name ?? '').trim();
        const hasId = eid !== undefined && eid !== null && eid !== '';
        const attrStyle = eventBadgeInline(movement);
        if (hasId && ename) {
            return `<span class="admin-mov__event-cell"><span class="admin-nav__event-badge admin-mov__event-badge"${attrStyle} title="Evento #${escapeHtml(eid)}">
                ${escapeHtml(ename)}
            </span></span>`;
        }
        if (hasId) {
            return `<span class="admin-mov__event-cell"><span class="admin-mov__event-unknown" title="Evento #${escapeHtml(eid)} (cadastro indisponível)">#${escapeHtml(eid)}</span></span>`;
        }
        return '<span class="admin-mov__event-cell"><span class="admin-mov__event-none" title="Movimentação no estoque global do catálogo (sem evento)">—</span></span>';
    }

    function renderMovement(movement) {
        const reason = `${movement.reference ? `<code>${escapeHtml(movement.reference)}</code>` : ''} ${escapeHtml(movement.reason || '-')}`;
        const skuMeta = movement.product_sku
            ? ` &middot; <code class="admin-mov__sku">${escapeHtml(movement.product_sku)}</code>`
            : '';

        return `
            <div class="admin-mov__wrapper" data-movement-id="${escapeHtml(movement.id)}">
                <div class="admin-mov__row admin-mov__row--wide admin-mov__row--wide-events" role="row">
                    <span>${escapeHtml(movement.created_at_display)}</span>
                    <span class="admin-mov__product">
                        <a href="${escapeHtml(movement.product_url)}">${escapeHtml(movement.product_name || '—')}</a>
                        <small>#${escapeHtml(movement.product_id)}${skuMeta} &middot; ${escapeHtml(movement.product_category || '-')}</small>
                    </span>
                    <span class="admin-mov__type-cell">
                        <span class="admin-badge admin-mov__badge admin-mov__badge--${escapeHtml(movement.movement_type)}">
                            ${escapeHtml(movement.movement_label)}
                        </span>
                    </span>
                    ${eventCell(movement)}
                    <span class="admin-table__col--num admin-mov__delta admin-mov__delta--${escapeHtml(movement.delta_kind)}">
                        ${escapeHtml(movement.delta_display)}
                    </span>
                    <span class="admin-table__col--num"><strong>${escapeHtml(movement.balance_after)}</strong></span>
                    <span class="admin-mov__reason">${reason}</span>
                    <span class="admin-mov__user-cell">
                        ${escapeHtml(movement.created_by_display || movement.created_by || '-')}
                    </span>
                </div>
            </div>
        `;
    }

    function trimExcessMovementRows() {
        if (!table) return;
        while (table.querySelectorAll('.admin-mov__wrapper').length > trimCap) {
            const wrappers = table.querySelectorAll('.admin-mov__wrapper');
            wrappers[wrappers.length - 1].remove();
        }
    }

    /**
     * Incorpora só movimentações novas (id maior que o último conhecido), sem recriar a tabela.
     */
    function mergeNewMovements(movements, nextLatest) {
        if (!table) {
            latestId = nextLatest;
            return;
        }
        const head = table.querySelector('.admin-table__head');
        const prevLatest = latestId;

        if (!movements || !movements.length) {
            latestId = nextLatest;
            return;
        }

        if (!head) {
            latestId = nextLatest;
            return;
        }

        const newItems = [];
        for (let i = 0; i < movements.length; i += 1) {
            const m = movements[i];
            const mid = Number(m.id);
            if (mid <= prevLatest) break;
            if (table.querySelector(`.admin-mov__wrapper[data-movement-id="${mid}"]`)) {
                continue;
            }
            newItems.push(m);
        }

        latestId = nextLatest;

        if (!newItems.length) {
            return;
        }

        const emptyEl = table.querySelector('.admin-empty');
        if (emptyEl && emptyEl.parentElement === table) emptyEl.remove();

        const html = newItems.map(renderMovement).join('');
        head.insertAdjacentHTML('afterend', html);
        trimExcessMovementRows();
    }

    async function refreshMovements() {
        if (!table || !table.dataset.apiUrl) return;
        const response = await fetch(table.dataset.apiUrl, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' },
        });
        if (!response.ok) return;
        const data = await response.json();
        const nextLatest = Number(data.latest_id || 0);
        if (nextLatest <= latestId) return;

        const movements = data.movements || [];
        const hasNew = movements.some(movement => Number(movement.id) > latestId);
        if (!hasNew) {
            latestId = nextLatest;
            return;
        }

        mergeNewMovements(movements, nextLatest);
    }

    if (table && table.dataset.apiUrl && !disablePoll) {
        setInterval(refreshMovements, POLL_MS);
    }
})();
