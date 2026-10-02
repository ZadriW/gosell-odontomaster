(() => {
    'use strict';

    const ITEMS_PER_PAGE = 2;
    const SWIPE_THRESHOLD_PX = 40;
    const MOBILE_MQ = window.matchMedia('(max-width: 767px)');

    function initNavSlide(nav) {
        const viewport = nav.querySelector('[data-admin-nav-viewport]');
        const track = nav.querySelector('[data-admin-nav-track]');
        const prevBtn = nav.querySelector('[data-admin-nav-prev]');
        const nextBtn = nav.querySelector('[data-admin-nav-next]');
        const pager = nav.querySelector('[data-admin-nav-pager]');
        if (!viewport || !track || !prevBtn || !nextBtn) return;

        let page = 0;
        let pages = 1;
        let touchStartX = 0;

        function items() {
            return Array.from(track.querySelectorAll('.admin-nav__item'));
        }

        function syncLayout() {
            items().forEach(el => {
                el.style.flexBasis = '';
                el.style.width = '';
            });
            track.style.width = '';

            if (!MOBILE_MQ.matches) return;

            const pageWidth = viewport.clientWidth;
            if (!pageWidth) return;

            const itemWidth = pageWidth / ITEMS_PER_PAGE;
            items().forEach(el => {
                el.style.flexBasis = `${itemWidth}px`;
                el.style.width = `${itemWidth}px`;
            });
            track.style.width = `${itemWidth * items().length}px`;
        }

        function calcPages() {
            return Math.max(1, Math.ceil(items().length / ITEMS_PER_PAGE));
        }

        function pageForActiveItem() {
            const activeIdx = items().findIndex(el => el.classList.contains('is-active'));
            if (activeIdx < 0) return page;
            return Math.floor(activeIdx / ITEMS_PER_PAGE);
        }

        function setControlsVisibility() {
            const showControls = MOBILE_MQ.matches && pages > 1;
            prevBtn.hidden = !showControls;
            nextBtn.hidden = !showControls;
            if (pager) {
                pager.hidden = !showControls;
                pager.setAttribute('aria-hidden', showControls ? 'false' : 'true');
            }
        }

        function updatePagerDots() {
            if (!pager || pager.hidden) return;
            pager.querySelectorAll('.admin-nav__pager-dot').forEach((dot, index) => {
                dot.classList.toggle('is-active', index === page);
                dot.setAttribute('aria-current', index === page ? 'true' : 'false');
            });
        }

        function buildPager() {
            if (!pager) return;
            pager.innerHTML = '';
            pages = calcPages();
            if (pages <= 1) return;
            for (let index = 0; index < pages; index += 1) {
                const dot = document.createElement('button');
                dot.type = 'button';
                dot.className = 'admin-nav__pager-dot';
                dot.setAttribute('aria-label', `Grupo ${index + 1} de ${pages}`);
                dot.addEventListener('click', () => goToPage(index));
                pager.appendChild(dot);
            }
        }

        function goToPage(index, { animate = true } = {}) {
            pages = calcPages();
            page = Math.max(0, Math.min(index, pages - 1));

            if (!MOBILE_MQ.matches) {
                track.style.transform = '';
                prevBtn.disabled = true;
                nextBtn.disabled = true;
                setControlsVisibility();
                return;
            }

            if (!animate) track.style.transition = 'none';
            syncLayout();
            track.style.transform = `translate3d(-${page * viewport.clientWidth}px, 0, 0)`;
            if (!animate) {
                track.offsetHeight;
                track.style.transition = '';
            }

            prevBtn.disabled = page <= 0;
            nextBtn.disabled = page >= pages - 1;
            setControlsVisibility();
            updatePagerDots();
        }

        function refresh({ animate = false } = {}) {
            syncLayout();
            buildPager();
            if (MOBILE_MQ.matches) {
                goToPage(pageForActiveItem(), { animate });
            } else {
                goToPage(0, { animate: false });
            }
        }

        prevBtn.addEventListener('click', () => goToPage(page - 1));
        nextBtn.addEventListener('click', () => goToPage(page + 1));

        viewport.addEventListener('touchstart', event => {
            touchStartX = event.changedTouches[0].screenX;
        }, { passive: true });

        viewport.addEventListener('touchend', event => {
            if (!MOBILE_MQ.matches) return;
            const deltaX = event.changedTouches[0].screenX - touchStartX;
            if (Math.abs(deltaX) < SWIPE_THRESHOLD_PX) return;
            if (deltaX < 0) goToPage(page + 1);
            else goToPage(page - 1);
        }, { passive: true });

        MOBILE_MQ.addEventListener('change', () => refresh({ animate: false }));
        window.addEventListener('resize', () => {
            if (MOBILE_MQ.matches) goToPage(page, { animate: false });
        });

        refresh({ animate: false });
    }

    document.querySelectorAll('[data-admin-nav-slide]').forEach(initNavSlide);
})();
