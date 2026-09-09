/**
 * Business AI — tiny shared motion helpers (scroll reveal + count-up).
 * No library: a single IntersectionObserver, used by the marketing page
 * and the dashboard. Kept under ~30 lines on purpose.
 */
(function (window) {
  'use strict';

  function initReveal(selector) {
    selector = selector || '.bm-reveal, .bm-reveal-stagger';
    const els = document.querySelectorAll(selector);
    if (!els.length) return;
    if (!('IntersectionObserver' in window)) {
      els.forEach(el => el.classList.add('in'));
      return;
    }
    // threshold 0 + a generous rootMargin (not a tight -40px bottom
    // margin) is deliberate: found in testing that a fast or
    // instantaneous scroll (an in-page anchor jump, or a user mashing
    // Page Down) can carry a section through the viewport in fewer
    // rendered frames than a narrow detection zone reliably samples,
    // leaving it stuck invisible forever since it's only ever observed
    // once. A wide margin makes an element "intersecting" long before
    // and after it's actually on-screen, so a fast pass-through still
    // lands inside the zone.
    const io = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('in');
          io.unobserve(entry.target);
        }
      });
    }, { threshold: 0, rootMargin: '400px 0px 400px 0px' });
    els.forEach(el => io.observe(el));
  }

  function countUp(el, target, duration = 900) {
    const start = performance.now();
    function tick(now) {
      const p = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(eased * target).toLocaleString();
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  // Self-driven scroll animation — NOT the browser's native
  // scrollIntoView({behavior:'smooth'})/CSS scroll-behavior:smooth.
  // Tested and found both native paths can silently fail to animate at
  // all in some browser/automation contexts (the scroll position simply
  // never moves), which is a correctness bug, not a cosmetic one, for
  // an in-page nav link. Driving the scroll frame-by-frame with rAF and
  // plain instant window.scrollTo() sidesteps the native implementation
  // entirely, so it can't inherit that failure mode.
  function smoothScrollTo(targetEl, duration = 500) {
    const startY = window.scrollY;
    const endY = startY + targetEl.getBoundingClientRect().top;
    const delta = endY - startY;
    const start = performance.now();
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      window.scrollTo(0, endY);
      return;
    }
    function tick(now) {
      const p = Math.min(1, (now - start) / duration);
      const eased = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
      window.scrollTo(0, startY + delta * eased);
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  window.BizMotion = { initReveal, countUp, smoothScrollTo };
})(window);
