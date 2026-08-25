/**
 * Aguardo da maquininha + confirmação da venda (vendedor autenticado).
 *
 * Fluxo:
 *  1. Vendedor clica "Pagamento realizado" → cria transação PENDENTE via POST.
 *  2. PIX/cartão: tela de AUT → PATCH confirma, baixa estoque.
 *  3. Dinheiro: confirma direto (AUT interno "DINHEIRO"), sem maquininha.
 *  4. Tela de sucesso é exibida.
 */
(() => {
    'use strict';

    const RESUME = window.__PENDING_AUT_RESUME__;
    const isResumeMode =
        RESUME &&
        RESUME.transaction_id != null &&
        Number.isFinite(Number(RESUME.transaction_id));

    const Cart = window.Cart;
    const PromoPricing = window.PromoPricing;
    if (!isResumeMode && !Cart) return;
    if (isResumeMode && (!Cart || typeof Cart.getItems !== 'function')) {
        window.location.assign(CATALOG_URL);
        return;
    }

    const FLOW = window.__TOTEM_FLOW__ || {};
    const SUMMARY_URL = FLOW.payment || '/vendedor/pagamento';
    const CATALOG_URL = FLOW.catalog || '/vendedor/venda';
    const HOME_URL = FLOW.home || '/';
    const SUCCESS_REDIRECT_MS = 30000;
    const CASH_AUT = 'DINHEIRO';

    const RESUME_PENDING_TX_KEY = 'totem_resume_pending_tx_id';

    function readResumePendingTxId() {
        try {
            const raw = sessionStorage.getItem(RESUME_PENDING_TX_KEY);
            return raw && /^\d+$/.test(raw.trim()) ? raw.trim() : null;
        } catch (_) {
            return null;
        }
    }

    const content    = document.getElementById('waitingContent');
    const autSection = document.getElementById('autSection');
    const autInput   = document.getElementById('autInput');
    const autSave    = document.getElementById('autSave');
    const autError   = document.getElementById('autError');
    const success    = document.getElementById('paymentSuccess');
    const itemsEl    = document.getElementById('waitingItems');
    const countEl    = document.getElementById('waitingCount');
    const totalEl    = document.getElementById('waitingTotal');
    const confirmBtn = document.getElementById('waitingConfirm');
    const confirmLabel = document.getElementById('waitingConfirmLabel');
    const backBtn    = document.getElementById('waitingBack');
    const successOrder     = document.getElementById('successOrder');
    const successCountdown = document.getElementById('successCountdown');
    const successPrint     = document.getElementById('successPrint');
    const successFinish    = document.getElementById('successFinish');
    const waitingBackLabel = document.getElementById('waitingBackLabel');
    const waitingTitle = document.getElementById('waitingTitle');
    const waitingLead = document.getElementById('waitingLead');

    /** id da transação pendente criada no primeiro step. */
    let pendingTxId = null;
    /** Número do pedido retornado pela API, definido após confirmação com AUT. */
    let confirmedOrderNumber = null;
    /** Token assinado (``t``) para abrir a nota de retirada. */
    let confirmedReceiptToken = null;

    function loadClientData() {
        return window.PaymentForm && typeof window.PaymentForm.load === 'function'
            ? window.PaymentForm.load()
            : null;
    }

    function isCashPayment(clientData) {
        const data = clientData || loadClientData();
        return String(data && data.payment_method || '').toLowerCase() === 'dinheiro';
    }

    function paymentMethodLabel(raw, installments) {
        const v = String(raw || '').toLowerCase();
        if (v === 'pix') return 'PIX';
        if (v === 'dinheiro') return 'Dinheiro';
        const n = parseInt(String(installments ?? ''), 10);
        if (Number.isFinite(n) && n > 1) return `Cartão em ${n}x`;
        return 'Cartão';
    }

    function syncWaitingPaymentMethodUi() {
        const clientData = loadClientData();
        const pm   = clientData && clientData.payment_method;
        const inst = clientData && clientData.installments;
        const cash = isCashPayment(clientData);

        const methodEl = document.getElementById('waitingPaymentMethod');
        if (methodEl) methodEl.textContent = paymentMethodLabel(pm, inst);

        const heroIcon = document.querySelector('.payment-wait__pulse i');
        if (heroIcon) {
            if (cash) {
                heroIcon.className = 'fa-solid fa-money-bill-wave';
            } else if (String(pm || '').toLowerCase() === 'pix') {
                heroIcon.className = 'fa-brands fa-pix';
            } else {
                heroIcon.className = 'fa-solid fa-credit-card';
            }
        }

        if (waitingTitle) {
            waitingTitle.textContent = cash
                ? 'Pagamento em dinheiro'
                : 'Aguardando pagamento';
        }

        if (waitingLead) {
            waitingLead.innerHTML = cash
                ? 'Receba o valor em <strong>espécie</strong> do cliente conforme o total do pedido. '
                    + 'Depois de conferir o recebimento, toque no botão abaixo para registrar a venda '
                    + 'e liberar a nota de retirada.'
                : 'Utilize a <strong>maquininha</strong> ao lado do totem para concluir o pagamento '
                    + '(<strong>PIX</strong> ou <strong>cartão</strong>, conforme selecionado no passo anterior). '
                    + 'Quando a operação for aprovada na maquininha, toque no botão abaixo para registrar '
                    + 'a venda e liberar a nota de retirada.';
        }

        if (confirmLabel) {
            confirmLabel.textContent = cash
                ? 'Confirmar recebimento em dinheiro'
                : 'Pagamento realizado';
        }
    }

    function renderItem(item) {
        if (PromoPricing && typeof PromoPricing.renderLineItemHtml === 'function') {
            return PromoPricing.renderLineItemHtml(item, Cart.formatBRL.bind(Cart), 'payment-item');
        }
        const subtotal = Cart.formatBRL(item.subtotal != null ? item.subtotal : item.preco * item.quantidade);
        const unit     = Cart.formatBRL(item.preco);
        return `
            <article class="payment-item" data-id="${item.id}">
                <div class="payment-item__image">
                    <img src="${item.imagem}" alt="${item.nome}" loading="lazy">
                </div>
                <div class="payment-item__info">
                    <span class="payment-item__category">${item.categoria || ''}</span>
                    <h3 class="payment-item__name">${item.nome}</h3>
                    ${item.variante ? `<p class="payment-item__variant">${item.variante}</p>` : ''}
                    ${item.sku ? `<p class="payment-item__sku">SKU ${item.sku}</p>` : ''}
                    <p class="payment-item__meta">${item.quantidade} × ${unit}</p>
                </div>
                <div class="payment-item__total">${subtotal}</div>
            </article>
        `;
    }

    function renderWaiting() {
        if (!Cart || typeof Cart.getItems !== 'function') return;
        const items = Cart.getItems();
        if (items.length === 0) {
            window.location.replace(CATALOG_URL);
            return;
        }
        const totals = Cart.getTotals();
        itemsEl.innerHTML = items.map(renderItem).join('');
        countEl.textContent = totals.count;
        const clientData = loadClientData();
        const payable = clientData && clientData.seller_total != null
            ? Number(clientData.seller_total)
            : totals.total;
        totalEl.textContent = Cart.formatBRL(Number.isFinite(payable) ? payable : totals.total);
        syncWaitingPaymentMethodUi();
    }

    function applyTxQuote(data) {
        if (data && Array.isArray(data.items) && typeof Cart.applyServerQuote === 'function') {
            Cart.applyServerQuote(data);
        }
    }

    function buildTransactionPayload() {
        const clientData = loadClientData();
        return {
            items: Cart.getItems(),
            client: clientData || {},
            payment_method: (clientData && clientData.payment_method)
                ? clientData.payment_method
                : 'cartao',
            seller_total: clientData && clientData.seller_total != null
                ? clientData.seller_total
                : undefined,
        };
    }

    /** Etapa 1: cria transação pendente. Retorna o id. */
    async function createPendingTransaction() {
        const data = await window.TotemApiErrors.fetchJson('/api/transacoes', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(buildTransactionPayload()),
        });
        applyTxQuote(data);
        return data.id;
    }

    /** Sincroniza pedido pendente com carrinho + formulário atuais (retomada de checkout). */
    async function patchPendingTransaction(txId) {
        const data = await window.TotemApiErrors.fetchJson(`/api/transacoes/${txId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify(buildTransactionPayload()),
        });
        applyTxQuote(data);
        return data;
    }

    /** Etapa 2: confirma com AUT. Retorna a resposta completa (order_number, pending_items…). */
    async function confirmWithAut(txId, aut) {
        const data = await window.TotemApiErrors.fetchJson(`/api/transacoes/${txId}/aut`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
            body: JSON.stringify({ aut }),
        });
        confirmedReceiptToken =
            data.receipt_token != null ? String(data.receipt_token) : null;
        return data;
    }

    async function finalizePendingTransaction(txId) {
        const resumeKey = readResumePendingTxId();
        if (
            resumeKey != null
            && Number(resumeKey) === Number(txId)
        ) {
            await patchPendingTransaction(txId);
            renderWaiting();
        }
        const aut = isCashPayment() ? CASH_AUT : null;
        if (!aut) {
            throw new Error('Fluxo inválido: confirmação sem AUT.');
        }
        return confirmWithAut(txId, aut);
    }

    function showAutScreen() {
        if (content) {
            content.hidden = true;
            content.setAttribute('aria-hidden', 'true');
        }
        if (autSection) {
            autSection.hidden = false;
            autSection.removeAttribute('aria-hidden');
        }
        if (autInput) {
            autInput.value = '';
            autInput.focus();
        }
        if (autError) autError.hidden = true;
    }

    function showError(msg) {
        if (!autError) return;
        autError.textContent = msg;
        autError.hidden = false;
    }

    function renderPendingDeliveryNotice(pendingItems) {
        const box = document.getElementById('successPendingDelivery');
        if (!box) return;
        const pending = (Array.isArray(pendingItems) ? pendingItems : [])
            .filter(p => Number(p.pending) > 0);
        if (pending.length === 0) {
            box.innerHTML = '';
            box.hidden = true;
            return;
        }
        const rows = pending
            .map(p => `<li><strong>${p.pending}×</strong> ${p.product_name}</li>`)
            .join('');
        box.innerHTML = `
            <i class="fa-solid fa-box-open" aria-hidden="true"></i>
            <span>
                Itens pagos aguardando reposição de estoque (retirada posterior):
                <ul>${rows}</ul>
                Eles aparecem no pedido como <strong>retirada pendente</strong> e constam na nota.
            </span>
        `;
        box.hidden = false;
    }

    function showSuccess(orderNumber, pendingItems) {
        confirmedOrderNumber = orderNumber != null ? String(orderNumber) : null;
        successOrder.textContent = `Pedido #${orderNumber}`;
        renderPendingDeliveryNotice(pendingItems);
        document.querySelector('.payment')?.classList.add('payment--success-only');

        if (autSection) {
            autSection.hidden = true;
            autSection.setAttribute('aria-hidden', 'true');
        }
        if (content) {
            content.hidden = true;
            content.setAttribute('aria-hidden', 'true');
        }

        success.hidden = false;
        success.removeAttribute('aria-hidden');

        if (Cart && typeof Cart.clear === 'function') Cart.clear();
        if (window.PaymentForm) window.PaymentForm.clear();
        try {
            sessionStorage.removeItem(RESUME_PENDING_TX_KEY);
        } catch (_) {}

        let remaining = Math.floor(SUCCESS_REDIRECT_MS / 1000);
        successCountdown.textContent = String(remaining);
        const tick = setInterval(() => {
            remaining -= 1;
            if (remaining <= 0) {
                clearInterval(tick);
                successCountdown.textContent = '0';
            } else {
                successCountdown.textContent = String(remaining);
            }
        }, 1000);

        setTimeout(() => {
            window.location.assign(HOME_URL);
        }, SUCCESS_REDIRECT_MS);
    }

    confirmBtn?.addEventListener('click', async () => {
        if (!Cart || Cart.isEmpty()) return;
        const cash = isCashPayment();
        const originalLabel = confirmLabel
            ? confirmLabel.textContent
            : confirmBtn.textContent;
        confirmBtn.disabled = true;
        confirmBtn.innerHTML =
            '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> Aguarde...';
        try {
            pendingTxId = await createPendingTransaction();
            renderWaiting();
            if (cash) {
                const confirmData = await finalizePendingTransaction(pendingTxId);
                showSuccess(confirmData.order_number, confirmData.pending_items);
                return;
            }
            if (confirmLabel) {
                confirmBtn.innerHTML =
                    `<i class="fa-solid fa-check" aria-hidden="true"></i> <span id="waitingConfirmLabel">${originalLabel}</span>`;
            } else {
                confirmBtn.innerHTML = originalLabel;
            }
            showAutScreen();
        } catch (err) {
            confirmBtn.disabled = false;
            if (confirmLabel) {
                confirmBtn.innerHTML =
                    `<i class="fa-solid fa-check" aria-hidden="true"></i> <span id="waitingConfirmLabel">${originalLabel}</span>`;
            } else {
                confirmBtn.innerHTML = originalLabel;
            }
            window.alert(err.message || 'Não foi possível registrar o pedido. Tente novamente.');
        }
    });

    if (autSave) {
        autSave.addEventListener('click', async () => {
            const aut = (autInput ? autInput.value : '').trim();
            if (!aut) {
                showError('Por favor, informe o código AUT antes de salvar.');
                autInput && autInput.focus();
                return;
            }
            if (!pendingTxId) {
                showError('Sessão inválida. Recarregue a página e tente novamente.');
                return;
            }
            const originalLabel = autSave.innerHTML;
            autSave.disabled = true;
            autSave.innerHTML =
                '<i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i> Confirmando...';
            if (autError) autError.hidden = true;
            try {
                const resumeKey = readResumePendingTxId();
                if (
                    resumeKey != null
                    && Number(resumeKey) === Number(pendingTxId)
                ) {
                    await patchPendingTransaction(pendingTxId);
                    renderWaiting();
                }
                const confirmData = await confirmWithAut(pendingTxId, aut);
                autSave.innerHTML = originalLabel;
                showSuccess(confirmData.order_number, confirmData.pending_items);
            } catch (err) {
                autSave.disabled = false;
                autSave.innerHTML = originalLabel;
                showError(err.message || 'Não foi possível confirmar a venda. Tente novamente.');
            }
        });
    }

    backBtn?.addEventListener('click', () => {
        window.location.assign(isResumeMode ? HOME_URL : SUMMARY_URL);
    });

    function openReceiptPrint() {
        if (!confirmedOrderNumber || !confirmedReceiptToken) return;
        const qs = new URLSearchParams({
            t: confirmedReceiptToken,
            print: '1',
        });
        const path = `/nota/${encodeURIComponent(confirmedOrderNumber)}?${qs.toString()}`;
        const a = document.createElement('a');
        a.href = path;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        document.body.appendChild(a);
        a.click();
        a.remove();
    }

    successPrint?.addEventListener('click', () => openReceiptPrint());

    successFinish?.addEventListener('click', () => {
        window.location.assign(HOME_URL);
    });

    if (isResumeMode) {
        pendingTxId = Number(RESUME.transaction_id);
        document.querySelector('.payment')?.classList.add('payment--resume-aut');
        if (waitingBackLabel) waitingBackLabel.textContent = 'Voltar ao painel';
        if (content) {
            content.hidden = true;
            content.setAttribute('aria-hidden', 'true');
        }
        const hint = document.getElementById('autResumeOrderHint');
        (async () => {
            try {
                await patchPendingTransaction(pendingTxId);
                renderWaiting();
            } catch (err) {
                window.alert(err.message || 'Não foi possível atualizar o pedido. Verifique o carrinho e tente novamente.');
                window.location.assign(SUMMARY_URL);
                return;
            }
            if (isCashPayment()) {
                if (content) {
                    content.hidden = false;
                    content.removeAttribute('aria-hidden');
                }
                if (hint && RESUME.order_number != null && String(RESUME.order_number).trim()) {
                    hint.hidden = false;
                    hint.textContent =
                        `Pedido #${RESUME.order_number}. Confirme o recebimento em dinheiro para concluir a venda.`;
                }
                syncWaitingPaymentMethodUi();
                return;
            }
            if (hint && RESUME.order_number != null && String(RESUME.order_number).trim()) {
                hint.hidden = false;
                hint.textContent =
                    `Pedido #${RESUME.order_number}. Informe o código AUT para confirmar a venda e baixar o estoque.`;
            }
            showAutScreen();
            syncWaitingPaymentMethodUi();
        })();
    } else {
        Cart.subscribe(() => {
            if (!success.hidden) return;
            renderWaiting();
        });
        renderWaiting();
        syncWaitingPaymentMethodUi();
    }
})();
