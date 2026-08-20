/**
 * Cálculo de promoções no carrinho (espelha database/promotions.py).
 */
(() => {
    'use strict';

    function round2(n) {
        return Math.round((Number(n) + Number.EPSILON) * 100) / 100;
    }

    function packGroupsAndExtra(qty, packQty) {
        const pack = Math.max(2, parseInt(String(packQty), 10) || 2);
        const q = Math.max(0, parseInt(String(qty), 10) || 0);
        return { groups: Math.floor(q / pack), extra: q % pack, pack };
    }

    function packSubtotal(qty, packQty, packTotal, listPrice) {
        const { groups, extra } = packGroupsAndExtra(qty, packQty);
        return round2(groups * Math.max(0, Number(packTotal) || 0) + extra * (Number(listPrice) || 0));
    }

    function computeEffectiveSubtotal(ruleType, ruleValue, minQty, freeQty, listPrice, qty) {
        const list = Number(listPrice) || 0;
        const q = Math.max(0, parseInt(String(qty), 10) || 0);
        if (q <= 0) return 0;

        const rt = String(ruleType || '').trim();
        if (rt === 'percent') {
            const pct = Math.max(0, Math.min(100, Number(ruleValue) || 0));
            return round2(list * q * (1 - pct / 100));
        }
        if (rt === 'fixed') {
            const discount = Math.max(0, Number(ruleValue) || 0);
            return round2(Math.max(0, list - discount) * q);
        }
        if (rt === 'bogo') {
            const minQ = Math.max(1, parseInt(String(minQty), 10) || 1);
            const freeQ = Math.max(0, parseInt(String(freeQty), 10) || 0);
            if (freeQ === 0) return round2(list * q);
            const group = minQ + freeQ;
            const groups = Math.floor(q / group);
            const rem = q % group;
            const paid = groups * minQ + Math.min(rem, minQ);
            return round2(list * paid);
        }
        if (rt === 'min_bundle') {
            const minQ = Math.max(2, parseInt(String(minQty), 10) || 2);
            if (q < minQ) return round2(list * q);
            const eff = packSubtotal(q, minQ, ruleValue, list);
            if (eff >= round2(list * q)) return round2(list * q);
            return eff;
        }
        if (rt === 'exact_bundle') {
            // Kit de minQ por ruleValue; unidades além do pacote (ex.: 6ª) no preço de lista.
            const minQ = Math.max(2, parseInt(String(minQty), 10) || 2);
            const { groups } = packGroupsAndExtra(q, minQ);
            if (groups <= 0) return round2(list * q);
            const eff = packSubtotal(q, minQ, ruleValue, list);
            if (eff >= round2(list * q)) return round2(list * q);
            return eff;
        }
        return round2(list * q);
    }

    function extraBitText(extra, listUnit, formatBRL) {
        if (extra <= 0) return '';
        return extra === 1
            ? `1 un. a ${formatBRL(listUnit)}`
            : `${extra} un. a ${formatBRL(listUnit)}`;
    }

    function packBitText(groups, minQ, bundleTotal, formatBRL) {
        if (groups <= 0) return '';
        return groups === 1
            ? `1 pacote de ${minQ} un. por ${formatBRL(bundleTotal)}`
            : `${groups} pacotes de ${minQ} un. por ${formatBRL(bundleTotal)} cada`;
    }

    function formatBundleQtyMeta(item, formatBRL) {
        const tipo = String(item && item.promo_tipo ? item.promo_tipo : '');
        if (tipo !== 'exact_bundle' && tipo !== 'min_bundle') return '';
        if (!item || !item.promo_aplicada) return '';
        const minQ = Math.max(2, parseInt(String(item.promo_min_qty), 10) || 2);
        const qty = Math.max(0, parseInt(String(item.quantidade), 10) || 0);
        const bundleTotal = Math.max(0, Number(item.promo_rule_value) || 0);
        const listUnit = Number(item.preco_lista) || Number(item.preco) || 0;

        const grouped = Number.isFinite(Number(item.bundle_groups));
        const groups = grouped
            ? Math.max(0, parseInt(String(item.bundle_groups), 10) || 0)
            : packGroupsAndExtra(qty, minQ).groups;
        const extra = grouped
            ? Math.max(0, parseInt(String(item.bundle_item_extra), 10) || 0)
            : packGroupsAndExtra(qty, minQ).extra;
        const inPack = grouped
            ? Math.max(0, qty - extra)
            : qty - extra;

        const packBit = inPack > 0 ? packBitText(groups, minQ, bundleTotal, formatBRL) : '';
        const extraBit = extraBitText(extra, listUnit, formatBRL);
        if (packBit && extraBit) return `${packBit} + ${extraBit}`;
        return packBit || extraBit;
    }

    function promoMetaFromProduct(product) {
        if (!product || !product.em_promocao) return null;
        return {
            promo_tipo: product.promo_tipo || '',
            promo_rule_value: Number(product.promo_rule_value) || 0,
            promo_min_qty: Math.max(1, parseInt(String(product.promo_min_qty), 10) || 1),
            promo_free_qty: Math.max(0, parseInt(String(product.promo_free_qty), 10) || 0),
            promo_nome: product.promo_nome || '',
            promo_badge: product.promo_badge || '',
        };
    }

    function applyPromoToItem(item) {
        const next = { ...item };
        const qty = Math.max(1, parseInt(String(next.quantidade), 10) || 1);
        const listPrice = Number(next.preco_lista ?? next.preco_original ?? next.preco) || 0;
        next.preco_lista = listPrice;
        next.quantidade = qty;

        const listSubtotal = round2(listPrice * qty);
        if (!next.em_promocao || !next.promo_tipo) {
            next.preco = listPrice;
            next.subtotal = listSubtotal;
            next.economia = 0;
            next.promo_aplicada = false;
            return next;
        }

        const effSubtotal = computeEffectiveSubtotal(
            next.promo_tipo,
            next.promo_rule_value,
            next.promo_min_qty,
            next.promo_free_qty,
            listPrice,
            qty,
        );
        const hasDiscount = effSubtotal < listSubtotal - 0.001;
        if (hasDiscount) {
            next.subtotal = effSubtotal;
            next.economia = round2(listSubtotal - effSubtotal);
            next.promo_aplicada = true;
            const isPack = next.promo_tipo === 'exact_bundle' || next.promo_tipo === 'min_bundle';
            if (isPack) {
                const { extra } = packGroupsAndExtra(qty, next.promo_min_qty);
                // Não diluir o preço do kit nas unidades avulsas (6ª un. permanece no preço de lista).
                next.preco = extra > 0
                    ? listPrice
                    : (qty > 0 ? round2(effSubtotal / qty) : listPrice);
            } else {
                next.preco = qty > 0 ? round2(effSubtotal / qty) : listPrice;
            }
        } else {
            // Regra não atingida (ex.: qty < min_qty) ou bundle mais caro → preço de lista.
            next.subtotal = listSubtotal;
            next.preco = listPrice;
            next.economia = 0;
            next.promo_aplicada = false;
        }
        return next;
    }

    function recalculateItems(items) {
        const all = (items || []).map(applyPromoToItem);

        const bundleGroups = {};
        all.forEach((item, idx) => {
            if (!item.em_promocao || item.promo_tipo !== 'exact_bundle') return;
            const key = [
                item.promo_nome || '',
                item.promo_min_qty || 0,
                item.promo_rule_value || 0,
            ].join('|');
            if (!bundleGroups[key]) bundleGroups[key] = [];
            bundleGroups[key].push(idx);
        });

        Object.values(bundleGroups).forEach(indices => {
            const minQ = Math.max(2, parseInt(String(all[indices[0]].promo_min_qty), 10) || 2);
            const packTotal = Math.max(0, Number(all[indices[0]].promo_rule_value) || 0);
            let totalQty = 0;
            let originalSubtotal = 0;
            indices.forEach(i => {
                const q = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const lp = Number(all[i].preco_lista) || 0;
                totalQty += q;
                originalSubtotal += round2(lp * q);
            });
            const groups = Math.floor(totalQty / minQ);
            if (groups <= 0) return;
            const extra = totalQty % minQ;
            const itemExtra = {};
            let extraRemaining = extra;
            let extraSub = 0;
            for (let j = indices.length - 1; j >= 0 && extraRemaining > 0; j--) {
                const i = indices[j];
                const q = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const take = Math.min(q, extraRemaining);
                itemExtra[i] = take;
                const lp = Number(all[i].preco_lista) || 0;
                extraSub += round2(take * lp);
                extraRemaining -= take;
            }
            const bundleSub = round2(groups * packTotal);
            const promoTotal = round2(bundleSub + extraSub);
            const applyDiscount = promoTotal < originalSubtotal - 0.001;

            indices.forEach(i => {
                const q = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                if (q <= 0) return;
                const lp = Number(all[i].preco_lista) || 0;
                const extraOnItem = itemExtra[i] || 0;
                all[i].bundle_groups = groups;
                all[i].bundle_extra = extra;
                all[i].bundle_item_extra = extraOnItem;

                if (!applyDiscount) return;
                const itemOrig = round2(lp * q);
                const share = originalSubtotal > 0 ? itemOrig / originalSubtotal : 0;
                const itemPromo = round2(promoTotal * share);
                if (itemPromo < itemOrig) {
                    all[i].subtotal = itemPromo;
                    all[i].economia = round2(itemOrig - itemPromo);
                    all[i].promo_aplicada = true;
                    all[i].preco = extraOnItem > 0
                        ? lp
                        : (q > 0 ? round2(itemPromo / q) : lp);
                }
            });
        });

        return all;
    }

    function getTotals(items) {
        const priced = recalculateItems(items);
        const subtotalLista = round2(
            priced.reduce(
                (acc, i) => acc + (Number(i.preco_lista) || Number(i.preco) || 0) * (Number(i.quantidade) || 0),
                0,
            ),
        );
        const total = round2(priced.reduce((acc, i) => acc + (Number(i.subtotal) || 0), 0));
        const economiaTotal = round2(Math.max(0, subtotalLista - total));
        const count = priced.reduce((acc, i) => acc + (Number(i.quantidade) || 0), 0);
        return { items: priced, total, subtotalLista, economiaTotal, count };
    }

    function escapeHtml(text) {
        return String(text || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    /**
     * Ícone minimalista para itens acima do estoque (painel do vendedor).
     * Retorna string vazia quando não há retirada posterior pendente.
     */
    function backorderIndicatorHtml(item, articleClass) {
        if (!window.__SELLER_BACKORDER__) return '';
        const bl = Number(item.backorder_limit);
        if (Number.isFinite(bl) && bl === 0) return '';
        const stock = Number(item.estoque);
        if (!Number.isFinite(stock)) return '';
        const qty = Math.max(0, Number(item.quantidade) || 0);
        const available = Math.max(0, stock);
        const missing = qty - available;
        if (missing <= 0) return '';
        const label = available <= 0
            ? 'Sem estoque — retirada posterior pelo cliente'
            : `${missing} de ${qty} un. sem estoque — retirada posterior pelo cliente`;
        return (
            `<span class="${articleClass}__backorder" title="${escapeHtml(label)}" `
            + `role="img" aria-label="${escapeHtml(label)}">`
            + `<i class="fa-solid fa-box-open" aria-hidden="true"></i></span>`
        );
    }

    /**
     * HTML de linha para carrinho / pagamento.
     * @param {object} item
     * @param {function} formatBRL
     * @param {string} articleClass - ex. 'cart-item' ou 'payment-item'
     */
    function renderLineItemHtml(item, formatBRL, articleClass, options = {}) {
        const qty = Number(item.quantidade) || 0;
        const bundleMeta = formatBundleQtyMeta(item, formatBRL);
        const unit = formatBRL(item.preco);
        const subtotal = formatBRL(item.subtotal != null ? item.subtotal : item.preco * qty);
        const listUnit = Number(item.preco_lista) || Number(item.preco) || 0;
        const showOriginal = !bundleMeta && item.promo_aplicada && listUnit > Number(item.preco) + 0.001;
        const unitHtml = showOriginal
            ? `<span class="line-item__price-original">${formatBRL(listUnit)}</span> ${unit}`
            : unit;
        const qtyMeta = bundleMeta || `${qty} × ${unitHtml}`;
        const promoHint = item.promo_aplicada && item.promo_nome
            ? `<p class="line-item__promo"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${escapeHtml(item.promo_nome)}</p>`
            : '';
        const badge = item.promo_aplicada && item.promo_badge && !item.promo_nome
            ? `<p class="line-item__promo"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${escapeHtml(item.promo_badge)}</p>`
            : '';
        const backorderIcon = backorderIndicatorHtml(item, articleClass);
        const backorderClass = backorderIcon ? ` ${articleClass}--backorder` : '';
        const removable = !!options.removable;
        const removeBtn = removable
            ? `<button type="button" class="${articleClass}__remove" data-payment-action="remove" aria-label="Remover ${escapeHtml(item.nome)}">
                    <i class="fa-solid fa-trash" aria-hidden="true"></i>
               </button>`
            : '';
        const totalCol = removable
            ? `<div class="${articleClass}__side">
                    <div class="${articleClass}__total">${subtotal}</div>
                    ${removeBtn}
               </div>`
            : `<div class="${articleClass}__total">${subtotal}</div>`;

        return `
            <article class="${articleClass}${backorderClass}" data-id="${item.id}">
                <div class="${articleClass}__image">
                    <img src="${item.imagem || ''}" alt="${escapeHtml(item.nome)}" loading="lazy">
                </div>
                <div class="${articleClass}__info">
                    <span class="${articleClass}__category">${escapeHtml(item.categoria || '')}</span>
                    <div class="${articleClass}__name-row">
                        <h3 class="${articleClass}__name">${escapeHtml(item.nome)}</h3>
                        ${backorderIcon}
                    </div>
                    ${item.sku ? `<p class="${articleClass}__sku">SKU ${escapeHtml(item.sku)}</p>` : ''}
                    <p class="${articleClass}__meta">${qtyMeta}</p>
                    ${promoHint || badge}
                </div>
                ${totalCol}
            </article>
        `;
    }

    window.PromoPricing = {
        computeEffectiveSubtotal,
        formatBundleQtyMeta,
        promoMetaFromProduct,
        applyPromoToItem,
        recalculateItems,
        getTotals,
        renderLineItemHtml,
        backorderIndicatorHtml,
    };
})();
