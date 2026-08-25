/**
 * Painel administrativo — interações de front.
 *
 * - Histórico de transações: copiar código do pedido; apenas um detalhe expandido por tabela (acordeão).
 * - Toggle do painel “Dados e acesso” no detalhe do vendedor (aria-expanded + hidden).
 * - Confirmação via diálogo interno (`data-confirm` em forms/botões).
 */
(() => {
    'use strict';

    function copyTextToClipboard(text) {
        const v = String(text || '').trim();
        if (!v) return Promise.reject(new Error('empty'));
        if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
            return navigator.clipboard.writeText(v);
        }
        return new Promise((resolve, reject) => {
            const ta = document.createElement('textarea');
            ta.value = v;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            ta.style.top = '0';
            document.body.appendChild(ta);
            ta.focus();
            ta.select();
            try {
                const ok = document.execCommand('copy');
                document.body.removeChild(ta);
                if (ok) resolve();
                else reject(new Error('execCommand'));
            } catch (e) {
                document.body.removeChild(ta);
                reject(e);
            }
        });
    }

    function triggerAdminTxCopyOrder(el) {
        const order = (el.getAttribute('data-copy-order') || '').trim();
        if (!order) return;
        const restoreLabel = `Copiar código do pedido ${order}`;
        copyTextToClipboard(order)
            .then(() => {
                el.classList.add('is-done');
                el.setAttribute('aria-label', `Copiado: ${order}`);
                window.clearTimeout(el._adminTxCopyReset);
                el._adminTxCopyReset = window.setTimeout(() => {
                    el.classList.remove('is-done');
                    el.setAttribute('aria-label', restoreLabel);
                }, 1600);
            })
            .catch(() => {});
    }

    // --- 1. Expansão de linhas do histórico de transações --------------------
    // Delegação para linhas injetadas depois (ex.: atualização ao vivo no painel do vendedor).
    document.querySelector('.admin-main')?.addEventListener('click', event => {
        const collapseBtn = event.target.closest('.admin-section__collapse-toggle');
        if (collapseBtn) {
            const panelId = collapseBtn.getAttribute('aria-controls');
            const panel = panelId ? document.getElementById(panelId) : null;
            if (!panel) return;
            const expanded = collapseBtn.getAttribute('aria-expanded') === 'true';
            const nextExpanded = !expanded;
            collapseBtn.setAttribute('aria-expanded', String(nextExpanded));
            panel.hidden = !nextExpanded;
            const icon = collapseBtn.querySelector('i');
            if (icon) {
                icon.className = nextExpanded
                    ? 'fa-solid fa-chevron-up'
                    : 'fa-solid fa-chevron-down';
            }
            return;
        }

        const copyCtrl = event.target.closest('.admin-tx__copy-order');
        if (copyCtrl) {
            event.preventDefault();
            event.stopPropagation();
            triggerAdminTxCopyOrder(copyCtrl);
            return;
        }

        if (event.target.closest('.admin-tx__delivery')) return;
        if (event.target.closest('input[type="checkbox"]')) return;
        if (event.target.closest('[data-tx-note-toggle]')) return;
        if (event.target.closest('[data-tx-note-form]')) return;
        if (event.target.closest('[data-tx-note-delete-form]')) return;

        const btn = event.target.closest('.admin-tx__row');
        if (!btn) return;
        if (event.target.closest('a')) return;
        if (event.target.closest('.admin-tx__discard-form')) return;
        if (event.target.closest('.admin-tx__handover-form')) return;
        toggleAdminTxRowAccordion(btn);
    });

    /** Uma única linha de transação aberta por `.admin-table` (detalhe tipo acordeão). */
    function closeSiblingAdminTxRows(tableRoot, exceptBtn) {
        if (!tableRoot || !exceptBtn) return;
        tableRoot.querySelectorAll('.admin-tx__row.is-open').forEach(row => {
            if (row === exceptBtn) return;
            row.setAttribute('aria-expanded', 'false');
            row.classList.remove('is-open');
            const oid = row.getAttribute('aria-controls');
            const otherDetails = oid ? document.getElementById(oid) : null;
            if (otherDetails) otherDetails.hidden = true;
        });
    }

    function toggleAdminTxRowAccordion(btn) {
        const expanded = btn.getAttribute('aria-expanded') === 'true';
        const opening = !expanded;
        const targetId = btn.getAttribute('aria-controls');
        const details = targetId ? document.getElementById(targetId) : null;
        if (opening) {
            const tableRoot = btn.closest('.admin-table');
            closeSiblingAdminTxRows(tableRoot, btn);
        }
        btn.setAttribute('aria-expanded', String(!expanded));
        btn.classList.toggle('is-open', !expanded);
        if (details) details.hidden = expanded;
    }

    document.querySelector('.admin-main')?.addEventListener('click', event => {
        const toggle = event.target.closest('[data-tx-note-toggle]');
        if (!toggle) return;
        event.preventDefault();
        event.stopPropagation();
        const wrap = toggle.closest('[data-tx-note]');
        const form = wrap ? wrap.querySelector('[data-tx-note-form]') : null;
        if (!form) return;
        const opening = form.hidden;
        form.hidden = !opening;
        toggle.setAttribute('aria-expanded', String(opening));
        if (opening) {
            const field = form.querySelector('textarea');
            if (field) field.focus();
        }
    });

    // `<div role="button">` no painel do vendedor: Enter/Espaço abrem/fecham (sem click nativo).
    document.querySelector('.admin-main')?.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;

        const copyCtrl = event.target.closest('.admin-tx__copy-order');
        if (copyCtrl) {
            event.preventDefault();
            event.stopPropagation();
            triggerAdminTxCopyOrder(copyCtrl);
            return;
        }

        if (event.target.closest('.admin-tx__delivery')) return;
        if (event.target.closest('input[type="checkbox"]')) return;
        if (event.target.closest('[data-tx-note-toggle]')) return;
        if (event.target.closest('[data-tx-note-form]')) return;
        if (event.target.closest('[data-tx-note-delete-form]')) return;

        const btn = event.target.closest('.admin-tx__row');
        if (!btn || btn.tagName === 'BUTTON') return;
        if (btn.getAttribute('role') !== 'button') return;
        if (event.target.closest('a')) return;
        if (event.target.closest('.admin-tx__discard-form')) return;
        if (event.target.closest('.admin-tx__handover-form')) return;
        event.preventDefault();
        toggleAdminTxRowAccordion(btn);
    });

    /** Abre a linha do pedido indicado no fragmento ``#tx-<id>`` (vindo do motivo/ref do estoque). */
    function openAdminTxFromHash() {
        const match = /^#tx-(\d+)$/i.exec(window.location.hash || '');
        if (!match) return;
        const txId = match[1];
        const txRoot = document.querySelector(`.admin-tx[data-tx-id="${txId}"]`);
        if (!txRoot) return;
        const row = txRoot.querySelector('.admin-tx__row');
        if (!row) return;
        if (row.getAttribute('aria-expanded') !== 'true') {
            toggleAdminTxRowAccordion(row);
        }
        requestAnimationFrame(() => {
            txRoot.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
    }

    openAdminTxFromHash();
    window.addEventListener('hashchange', openAdminTxFromHash);

    // --- 2. Diálogo de confirmação (substitui window.confirm) ---------------
    const confirmDialog = document.getElementById('admin-confirm-dialog');
    const confirmMessageEl = document.getElementById('admin-confirm-message');
    const confirmTitleEl = document.getElementById('admin-confirm-title');
    const confirmOkBtn = document.getElementById('admin-confirm-ok');
    const confirmCancelBtn = document.getElementById('admin-confirm-cancel');
    const confirmCloseBtn = document.getElementById('admin-confirm-close');
    const confirmIconWrap = confirmDialog?.querySelector('.admin-confirm__icon');

    let confirmResolve = null;
    let ignoreNextConfirmClose = false;

    function finishConfirmDialog(result) {
        if (!confirmDialog) return;
        const resolve = confirmResolve;
        confirmResolve = null;

        const done = () => {
            if (resolve) resolve(result);
        };

        if (confirmDialog.open) {
            ignoreNextConfirmClose = true;
            const onClosed = () => {
                confirmDialog.removeEventListener('close', onClosed);
                done();
            };
            confirmDialog.addEventListener('close', onClosed);
            confirmDialog.close();
            return;
        }
        done();
    }

    function readConfirmOptions(el) {
        return {
            title: el.getAttribute('data-confirm-title') || 'Confirmar ação',
            confirmLabel: el.getAttribute('data-confirm-label') || 'Confirmar',
            destructive: el.getAttribute('data-confirm-destructive') !== 'false',
            message: el.getAttribute('data-confirm') || 'Deseja continuar?',
        };
    }

    function readConfirmSecondOptions(el) {
        return {
            title: el.getAttribute('data-confirm-second-title') || 'Confirmação final',
            confirmLabel: el.getAttribute('data-confirm-second-label') || 'Sim, confirmar',
            destructive: el.getAttribute('data-confirm-second-destructive') !== 'false',
            message: el.getAttribute('data-confirm-second') || 'Deseja realmente continuar?',
        };
    }

    function runAdminConfirmFlow(el) {
        return openAdminConfirm(readConfirmOptions(el)).then(ok => {
            if (!ok) return false;
            if (!el.hasAttribute('data-confirm-double')) return true;
            return openAdminConfirm(readConfirmSecondOptions(el));
        });
    }

    function openAdminConfirm(options) {
        const message = options.message || 'Deseja continuar?';
        if (!confirmDialog || !confirmMessageEl || !confirmTitleEl) {
            return Promise.resolve(window.confirm(message));
        }

        confirmTitleEl.textContent = options.title || 'Confirmar ação';
        confirmMessageEl.textContent = message;

        if (confirmOkBtn) {
            confirmOkBtn.textContent = options.confirmLabel || 'Confirmar';
            confirmOkBtn.className = options.destructive === false
                ? 'admin-btn admin-btn--success'
                : 'admin-btn admin-btn--danger-solid';
        }

        if (confirmIconWrap) {
            confirmIconWrap.classList.toggle(
                'admin-confirm__icon--destructive',
                options.destructive !== false
            );
            confirmIconWrap.classList.toggle(
                'admin-confirm__icon--positive',
                options.destructive === false
            );
        }

        return new Promise(resolve => {
            confirmResolve = resolve;
            confirmDialog.showModal();
            confirmCancelBtn?.focus();
        });
    }

    confirmCancelBtn?.addEventListener('click', () => finishConfirmDialog(false));
    confirmCloseBtn?.addEventListener('click', () => finishConfirmDialog(false));
    confirmOkBtn?.addEventListener('click', () => finishConfirmDialog(true));
    confirmDialog?.addEventListener('cancel', event => {
        event.preventDefault();
        finishConfirmDialog(false);
    });
    confirmDialog?.addEventListener('close', () => {
        if (ignoreNextConfirmClose) {
            ignoreNextConfirmClose = false;
            return;
        }
        if (confirmResolve) finishConfirmDialog(false);
    });
    confirmDialog?.addEventListener('click', event => {
        if (event.target === confirmDialog) finishConfirmDialog(false);
    });

    document.querySelector('.admin-main')?.addEventListener('submit', event => {
        const form = event.target.closest('form[data-confirm]');
        if (!form) return;
        if (form.dataset.adminConfirmSubmitting === '1') {
            delete form.dataset.adminConfirmSubmitting;
            return;
        }
        event.preventDefault();
        runAdminConfirmFlow(form).then(ok => {
            if (!ok) return;
            form.dataset.adminConfirmSubmitting = '1';
            form.requestSubmit();
        });
    });

    document.querySelectorAll('button[data-confirm]').forEach(btn => {
        btn.addEventListener('click', event => {
            if (btn.dataset.adminConfirmSubmitting === '1') {
                delete btn.dataset.adminConfirmSubmitting;
                return;
            }
            event.preventDefault();
            openAdminConfirm(readConfirmOptions(btn)).then(ok => {
                if (!ok) return;
                btn.dataset.adminConfirmSubmitting = '1';
                const form = btn.closest('form');
                if (form) {
                    form.requestSubmit();
                } else {
                    btn.click();
                }
            });
        });
    });

    // --- 3. Auto-dismiss das flash messages ----------------------------------
    document.querySelectorAll('.admin-flash').forEach(el => {
        setTimeout(() => {
            el.style.transition = 'opacity 400ms ease, transform 400ms ease';
            el.style.opacity = '0';
            el.style.transform = 'translateY(-6px)';
            setTimeout(() => el.remove(), 450);
        }, 6000);
    });

    // --- 3b. Seleção em lote — itens aguardando retirada ---------------------
    function syncDeliveryPanel(panel) {
        if (!panel) return;
        const items = panel.querySelectorAll('input[data-delivery-item]');
        const checkAll = panel.querySelector('input[data-delivery-check-all]');
        const submitBtn = panel.querySelector('[data-delivery-batch-submit]');
        const countEl = panel.querySelector('[data-delivery-sel-count]');
        const batchForm = panel.querySelector('[data-delivery-batch]');
        const selected = Array.from(items).filter(el => el.checked);
        const n = selected.length;
        const total = items.length;

        if (submitBtn) submitBtn.disabled = n === 0;
        if (countEl) {
            if (n > 0) {
                countEl.hidden = false;
                countEl.textContent = String(n);
            } else {
                countEl.hidden = true;
                countEl.textContent = '';
            }
        }
        if (checkAll) {
            checkAll.checked = total > 0 && n === total;
            checkAll.indeterminate = n > 0 && n < total;
        }
        if (batchForm) {
            const label = n === 1 ? '1 item selecionado' : `${n} itens selecionados`;
            batchForm.setAttribute(
                'data-confirm',
                `Confirmar a entrega de ${label}? O estoque será baixado.`
            );
        }
    }

    function initDeliveryPanels(root) {
        const scope = root && typeof root.querySelectorAll === 'function' ? root : document;
        if (scope.matches && scope.matches('[data-delivery-panel]')) {
            syncDeliveryPanel(scope);
            return;
        }
        scope.querySelectorAll('[data-delivery-panel]').forEach(syncDeliveryPanel);
    }

    const adminMain = document.querySelector('.admin-main');
    if (adminMain) {
        adminMain.addEventListener('change', event => {
            const target = event.target;
            if (!(target instanceof HTMLInputElement) || target.type !== 'checkbox') return;

            const panel = target.closest('[data-delivery-panel]');
            if (!panel) return;

            if (target.hasAttribute('data-delivery-check-all')) {
                panel.querySelectorAll('input[data-delivery-item]').forEach(el => {
                    el.checked = target.checked;
                });
            } else if (!target.hasAttribute('data-delivery-item')) {
                return;
            }

            syncDeliveryPanel(panel);
        });
    }

    function setTxItemEditing(row, editing) {
        if (!row) return;
        row.classList.toggle('is-editing', editing);
        const skuInput = row.querySelector('.admin-tx__sku-input');
        const qtyInput = row.querySelector('.admin-tx__qty-input');
        const editBtn = row.querySelector('[data-tx-item-edit]');
        const confirmBtn = row.querySelector('.admin-tx__item-confirm');
        const cancelBtn = row.querySelector('[data-tx-item-cancel]');
        [skuInput, qtyInput].forEach((input) => {
            if (!input) return;
            input.hidden = !editing;
            input.disabled = !editing;
        });
        if (editBtn) editBtn.hidden = editing;
        if (confirmBtn) confirmBtn.hidden = !editing;
        if (cancelBtn) cancelBtn.hidden = !editing;
        if (editing && skuInput) {
            skuInput.focus();
            skuInput.select();
        }
    }

    function initTxItemReplace(root) {
        const scope = root || document;
        scope.querySelectorAll('.admin-tx__items-row--editable').forEach((row) => {
            if (row.dataset.txItemBound === '1') return;
            row.dataset.txItemBound = '1';
            row.addEventListener('click', (event) => {
                const editBtn = event.target.closest('[data-tx-item-edit]');
                const cancelBtn = event.target.closest('[data-tx-item-cancel]');
                if (editBtn) {
                    event.preventDefault();
                    setTxItemEditing(row, true);
                } else if (cancelBtn) {
                    event.preventDefault();
                    const skuInput = row.querySelector('.admin-tx__sku-input');
                    const qtyInput = row.querySelector('.admin-tx__qty-input');
                    if (skuInput) skuInput.value = skuInput.defaultValue;
                    if (qtyInput) qtyInput.value = qtyInput.defaultValue;
                    setTxItemEditing(row, false);
                }
            });
        });
    }

    initDeliveryPanels();
    initTxItemReplace();
    document.addEventListener('totem:admin-tx-live-updated', event => {
        const liveRoot = event.detail?.root || document;
        initDeliveryPanels(liveRoot);
        initTxItemReplace(liveRoot);
    });
    window.TotemAdminDelivery = { initDeliveryPanels, syncDeliveryPanel };

    // --- 4. Altura real da admin-topbar — sticky do catálogo embutido (mobile + desktop) ---
    const adminShell = document.querySelector('.admin-shell');
    const adminTopbar = adminShell?.querySelector(':scope > .admin-topbar');

    if (adminShell && adminTopbar) {
        const syncAdminTopbarHeight = () => {
            adminShell.style.setProperty(
                '--admin-topbar-height',
                `${Math.round(adminTopbar.getBoundingClientRect().height)}px`
            );
        };

        syncAdminTopbarHeight();

        if (typeof ResizeObserver !== 'undefined') {
            new ResizeObserver(syncAdminTopbarHeight).observe(adminTopbar);
        } else {
            window.addEventListener('resize', syncAdminTopbarHeight, { passive: true });
        }
    }
})();
