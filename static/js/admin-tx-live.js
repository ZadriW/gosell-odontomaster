(() => {
    'use strict';

    const DEFAULT_POLL_MS = 20000;

    const root = document.querySelector('[data-admin-event-tx-live]');
    if (!root) return;

    const apiUrl = String(root.dataset.apiUrl || '').trim();
    if (!apiUrl) return;

    const pollMs = Math.max(
        5000,
        Number.parseInt(root.dataset.pollMs || '', 10) || DEFAULT_POLL_MS,
    );

    let inFlight = false;

    function openTxIds(container) {
        const ids = [];
        container.querySelectorAll('.admin-tx__row.is-open, .admin-tx__row[aria-expanded="true"]').forEach((row) => {
            const tx = row.closest('.admin-tx');
            const id = tx && tx.dataset.txId;
            if (id) ids.push(String(id));
        });
        return ids;
    }

    function restoreOpenRows(container, ids) {
        ids.forEach((id) => {
            const txRoot = container.querySelector(`.admin-tx[data-tx-id="${id}"]`);
            if (!txRoot) return;
            const row = txRoot.querySelector('.admin-tx__row');
            const details = txRoot.querySelector('.admin-tx__details');
            if (!row) return;
            row.classList.add('is-open');
            row.setAttribute('aria-expanded', 'true');
            if (details) details.hidden = false;
        });
    }

    function shouldSkipRefresh() {
        if (document.hidden) return true;
        if (document.getElementById('admin-confirm-dialog')?.open) return true;

        const active = document.activeElement;
        if (!active || !root.contains(active)) return false;

        const tag = active.tagName;
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
        if (active.closest('form')) return true;
        return false;
    }

    function pollUrl() {
        const qs = window.location.search || '';
        return qs ? `${apiUrl}${qs}` : apiUrl;
    }

    async function refreshTransactions() {
        if (inFlight || shouldSkipRefresh()) return;
        inFlight = true;
        const opened = openTxIds(root);
        try {
            const response = await fetch(pollUrl(), {
                credentials: 'same-origin',
                headers: { Accept: 'text/html' },
            });
            if (!response.ok) return;
            const html = await response.text();
            if (shouldSkipRefresh()) return;
            root.innerHTML = html;
            restoreOpenRows(root, opened);
            document.dispatchEvent(new CustomEvent('totem:admin-tx-live-updated', { detail: { root } }));

            const hashMatch = /^#tx-(\d+)$/i.exec(window.location.hash || '');
            if (hashMatch && opened.indexOf(hashMatch[1]) === -1) {
                restoreOpenRows(root, [hashMatch[1]]);
            }
        } catch (_err) {
            /* rede instável: tenta de novo no próximo ciclo */
        } finally {
            inFlight = false;
        }
    }

    setInterval(refreshTransactions, pollMs);

    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refreshTransactions();
    });
})();
