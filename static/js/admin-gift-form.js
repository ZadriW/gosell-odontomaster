/**
 * Formulário de brinde (templates/admin/event_gift_detail.html), criação e edição.
 *
 * - Brinde: produto do estoque (seletor de um produto só, admin-promo-picker.js,
 *   ``gift_product_id``) ou brinde avulso (``gift_item_id``; "novo" cadastra o
 *   avulso junto, com ``new_item_name``/``new_item_stock``).
 * - Regra: "A partir de N un.", "A cada kit de N un. do mesmo produto" ou
 *   "Pedidos a partir de R$ X"; só a seção escolhida fica habilitada (cada uma
 *   tem o próprio campo). Na regra por valor os participantes são opcionais.
 * - Resumo ao vivo e validação antes do envio; o servidor valida de novo.
 */
(() => {
    'use strict';

    const form = document.querySelector('[data-gift-form]');
    if (!form) return;

    const giftRoot = form.querySelector('[data-gift-picker]');
    const productsRoot = form.querySelector('[data-promo-picker]');
    const readonly = productsRoot?.dataset.readonly === '1';

    const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
    const int = (input) => {
        const v = parseInt(String(input?.value ?? '').trim(), 10);
        return Number.isFinite(v) ? v : NaN;
    };
    const money = (input) => {
        const v = parseFloat(String(input?.value ?? '').replace(',', '.'));
        return Number.isFinite(v) ? v : NaN;
    };
    const brlFmt = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
    const brl = (n) => brlFmt.format(Number(n) || 0);
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    const flash = (msg, cat = 'error') => {
        if (typeof window.showAdminFlash === 'function') window.showAdminFlash(msg, cat);
    };

    const RULE_ICONS = { min_qty: 'fa-cart-plus', kit: 'fa-boxes-stacked', min_total: 'fa-sack-dollar' };
    const NEW_ITEM = 'novo';

    let giftIds = [];
    let productIds = [];
    let giftPicker = null;

    const ruleRadios = [...form.querySelectorAll('[data-gift-rule]')];
    const sections = [...form.querySelectorAll('[data-rule-section]')];
    const sourceRadios = [...form.querySelectorAll('[data-gift-source]')];
    const sourcePanels = [...form.querySelectorAll('[data-gift-source-panel]')];
    const itemRadios = [...form.querySelectorAll('[data-gift-item]')];
    const newFields = form.querySelector('[data-gift-new-fields]');
    const currentRule = () => (ruleRadios.find((r) => r.checked) || {}).value || 'min_qty';
    const currentSource = () => (sourceRadios.find((r) => r.checked) || {}).value || 'produto';
    const currentItem = () => (itemRadios.find((r) => r.checked) || {});
    const field = (name) => form.querySelector(`[data-rule-section="${currentRule()}"] [name="${name}"]`);
    const giftQtyInput = form.querySelector('[data-gift-qty]');

    function syncRule() {
        const rule = currentRule();
        sections.forEach((s) => {
            const on = s.dataset.ruleSection === rule;
            s.hidden = !on;
            s.querySelectorAll('input, select').forEach((el) => { el.disabled = !on || readonly; });
        });
        ruleRadios.forEach((r) => r.closest('.promo-rule')?.classList.toggle('is-checked', r.checked));
        const icon = form.querySelector('[data-summary-icon]');
        if (icon) {
            icon.className = `promo-summary__icon gift-type--${rule}`;
            icon.innerHTML = `<i class="fa-solid ${RULE_ICONS[rule] || 'fa-gift'}" aria-hidden="true"></i>`;
        }
        const optional = rule === 'min_total';
        const tag = form.querySelector('[data-products-optional]');
        if (tag) tag.hidden = !optional;
        const hint = form.querySelector('[data-products-hint]');
        if (hint) {
            hint.textContent = optional
                ? 'Opcional nesta regra: sem produtos, vale o valor do pedido inteiro; com produtos, só o valor deles conta para chegar ao mínimo.'
                : 'As compras destes produtos contam para a regra. Busque pelo nome, SKU ou categoria; dá para colar vários SKUs de uma vez.';
        }
        updateSummary();
    }

    function syncSource() {
        const src = currentSource();
        sourcePanels.forEach((p) => { p.hidden = p.dataset.giftSourcePanel !== src; });
        sourceRadios.forEach((r) => r.closest('.gift-source__opt')?.classList.toggle('is-checked', r.checked));
        // O seletor de produto continua no DOM; o servidor ignora ``gift_product_id``
        // quando o brinde é avulso. Os campos do avulso novo só vão quando usados.
        const isNew = src === 'avulso' && currentItem().value === NEW_ITEM;
        if (newFields) {
            newFields.hidden = !isNew;
            newFields.querySelectorAll('input').forEach((el) => { el.disabled = !isNew || readonly; });
        }
        itemRadios.forEach((r) => r.closest('.gift-avulso')?.classList.toggle('is-checked', r.checked));
        updateSummary();
    }

    /** ``{ name, image, stockText, stock }`` do brinde escolhido, ou null. */
    function gift() {
        if (currentSource() === 'avulso') {
            const r = currentItem();
            if (!r.value) return null;
            if (r.value === NEW_ITEM) {
                const name = form.querySelector('[data-gift-new-name]')?.value.trim();
                const stock = int(form.querySelector('[name="new_item_stock"]'));
                return name ? { name, image: '', avulso: true, stock: Number.isFinite(stock) ? stock : null } : null;
            }
            const avail = r.dataset.available;
            return { name: r.dataset.name, image: '', avulso: true, stock: avail === '' ? null : Number(avail) };
        }
        if (!giftIds.length || !giftPicker) return null;
        const p = giftPicker.productOf(giftIds[0]);
        const inEvent = !(p.outside_event || p.outsideEvent) && Number.isFinite(Number(p.stock));
        return { name: p.name, image: p.image, avulso: false, stock: inEvent ? Number(p.stock) : null };
    }

    function ruleSentence() {
        const g = gift();
        const q = int(giftQtyInput);
        const giftText = g && q >= 1 ? `${q} un. de ${g.name}` : 'o brinde';
        let sentence = '';
        let detail = '';
        const rule = currentRule();
        if (rule === 'kit') {
            const n = int(field('min_qty_kit'));
            const limit = int(field('max_per_order'));
            if (n >= 2) {
                sentence = `A cada kit de ${n} un. do mesmo produto, ganhe ${giftText}`;
                if (limit >= 1) sentence += ` (até ${limit} por pedido)`;
                if (q >= 1) detail = `Ex.: ${n * 2} un. de um produto = ${plural(Math.min(q * 2, limit >= 1 ? limit : Infinity), 'brinde', 'brindes')}.`;
            }
        } else if (rule === 'min_total') {
            const v = money(field('min_value'));
            if (v > 0) {
                sentence = `${productIds.length ? 'Compras dos participantes' : 'Pedidos'} a partir de ${brl(v)}, ganhe ${giftText}`;
                detail = 'Uma vez por pedido, pelo valor cobrado (com promoções e desconto manual).';
            }
        } else {
            const n = int(field('min_qty_min'));
            if (n >= 1) {
                sentence = `A partir de ${n} un. dos participantes, ganhe ${giftText}`;
                detail = 'Uma vez por pedido, somando todos os participantes.';
            }
        }
        if (g && g.stock != null) {
            detail += g.stock > 0
                ? ` Saldo do brinde: ${g.stock} un.`
                : (g.avulso
                    ? ' Atenção: o brinde avulso está sem saldo; ele não sai até a quantidade aumentar.'
                    : ' Atenção: o brinde está sem saldo no evento; sem venda futura liberada, ele não sai.');
        }
        return { sentence, detail: detail.trim() };
    }

    function problems() {
        const out = [];
        if (!form.querySelector('[data-gift-name]')?.value.trim()) out.push({ msg: 'Dê um nome ao brinde', step: 0 });
        if (currentSource() === 'avulso') {
            const r = currentItem();
            if (!r.value) out.push({ msg: 'Escolha o brinde avulso ou cadastre um novo', step: 1 });
            else if (r.value === NEW_ITEM && !form.querySelector('[data-gift-new-name]')?.value.trim()) {
                out.push({ msg: 'Informe o nome do brinde avulso novo', step: 1 });
            }
        } else if (!giftIds.length) {
            out.push({ msg: 'Escolha o produto do brinde', step: 1 });
        }
        if (!(int(giftQtyInput) >= 1)) out.push({ msg: 'Quantidade do brinde: pelo menos 1', step: 1 });
        const rule = currentRule();
        if (rule === 'kit') {
            if (!(int(field('min_qty_kit')) >= 2)) out.push({ msg: 'O kit precisa de pelo menos 2 unidades', step: 2 });
            const raw = String(field('max_per_order')?.value || '').trim();
            if (raw && !(int(field('max_per_order')) >= 1)) out.push({ msg: 'Limite por pedido: 1 ou mais (ou vazio)', step: 2 });
        } else if (rule === 'min_total') {
            if (!(money(field('min_value')) > 0)) out.push({ msg: 'Informe o valor mínimo do pedido', step: 2 });
        } else if (!(int(field('min_qty_min')) >= 1)) {
            out.push({ msg: 'Informe a partir de quantas unidades', step: 2 });
        }
        if (!productIds.length && rule !== 'min_total') out.push({ msg: 'Adicione ao menos um produto participante', step: 3 });
        return out;
    }

    function updateSummary() {
        const name = form.querySelector('[data-gift-name]')?.value.trim();
        const nameEl = form.querySelector('[data-summary-name]');
        if (nameEl) {
            nameEl.textContent = name || 'Sem nome';
            nameEl.classList.toggle('is-empty', !name);
        }
        const { sentence, detail } = ruleSentence();
        const sentenceEl = form.querySelector('[data-summary-sentence]');
        if (sentenceEl) {
            sentenceEl.textContent = sentence || 'Complete a regra para ver como fica.';
            sentenceEl.classList.toggle('is-empty', !sentence);
        }
        const detailEl = form.querySelector('[data-summary-detail]');
        if (detailEl) {
            detailEl.textContent = detail;
            detailEl.hidden = !detail;
            detailEl.classList.toggle('is-warn', detail.includes('Atenção'));
        }
        const chip = form.querySelector('[data-summary-gift]');
        const g = gift();
        if (chip) {
            chip.hidden = !g;
            chip.classList.toggle('gift-chip--avulso', !!g?.avulso);
            if (g) {
                const img = g.image ? `<img src="${esc(g.image)}" alt="" loading="lazy">` : '<i class="fa-solid fa-gift" aria-hidden="true"></i>';
                const q = int(giftQtyInput);
                chip.innerHTML = `<span class="promo-thumb">${img}</span>
                    <span class="gift-chip__text"><small>${g.avulso ? 'Brinde avulso' : 'Brinde'}</small><strong>${q >= 1 ? `${q}× ` : ''}${esc(g.name)}</strong></span>`;
            }
        }
        const countEl = form.querySelector('[data-summary-count]');
        if (countEl) {
            countEl.textContent = !productIds.length && currentRule() === 'min_total'
                ? 'Vale para o pedido inteiro'
                : plural(productIds.length, 'produto participante', 'produtos participantes');
        }
        const missingEl = form.querySelector('[data-summary-missing]');
        if (missingEl) {
            const list = readonly ? [] : problems();
            missingEl.innerHTML = list.map((p) => `<li>${esc(p.msg)}</li>`).join('');
            missingEl.hidden = !list.length;
        }
    }

    form.addEventListener('input', (e) => {
        if (!e.target.closest('.promo-picker')) updateSummary();
    });
    ruleRadios.forEach((r) => r.addEventListener('change', syncRule));
    sourceRadios.forEach((r) => r.addEventListener('change', syncSource));
    itemRadios.forEach((r) => r.addEventListener('change', () => {
        syncSource();
        if (r.value === NEW_ITEM) form.querySelector('[data-gift-new-name]')?.focus();
    }));

    form.addEventListener('submit', (e) => {
        syncRule(); // só a seção da regra escolhida vai no POST
        syncSource();
        const list = problems();
        if (!list.length) return;
        e.preventDefault();
        flash(`${list[0].msg}.`, 'error');
        form.querySelectorAll('.promo-step')[list[0].step]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });

    giftPicker = window.PromoPicker?.mount(giftRoot, {
        single: true,
        inputName: 'gift_product_id',
        onChange: (ids) => { giftIds = ids; updateSummary(); },
    });
    window.PromoPicker?.mount(productsRoot, {
        onChange: (ids) => { productIds = ids; updateSummary(); },
    });
    syncSource();
    syncRule();
})();
