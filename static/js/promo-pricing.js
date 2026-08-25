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

    function promoEntryFromFields(src) {
        if (!src) return null;
        const tipoNorm = String(src.promo_tipo || '').trim();
        if (!tipoNorm) return null;
        return {
            promo_id: parseInt(String(src.promo_id || 0), 10) || 0,
            promo_tipo: tipoNorm,
            promo_rule_value: Number(src.promo_rule_value != null ? src.promo_rule_value : src.rule_value) || 0,
            promo_min_qty: Math.max(1, parseInt(String(src.promo_min_qty != null ? src.promo_min_qty : src.min_qty), 10) || 1),
            promo_free_qty: Math.max(0, parseInt(String(src.promo_free_qty != null ? src.promo_free_qty : src.free_qty), 10) || 0),
            promo_nome: src.promo_nome || '',
            promo_badge: src.promo_badge || '',
            promo_bogo_buy_id: parseInt(String(src.promo_bogo_buy_id || src.bogo_buy_product_id || 0), 10) || 0,
            promo_bogo_free_id: parseInt(String(src.promo_bogo_free_id || src.bogo_free_product_id || 0), 10) || 0,
            promo_bogo_buy_sku: src.promo_bogo_buy_sku || src.bogo_buy_sku || '',
            promo_bogo_free_sku: src.promo_bogo_free_sku || src.bogo_free_sku || '',
        };
    }

    function promosOf(product) {
        if (!product) return [];
        if (Array.isArray(product.promos) && product.promos.length) {
            return product.promos.map(promoEntryFromFields).filter(Boolean);
        }
        const one = promoEntryFromFields(product);
        if (one && one.promo_tipo) return [one];
        return [];
    }

    function promoMetaFromProduct(product) {
        const list = promosOf(product);
        if (!list.length) return null;
        return { ...list[0], promos: list };
    }

    function isCrossBogo(item) {
        if (!item || String(item.promo_tipo || '') !== 'bogo') return false;
        const buy = parseInt(String(item.promo_bogo_buy_id), 10) || 0;
        const free = parseInt(String(item.promo_bogo_free_id), 10) || 0;
        return buy > 0 && free > 0 && buy !== free;
    }

    function isSameSkuBogo(item) {
        if (!item || String(item.promo_tipo || '') !== 'bogo') return false;
        const buy = parseInt(String(item.promo_bogo_buy_id), 10) || 0;
        const free = parseInt(String(item.promo_bogo_free_id), 10) || 0;
        return buy > 0 && free > 0 && buy === free;
    }

    function isBogoPairPromo(promo) {
        if (!promo || String(promo.promo_tipo || '') !== 'bogo') return false;
        const buy = parseInt(String(promo.promo_bogo_buy_id), 10) || 0;
        const free = parseInt(String(promo.promo_bogo_free_id), 10) || 0;
        return buy > 0 && free > 0;
    }

    function isCrossBogoPromo(promo) {
        if (!isBogoPairPromo(promo)) return false;
        const buy = parseInt(String(promo.promo_bogo_buy_id), 10) || 0;
        const free = parseInt(String(promo.promo_bogo_free_id), 10) || 0;
        return buy !== free;
    }

    function isSameSkuBogoPromo(promo) {
        if (!isBogoPairPromo(promo)) return false;
        const buy = parseInt(String(promo.promo_bogo_buy_id), 10) || 0;
        const free = parseInt(String(promo.promo_bogo_free_id), 10) || 0;
        return buy === free;
    }

    function stampPromoFields(target, promo) {
        if (!promo) return target;
        target.promo_id = promo.promo_id || 0;
        target.promo_tipo = promo.promo_tipo || '';
        target.promo_rule_value = promo.promo_rule_value || 0;
        target.promo_min_qty = promo.promo_min_qty || 1;
        target.promo_free_qty = promo.promo_free_qty || 0;
        target.promo_nome = promo.promo_nome || '';
        target.promo_badge = promo.promo_badge || target.promo_badge || '';
        target.promo_bogo_buy_id = promo.promo_bogo_buy_id || 0;
        target.promo_bogo_free_id = promo.promo_bogo_free_id || 0;
        target.promo_bogo_buy_sku = promo.promo_bogo_buy_sku || '';
        target.promo_bogo_free_sku = promo.promo_bogo_free_sku || '';
        return target;
    }

    function applyPromoToItem(item) {
        const next = { ...item };
        const qty = Math.max(1, parseInt(String(next.quantidade), 10) || 1);
        const listPrice = Number(next.preco_lista ?? next.preco_original ?? next.preco) || 0;
        next.preco_lista = listPrice;
        next.quantidade = qty;
        if (Array.isArray(item.promos) && item.promos.length) {
            next.promos = item.promos;
        }

        const listSubtotal = round2(listPrice * qty);
        if (next.bogo_auto_free) {
            next.preco = 0;
            next.subtotal = 0;
            next.economia = round2(listPrice * qty);
            next.promo_aplicada = true;
            next.bogo_free_units = qty;
            next.em_promocao = true;
            return next;
        }

        const candidates = promosOf(next).filter((promo) => {
            if (!promo.promo_tipo || promo.promo_tipo === 'combo_bundle') return false;
            if (isBogoPairPromo(promo)) return false;
            return true;
        });

        if (!candidates.length) {
            next.preco = listPrice;
            next.subtotal = listSubtotal;
            next.economia = 0;
            next.promo_aplicada = false;
            return next;
        }

        let best = null;
        let bestSubtotal = listSubtotal;
        candidates.forEach((promo) => {
            const eff = computeEffectiveSubtotal(
                promo.promo_tipo,
                promo.promo_rule_value,
                promo.promo_min_qty,
                promo.promo_free_qty,
                listPrice,
                qty,
            );
            if (eff < bestSubtotal - 0.001) {
                bestSubtotal = eff;
                best = promo;
            }
        });

        if (best) {
            stampPromoFields(next, best);
            next.subtotal = bestSubtotal;
            next.economia = round2(listSubtotal - bestSubtotal);
            next.promo_aplicada = true;
            const isPack = next.promo_tipo === 'exact_bundle' || next.promo_tipo === 'min_bundle';
            if (isPack) {
                const { extra } = packGroupsAndExtra(qty, next.promo_min_qty);
                next.preco = extra > 0
                    ? listPrice
                    : (qty > 0 ? round2(bestSubtotal / qty) : listPrice);
            } else {
                next.preco = qty > 0 ? round2(bestSubtotal / qty) : listPrice;
            }
        } else {
            next.subtotal = listSubtotal;
            next.preco = listPrice;
            next.economia = 0;
            next.promo_aplicada = false;
        }
        return next;
    }

    function lookupCatalogProduct(id) {
        const want = String(id);
        const list = window.__PRODUCTS__;
        if (!Array.isArray(list)) return null;
        return list.find((p) => String(p.id) === want) || null;
    }

    function clampGiftQty(qty, product, existing, reservedPaidQty) {
        let n = Math.max(0, parseInt(String(qty), 10) || 0);
        if (n <= 0) return 0;
        const stock = Number(product?.estoque ?? existing?.estoque);
        const blRaw = Number(product?.backorder_limit ?? existing?.backorder_limit);
        const bl = Number.isFinite(blRaw) ? blRaw : -1;
        const stockN = Number.isFinite(stock) ? Math.max(0, Math.floor(stock)) : null;
        const reserved = Math.max(0, parseInt(String(reservedPaidQty), 10) || 0);
        const available = stockN == null ? null : Math.max(0, stockN - reserved);
        if (window.__SELLER_BACKORDER__) {
            if (bl === 0) {
                if (available == null) return n;
                if (available <= 0) return 0;
                return Math.min(n, available);
            }
            return n;
        }
        if (available != null) {
            if (available <= 0) return 0;
            return Math.min(n, available);
        }
        return n;
    }

    function buildGiftItem(product, buyItem, qty) {
        const promo = promoMetaFromProduct(product) || {};
        const listPrice = Number(product.preco_original ?? product.preco) || 0;
        return {
            id: product.id,
            sku: product.sku || '',
            nome: product.nome,
            variante: product.variante || '',
            categoria: product.categoria,
            preco_lista: listPrice,
            preco: listPrice,
            imagem: product.imagem,
            estoque: Number.isFinite(Number(product.estoque)) ? Number(product.estoque) : undefined,
            backorder_limit: Number.isFinite(Number(product.backorder_limit)) ? Number(product.backorder_limit) : -1,
            quantidade: qty,
            em_promocao: true,
            promo_tipo: 'bogo',
            promo_rule_value: Number(buyItem.promo_rule_value || promo.promo_rule_value) || 0,
            promo_min_qty: Number(buyItem.promo_min_qty || promo.promo_min_qty) || 1,
            promo_free_qty: Number(buyItem.promo_free_qty || promo.promo_free_qty) || 0,
            promo_nome: buyItem.promo_nome || promo.promo_nome || '',
            promo_badge: promo.promo_badge || buyItem.promo_badge || '',
            promo_bogo_buy_id: Number(buyItem.promo_bogo_buy_id) || 0,
            promo_bogo_free_id: Number(buyItem.promo_bogo_free_id) || Number(product.id) || 0,
            promo_bogo_buy_sku: buyItem.promo_bogo_buy_sku || '',
            promo_bogo_free_sku: product.sku || buyItem.promo_bogo_free_sku || '',
            bogo_auto_free: true,
            subtotal: 0,
            economia: 0,
            promo_aplicada: true,
        };
    }

    function syncBogoGiftItems(items) {
        const list = Array.isArray(items) ? items.map((i) => ({ ...i })) : [];
        const groups = {};

        list.forEach((item) => {
            if (item.bogo_auto_free) return;
            const buy = parseInt(String(item.promo_bogo_buy_id), 10) || 0;
            const free = parseInt(String(item.promo_bogo_free_id), 10) || 0;
            if (String(item.promo_tipo || '') !== 'bogo' || !buy || !free) return;
            if (String(item.id) !== String(buy)) return;
            const key = [buy, free, item.promo_min_qty || 0, item.promo_free_qty || 0].join('|');
            if (!groups[key]) {
                groups[key] = {
                    buyId: buy,
                    freeId: free,
                    sameSku: buy === free,
                    buyQty: 0,
                    meta: item,
                };
            }
            groups[key].buyQty += Math.max(0, parseInt(String(item.quantidade), 10) || 0);
        });

        const keepFree = new Set();

        Object.values(groups).forEach((g) => {
            const minQ = Math.max(1, parseInt(String(g.meta.promo_min_qty), 10) || 1);
            const freeQ = Math.max(0, parseInt(String(g.meta.promo_free_qty), 10) || 0);
            const granted = freeQ > 0 ? Math.floor(g.buyQty / minQ) * freeQ : 0;
            const freeId = String(g.freeId);
            const keepKey = g.sameSku ? `same:${freeId}` : freeId;
            const existingIdx = list.findIndex((i) => {
                if (String(i.id) !== freeId) return false;
                return g.sameSku ? !!i.bogo_auto_free : true;
            });
            const existing = existingIdx >= 0 ? list[existingIdx] : null;
            const product = lookupCatalogProduct(g.freeId)
                || (existing && !existing.bogo_auto_free ? null : existing);
            const reservedPaid = g.sameSku ? g.buyQty : 0;
            const qty = clampGiftQty(granted, product || existing, existing, reservedPaid);

            if (qty <= 0) {
                if (existing && existing.bogo_auto_free) {
                    list.splice(existingIdx, 1);
                }
                return;
            }

            keepFree.add(keepKey);

            if (!existing || (g.sameSku && existing && !existing.bogo_auto_free)) {
                const source = lookupCatalogProduct(g.freeId) || existing || g.meta;
                if (!source) return;
                const gift = buildGiftItem(source, g.meta, qty);
                const buyIdx = list.findIndex((i) => String(i.id) === String(g.buyId) && !i.bogo_auto_free);
                list.splice(buyIdx >= 0 ? buyIdx + 1 : list.length, 0, gift);
                return;
            }

            if (existing.bogo_auto_free) {
                existing.quantidade = qty;
                existing.em_promocao = true;
                existing.promo_tipo = 'bogo';
                existing.promo_bogo_buy_id = g.buyId;
                existing.promo_bogo_free_id = g.freeId;
                existing.promo_min_qty = g.meta.promo_min_qty;
                existing.promo_free_qty = g.meta.promo_free_qty;
                existing.promo_nome = existing.promo_nome || g.meta.promo_nome;
                return;
            }

            if ((parseInt(String(existing.quantidade), 10) || 0) < qty) {
                existing.quantidade = qty;
            }
            existing.em_promocao = true;
            existing.promo_tipo = existing.promo_tipo || 'bogo';
            existing.promo_bogo_buy_id = existing.promo_bogo_buy_id || g.buyId;
            existing.promo_bogo_free_id = existing.promo_bogo_free_id || g.freeId;
        });

        return list.filter((item) => {
            if (!item.bogo_auto_free) return true;
            const id = String(item.id);
            return keepFree.has(id) || keepFree.has(`same:${id}`);
        });
    }

    function orderBogoGifts(items) {
        const used = new Set();
        const out = [];
        items.forEach((item, idx) => {
            if (used.has(idx) || item.bogo_auto_free) return;
            out.push(item);
            used.add(idx);
            const buy = parseInt(String(item.promo_bogo_buy_id), 10) || 0;
            const free = parseInt(String(item.promo_bogo_free_id), 10) || 0;
            if (String(item.promo_tipo || '') !== 'bogo' || !buy || !free) return;
            if (String(item.id) !== String(buy)) return;
            items.forEach((gift, j) => {
                if (used.has(j)) return;
                if (String(gift.id) !== String(free)) return;
                if (buy === free && !gift.bogo_auto_free) return;
                out.push(gift);
                used.add(j);
            });
        });
        items.forEach((item, idx) => {
            if (!used.has(idx)) out.push(item);
        });
        return out;
    }

    function applyBogoCross(all) {
        const groups = {};
        all.forEach((item, idx) => {
            if (!isCrossBogo(item)) return;
            const key = [
                item.promo_bogo_buy_id || 0,
                item.promo_bogo_free_id || 0,
                item.promo_min_qty || 0,
                item.promo_free_qty || 0,
                item.promo_nome || '',
            ].join('|');
            if (!groups[key]) groups[key] = { buy: [], free: [], meta: item };
            const id = String(item.id);
            if (id === String(item.promo_bogo_buy_id)) groups[key].buy.push(idx);
            if (id === String(item.promo_bogo_free_id)) groups[key].free.push(idx);
        });

        Object.values(groups).forEach(g => {
            if (!g.buy.length || !g.free.length) return;
            const minQ = Math.max(1, parseInt(String(g.meta.promo_min_qty), 10) || 1);
            const freeQ = Math.max(0, parseInt(String(g.meta.promo_free_qty), 10) || 0);
            if (freeQ <= 0) return;
            const buyQty = g.buy.reduce((acc, i) => acc + (parseInt(String(all[i].quantidade), 10) || 0), 0);
            if (buyQty < minQ) return;
            let remaining = Math.floor(buyQty / minQ) * freeQ;
            g.free.forEach(i => {
                if (remaining <= 0) return;
                const qty = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const take = Math.min(qty, remaining);
                if (take <= 0) return;
                const lp = Number(all[i].preco_lista) || 0;
                const orig = round2(lp * qty);
                const paidQty = qty - take;
                const nextSub = round2(lp * paidQty);
                if (nextSub < orig) {
                    all[i].subtotal = nextSub;
                    all[i].economia = round2(orig - nextSub);
                    all[i].promo_aplicada = true;
                    all[i].preco = qty > 0 ? round2(nextSub / qty) : lp;
                    all[i].bogo_free_units = take;
                }
                remaining -= take;
            });
        });
    }

    function applyComboBundle(all) {
        const comboGroups = {};
        all.forEach((item, idx) => {
            promosOf(item).forEach((promo) => {
                if (promo.promo_tipo !== 'combo_bundle') return;
                const key = [
                    promo.promo_id || 0,
                    promo.promo_nome || '',
                    promo.promo_rule_value || 0,
                ].join('|');
                if (!comboGroups[key]) {
                    comboGroups[key] = { indices: [], meta: promo };
                }
                if (!comboGroups[key].indices.includes(idx)) {
                    comboGroups[key].indices.push(idx);
                }
            });
        });

        Object.values(comboGroups).forEach((group) => {
            const indices = group.indices;
            if (indices.length < 2) return;
            const comboTotal = Math.max(0, Number(group.meta.promo_rule_value) || 0);
            if (comboTotal <= 0) return;

            const qtyPerItem = indices.map(i =>
                Math.max(0, parseInt(String(all[i].quantidade), 10) || 0)
            );
            const numCombos = Math.min(...qtyPerItem);
            if (numCombos <= 0) return;

            let originalComboSub = 0;
            indices.forEach(i => {
                const lp = Number(all[i].preco_lista) || 0;
                const inCombo = Math.min(parseInt(String(all[i].quantidade), 10) || 0, numCombos);
                originalComboSub += round2(inCombo * lp);
            });

            const promoSub = round2(numCombos * comboTotal);
            if (promoSub >= originalComboSub) return;

            let currentGroup = 0;
            indices.forEach(i => {
                currentGroup += Number(all[i].subtotal) || 0;
            });
            let proposedGroup = 0;
            const proposed = [];
            indices.forEach(i => {
                const qty = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const lp = Number(all[i].preco_lista) || 0;
                const inCombo = Math.min(qty, numCombos);
                const extra = qty - inCombo;
                const share = originalComboSub > 0 ? round2(inCombo * lp) / originalComboSub : 0;
                const itemPromo = round2(promoSub * share);
                const itemTotal = round2(itemPromo + extra * lp);
                proposed.push(itemTotal);
                proposedGroup += itemTotal;
            });
            if (proposedGroup >= currentGroup - 0.001) return;

            indices.forEach((i, n) => {
                const qty = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const lp = Number(all[i].preco_lista) || 0;
                const itemOrig = round2(lp * qty);
                const itemTotal = proposed[n];
                if (itemTotal < itemOrig) {
                    stampPromoFields(all[i], group.meta);
                    all[i].subtotal = itemTotal;
                    all[i].economia = round2(itemOrig - itemTotal);
                    all[i].promo_aplicada = true;
                    all[i].preco = qty > 0 ? round2(itemTotal / qty) : lp;
                }
            });
        });
    }

    function recalculateItems(items) {
        const synced = syncBogoGiftItems(items || []);
        const all = synced.map(applyPromoToItem);

        applyBogoCross(all);
        applyComboBundle(all);

        const bundleGroups = {};
        all.forEach((item, idx) => {
            promosOf(item).forEach((promo) => {
                if (promo.promo_tipo !== 'exact_bundle') return;
                const key = [
                    promo.promo_id || 0,
                    promo.promo_nome || '',
                    promo.promo_min_qty || 0,
                    promo.promo_rule_value || 0,
                ].join('|');
                if (!bundleGroups[key]) {
                    bundleGroups[key] = { indices: [], meta: promo };
                }
                if (!bundleGroups[key].indices.includes(idx)) {
                    bundleGroups[key].indices.push(idx);
                }
            });
        });

        Object.values(bundleGroups).forEach((group) => {
            const indices = group.indices;
            if (indices.length < 2) return;
            const minQ = Math.max(2, parseInt(String(group.meta.promo_min_qty), 10) || 2);
            const packTotal = Math.max(0, Number(group.meta.promo_rule_value) || 0);
            let totalQty = 0;
            let originalSubtotal = 0;
            let currentSubtotal = 0;
            indices.forEach(i => {
                const q = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                const lp = Number(all[i].preco_lista) || 0;
                totalQty += q;
                originalSubtotal += round2(lp * q);
                currentSubtotal += Number(all[i].subtotal) || 0;
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
            if (promoTotal >= originalSubtotal - 0.001) return;
            if (promoTotal >= currentSubtotal - 0.001) return;

            indices.forEach(i => {
                const q = Math.max(0, parseInt(String(all[i].quantidade), 10) || 0);
                if (q <= 0) return;
                const lp = Number(all[i].preco_lista) || 0;
                const extraOnItem = itemExtra[i] || 0;
                all[i].bundle_groups = groups;
                all[i].bundle_extra = extra;
                all[i].bundle_item_extra = extraOnItem;
                const itemOrig = round2(lp * q);
                const share = originalSubtotal > 0 ? itemOrig / originalSubtotal : 0;
                const itemPromo = round2(promoTotal * share);
                if (itemPromo < itemOrig) {
                    stampPromoFields(all[i], group.meta);
                    all[i].subtotal = itemPromo;
                    all[i].economia = round2(itemOrig - itemPromo);
                    all[i].promo_aplicada = true;
                    all[i].preco = extraOnItem > 0
                        ? lp
                        : (q > 0 ? round2(itemPromo / q) : lp);
                }
            });
        });

        return orderBogoGifts(all);
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
    function formatBogoFreeQtyMeta(item, formatBRL) {
        const freeUnits = parseInt(String(item.bogo_free_units), 10) || 0;
        if (freeUnits <= 0) return '';
        const qty = Math.max(0, parseInt(String(item.quantidade), 10) || 0);
        const listUnit = Number(item.preco_lista) || Number(item.preco) || 0;
        if (freeUnits >= qty) {
            return `${qty} un. <strong>GRÁTIS</strong>`;
        }
        const paid = qty - freeUnits;
        return `${paid} × ${formatBRL(listUnit)} + ${freeUnits} un. <strong>GRÁTIS</strong>`;
    }

    function renderLineItemHtml(item, formatBRL, articleClass, options = {}) {
        const qty = Number(item.quantidade) || 0;
        const freeUnits = parseInt(String(item.bogo_free_units), 10) || 0;
        const isFullyFree = !!item.bogo_auto_free || (freeUnits > 0 && freeUnits >= qty);
        const bogoFreeMeta = formatBogoFreeQtyMeta(item, formatBRL);
        const bundleMeta = formatBundleQtyMeta(item, formatBRL);
        const unit = formatBRL(item.preco);
        const subtotalValue = item.subtotal != null ? item.subtotal : item.preco * qty;
        const subtotal = isFullyFree ? '<strong class="line-item__free-tag">GRÁTIS</strong>' : formatBRL(subtotalValue);
        const listUnit = Number(item.preco_lista) || Number(item.preco) || 0;
        const showOriginal = !bundleMeta && !bogoFreeMeta && item.promo_aplicada && listUnit > Number(item.preco) + 0.001;
        const unitHtml = showOriginal
            ? `<span class="line-item__price-original">${formatBRL(listUnit)}</span> ${unit}`
            : unit;
        const qtyMeta = bogoFreeMeta || bundleMeta || `${qty} × ${unitHtml}`;
        const promoHint = item.promo_aplicada && item.promo_nome
            ? `<p class="line-item__promo"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${escapeHtml(item.promo_nome)}</p>`
            : '';
        const badge = item.promo_aplicada && item.promo_badge && !item.promo_nome
            ? `<p class="line-item__promo"><i class="fa-solid fa-tag" aria-hidden="true"></i> ${escapeHtml(item.promo_badge)}</p>`
            : '';
        const backorderIcon = backorderIndicatorHtml(item, articleClass);
        const backorderClass = backorderIcon ? ` ${articleClass}--backorder` : '';
        const freeClass = (isFullyFree || item.bogo_auto_free) ? ` ${articleClass}--free` : '';
        const removable = !!options.removable && !item.bogo_auto_free;
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
            <article class="${articleClass}${backorderClass}${freeClass}" data-id="${item.id}">
                <div class="${articleClass}__image">
                    <img src="${item.imagem || ''}" alt="${escapeHtml(item.nome)}" loading="lazy">
                </div>
                <div class="${articleClass}__info">
                    <span class="${articleClass}__category">${escapeHtml(item.categoria || '')}</span>
                    <div class="${articleClass}__name-row">
                        <h3 class="${articleClass}__name">${escapeHtml(item.nome)}</h3>
                        ${backorderIcon}
                    </div>
                    ${item.variante ? `<p class="${articleClass}__variant">${escapeHtml(item.variante)}</p>` : ''}
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
