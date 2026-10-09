/**
 * Seletor de produtos do evento (formulários de promoção e de brinde).
 *
 * ``PromoPicker.mount(root, { onChange, single, inputName })``:
 * - busca no catálogo do evento (nome, SKU, categoria, sem acento), clique ou
 *   Enter para adicionar, colar vários SKUs, "adicionar todos os resultados";
 * - ``single``: guarda um produto só (escolher outro troca o atual) e não
 *   oferece colagem nem "adicionar todos";
 * - os escolhidos viram ``<input type="hidden" name="{inputName}">``.
 *
 * O catálogo vem de ``[data-promo-catalog]`` dentro de ``root`` ou do
 * elemento apontado por ``root.dataset.catalogSrc`` (um JSON por página).
 */
(() => {
    'use strict';

    const fold = (text) => String(text ?? '')
        .normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
    const esc = (text) => String(text ?? '').replace(/[&<>"']/g, (c) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
    const brlFmt = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
    const brl = (n) => brlFmt.format(Number(n) || 0);
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    const flash = (msg, cat = 'error') => {
        if (typeof window.showAdminFlash === 'function') window.showAdminFlash(msg, cat);
    };

    const catalogCache = new WeakMap();

    function loadCatalog(root) {
        const src = root.dataset.catalogSrc
            ? document.querySelector(root.dataset.catalogSrc)
            : root.querySelector('[data-promo-catalog]');
        if (src && catalogCache.has(src)) return catalogCache.get(src);
        let list = [];
        try {
            list = JSON.parse(src?.textContent || '[]');
        } catch (_err) {
            list = [];
        }
        const byId = new Map();
        list.forEach((p) => {
            p.id = String(p.id);
            p.hay = fold(`${p.name} ${p.sku} ${p.category} ${p.id}`);
            p.skuFold = fold(p.sku);
            p.nameFold = fold(p.name);
            byId.set(p.id, p);
        });
        const out = { list, byId };
        if (src) catalogCache.set(src, out);
        return out;
    }

    function mount(root, opts = {}) {
        if (!root) return null;
        const single = !!opts.single;
        const inputName = opts.inputName || 'product_ids';
        const onChange = typeof opts.onChange === 'function' ? opts.onChange : () => {};
        const readonly = root.dataset.readonly === '1';
        const { list: catalog, byId } = loadCatalog(root);
        const nounOne = root.dataset.nounOne || (single ? 'produto escolhido' : 'produto participante');
        const nounMany = root.dataset.nounMany || 'produtos participantes';

        let selected = [];
        try {
            selected = JSON.parse(root.dataset.selected || '[]');
        } catch (_err) {
            selected = [];
        }
        selected = [...new Set((Array.isArray(selected) ? selected : [selected]).filter((v) => v != null && v !== '').map(String))];
        if (single) selected = selected.slice(0, 1);

        const productOf = (id) => byId.get(id) || {
            id, name: `Produto #${id}`, sku: '', category: '', price: null, image: '', outsideEvent: true,
        };

        const input = root.querySelector('[data-picker-input]');
        const results = root.querySelector('[data-picker-results]');
        const list = root.querySelector('[data-picker-selected]');
        const empty = root.querySelector('[data-picker-empty]');
        const count = root.querySelector('[data-picker-count]');
        const inputs = root.querySelector('[data-picker-inputs]');
        const clearBtn = root.querySelector('[data-picker-clear]');
        const MAX_RESULTS = 40;
        let options = [];        // ações visíveis no dropdown, na ordem
        let activeIndex = -1;
        let justAdded = new Set();

        const thumb = (p) => (p.image
            ? `<img src="${esc(p.image)}" alt="" loading="lazy">`
            : '<i class="fa-solid fa-box-open" aria-hidden="true"></i>');

        function renderSelected() {
            if (list) {
                list.innerHTML = selected.map((id) => {
                    const p = productOf(id);
                    const stock = Number.isFinite(Number(p.stock)) && !(p.outside_event || p.outsideEvent)
                        ? `<span class="promo-pill${Number(p.stock) > 0 ? '' : ' promo-pill--warn'}">${Number(p.stock)} em estoque</span>` : '';
                    const meta = [p.sku && `SKU ${p.sku}`, p.category].filter(Boolean).map(esc).join(' · ');
                    const outside = p.outside_event || p.outsideEvent
                        ? '<span class="promo-pill promo-pill--warn">fora do evento</span>' : '';
                    const removeLabel = single ? 'Trocar' : 'Remover';
                    const remove = readonly ? '' : `<button type="button" class="promo-picker__remove" data-remove="${esc(id)}" aria-label="${removeLabel} ${esc(p.name)}" title="${removeLabel}"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button>`;
                    return `<li class="promo-picked${justAdded.has(id) ? ' is-new' : ''}" data-id="${esc(id)}">
                        <span class="promo-thumb promo-thumb--md">${thumb(p)}</span>
                        <span class="promo-picked__text"><strong>${esc(p.name)}</strong><small>${meta}${outside}${single ? stock : ''}</small></span>
                        <span class="promo-picked__price">${p.price != null ? brl(p.price) : '—'}</span>
                        ${remove}
                    </li>`;
                }).join('');
            }
            justAdded = new Set();
            if (inputs) {
                inputs.innerHTML = selected.map((id) => `<input type="hidden" name="${esc(inputName)}" value="${esc(id)}">`).join('');
            }
            if (empty) empty.hidden = selected.length > 0;
            if (count) count.textContent = plural(selected.length, nounOne, nounMany);
            if (clearBtn) clearBtn.hidden = single || selected.length < 2;
            // Num seletor de um produto só, a busca some depois da escolha.
            if (single && !readonly) {
                root.querySelector('.promo-picker__search')?.toggleAttribute('hidden', selected.length > 0);
            }
            onChange(selected.slice());
        }

        function add(ids) {
            let n = 0;
            if (single) {
                const key = ids.length ? String(ids[0]) : null;
                if (key && selected[0] !== key) {
                    selected = [key];
                    justAdded.add(key);
                    n = 1;
                }
            } else {
                ids.forEach((id) => {
                    const key = String(id);
                    if (!selected.includes(key)) {
                        selected.push(key);
                        justAdded.add(key);
                        n += 1;
                    }
                });
            }
            if (n) renderSelected();
            return n;
        }

        function remove(id) {
            selected = selected.filter((x) => x !== String(id));
            renderSelected();
            if (single) input?.focus();
        }

        /** Lista de códigos colada (vírgula, ponto e vírgula, quebra de linha ou vários números). */
        function pastedCodes(raw) {
            if (single) return null;
            const parts = String(raw).split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
            const separated = /[,;\n]/.test(raw);
            if (parts.length < 2) return null;
            if (!separated && !parts.every((t) => /^#?\d+$/.test(t))) return null;
            return parts.map((t) => t.replace(/^#/, ''));
        }

        function search(raw) {
            const codes = pastedCodes(raw);
            if (codes) {
                const found = [];
                const missing = [];
                codes.forEach((code) => {
                    const c = fold(code);
                    const p = catalog.find((x) => x.skuFold === c) || byId.get(code);
                    if (p) { if (!found.includes(p.id)) found.push(p.id); } else missing.push(code);
                });
                return { bulk: true, found, missing };
            }
            const terms = fold(raw).split(/\s+/).filter(Boolean);
            if (!terms.length) return { matches: [] };
            const q = terms.join(' ');
            const scored = [];
            catalog.forEach((p) => {
                if (!terms.every((t) => p.hay.includes(t))) return;
                let score = 3;
                if (p.skuFold === q || p.id === q) score = 0;
                else if (p.skuFold.startsWith(q)) score = 1;
                else if (p.nameFold.startsWith(terms[0])) score = 2;
                scored.push([score, p]);
            });
            scored.sort((a, b) => a[0] - b[0] || a[1].name.localeCompare(b[1].name, 'pt-BR'));
            return { matches: scored.map((s) => s[1]) };
        }

        function openResults(open) {
            if (!results || !input) return;
            results.hidden = !open;
            input.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (!open) activeIndex = -1;
        }

        function renderResults() {
            if (!results || !input) return;
            const raw = input.value;
            if (!raw.trim()) { openResults(false); return; }
            const res = search(raw);
            options = [];
            let html = '';
            if (res.bulk) {
                const toAdd = res.found.filter((id) => !selected.includes(id));
                if (toAdd.length) {
                    options.push({ type: 'bulk', ids: toAdd });
                    html += `<button type="button" class="promo-result promo-result--action" role="option" data-opt="0">
                        <span class="promo-result__icon"><i class="fa-solid fa-list-check" aria-hidden="true"></i></span>
                        <span class="promo-result__text"><strong>Adicionar ${plural(toAdd.length, 'produto', 'produtos')} pelos códigos colados</strong>
                        <small>${res.found.length - toAdd.length ? `${res.found.length - toAdd.length} já estava(m) na lista. ` : ''}Enter para confirmar</small></span>
                    </button>`;
                } else if (res.found.length) {
                    html += '<p class="promo-results__note">Todos os códigos encontrados já estão na lista.</p>';
                }
                if (res.missing.length) {
                    html += `<p class="promo-results__note promo-results__note--warn"><i class="fa-solid fa-triangle-exclamation" aria-hidden="true"></i>
                        Não estão no evento: ${esc(res.missing.slice(0, 15).join(', '))}${res.missing.length > 15 ? '…' : ''}</p>`;
                }
            } else {
                const matches = res.matches;
                if (!matches.length) {
                    html = '<p class="promo-results__note">Nenhum produto do evento com essa busca.</p>';
                } else {
                    const notIn = matches.filter((p) => !selected.includes(p.id));
                    if (!single && matches.length > 1 && notIn.length > 1 && matches.length <= 300) {
                        options.push({ type: 'bulk', ids: notIn.map((p) => p.id) });
                    }
                    matches.slice(0, MAX_RESULTS).forEach((p) => options.push({ type: 'one', id: p.id }));
                    html = options.map((opt, i) => {
                        if (opt.type === 'bulk') {
                            return `<button type="button" class="promo-result promo-result--action" role="option" data-opt="${i}">
                                <span class="promo-result__icon"><i class="fa-solid fa-plus" aria-hidden="true"></i></span>
                                <span class="promo-result__text"><strong>Adicionar todos os ${opt.ids.length} resultados</strong>
                                <small>Todos os produtos desta busca que ainda não estão na lista</small></span>
                            </button>`;
                        }
                        const p = byId.get(opt.id);
                        const isIn = selected.includes(p.id);
                        const metaBits = [p.sku && `SKU ${p.sku}`, p.category];
                        if (single && Number.isFinite(Number(p.stock))) metaBits.push(`${Number(p.stock)} em estoque`);
                        const meta = metaBits.filter(Boolean).map(esc).join(' · ');
                        const action = single
                            ? (isIn ? '<i class="fa-solid fa-check" aria-hidden="true"></i> Escolhido' : '<i class="fa-solid fa-gift" aria-hidden="true"></i> Escolher')
                            : (isIn ? '<i class="fa-solid fa-check" aria-hidden="true"></i> Na lista' : '<i class="fa-solid fa-plus" aria-hidden="true"></i> Adicionar');
                        return `<button type="button" class="promo-result${isIn ? ' is-in' : ''}" role="option" aria-selected="${isIn}" data-opt="${i}">
                            <span class="promo-thumb">${thumb(p)}</span>
                            <span class="promo-result__text"><strong>${esc(p.name)}</strong><small>${meta}</small></span>
                            <span class="promo-result__price">${p.price != null ? brl(p.price) : ''}</span>
                            <span class="promo-result__state">${action}</span>
                        </button>`;
                    }).join('');
                    if (matches.length > MAX_RESULTS) {
                        html += `<p class="promo-results__note">Mostrando ${MAX_RESULTS} de ${matches.length}. Refine a busca para ver os outros.</p>`;
                    }
                }
            }
            results.innerHTML = html;
            // Enter adiciona o 1º produto; "adicionar todos" só com clique ou setas.
            // Na colagem de SKUs, a única ação é a de adicionar os códigos.
            const firstOne = options.findIndex((o) => o.type === 'one');
            activeIndex = firstOne >= 0 ? firstOne : (options.length ? 0 : -1);
            paintActive();
            openResults(true);
        }

        function paintActive() {
            results?.querySelectorAll('[data-opt]').forEach((el) => {
                el.classList.toggle('is-active', Number(el.dataset.opt) === activeIndex);
            });
            results?.querySelector('.is-active')?.scrollIntoView({ block: 'nearest' });
        }

        function choose(index) {
            const opt = options[index];
            if (!opt) return;
            if (opt.type === 'bulk') {
                const n = add(opt.ids);
                if (n) flash(`${plural(n, 'produto adicionado', 'produtos adicionados')}.`, 'success');
                input.value = '';
                openResults(false);
            } else if (single) {
                add([opt.id]);
                input.value = '';
                openResults(false);
                return;
            } else if (selected.includes(opt.id)) {
                remove(opt.id);
                renderResults();
            } else {
                add([opt.id]);
                renderResults();
            }
            input.focus();
        }

        if (!readonly && input && results) {
            input.addEventListener('input', renderResults);
            input.addEventListener('focus', () => { if (input.value.trim()) renderResults(); });
            input.addEventListener('keydown', (e) => {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    if (results.hidden) renderResults();
                    if (!options.length) return;
                    e.preventDefault();
                    const step = e.key === 'ArrowDown' ? 1 : -1;
                    activeIndex = (activeIndex + step + options.length) % options.length;
                    paintActive();
                } else if (e.key === 'Enter') {
                    e.preventDefault(); // nunca envia o formulário pela busca
                    if (!results.hidden && activeIndex >= 0) choose(activeIndex);
                } else if (e.key === 'Escape') {
                    if (!results.hidden) { e.preventDefault(); openResults(false); } else input.value = '';
                }
            });
            results.addEventListener('mousedown', (e) => e.preventDefault()); // mantém o foco na busca
            results.addEventListener('click', (e) => {
                const el = e.target.closest('[data-opt]');
                if (el) choose(Number(el.dataset.opt));
            });
            document.addEventListener('click', (e) => {
                if (!root.contains(e.target)) openResults(false);
            });
        }

        // Foto que não carrega (URL antiga, arquivo apagado) vira o ícone de caixa.
        root.addEventListener('error', (e) => {
            const img = e.target;
            if (img.tagName === 'IMG' && img.closest('.promo-thumb')) {
                img.replaceWith(Object.assign(document.createElement('i'), {
                    className: 'fa-solid fa-box-open',
                }));
            }
        }, true);

        list?.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-remove]');
            if (btn) remove(btn.dataset.remove);
        });

        // "Remover todos" pede um segundo clique em vez de abrir um diálogo.
        let clearArmed = null;
        clearBtn?.addEventListener('click', () => {
            if (clearArmed) {
                window.clearTimeout(clearArmed);
                clearArmed = null;
                clearBtn.textContent = 'Remover todos';
                clearBtn.classList.remove('is-armed');
                const n = selected.length;
                selected = [];
                renderSelected();
                flash(`${plural(n, 'produto removido', 'produtos removidos')}.`, 'info');
                return;
            }
            clearBtn.textContent = `Clique de novo para remover os ${selected.length}`;
            clearBtn.classList.add('is-armed');
            clearArmed = window.setTimeout(() => {
                clearArmed = null;
                clearBtn.textContent = 'Remover todos';
                clearBtn.classList.remove('is-armed');
            }, 3500);
        });

        renderSelected();

        return {
            selected: () => selected.slice(),
            productOf,
        };
    }

    window.PromoPicker = { mount };
})();
