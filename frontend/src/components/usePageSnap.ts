// Full-page snapping: one wheel flick / swipe / key press = one screen.
// Snap points: the top of every [data-snap] section, extra points inside any section taller than
// the screen, and one point per stop inside the pinned supervisor drive ([data-stops]).
import { useEffect } from "react";

// quick start, soft landing (easeOutQuart) — feels responsive without overshooting
const ease = (t: number) => 1 - Math.pow(1 - t, 4);

export default function usePageSnap() {
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let points: number[] = [];
    let animating = false, raf = 0, touchY = 0, lastWheel = 0;

    const build = () => {
      const vh = window.innerHeight;
      const max = document.documentElement.scrollHeight - vh;
      const pts = new Set<number>([0]);
      document.querySelectorAll<HTMLElement>("[data-snap]").forEach((el) => {
        const top = Math.round(el.getBoundingClientRect().top + window.scrollY);
        const h = el.offsetHeight;
        const stops = el.dataset.stops;
        if (stops) {
          const travel = h - vh;
          pts.add(top);
          stops.split(",").map(Number).forEach((f) => pts.add(Math.round(top + f * travel)));
        } else {
          pts.add(top);
          for (let y = top + vh; y < top + h - vh * 0.15; y += vh) pts.add(Math.round(y));   // tall section: page through it
        }
      });
      pts.add(max);
      points = [...pts].map((y) => Math.max(0, Math.min(max, y))).sort((a, b) => a - b)
        .filter((y, i, a) => i === 0 || y - a[i - 1] > 40);
    };

    const go = (target: number) => {
      cancelAnimationFrame(raf);
      const start = window.scrollY, dist = target - start;
      if (Math.abs(dist) < 2) return;
      if (reduced) { window.scrollTo(0, target); return; }
      const dur = Math.min(820, 560 + Math.abs(dist) * 0.08);
      const t0 = performance.now();
      animating = true;
      const step = (t: number) => {
        const k = Math.min(1, (t - t0) / dur);
        window.scrollTo(0, start + dist * ease(k));
        if (k < 1) raf = requestAnimationFrame(step);
        else animating = false;
      };
      raf = requestAnimationFrame(step);
    };

    const move = (dir: 1 | -1) => {
      if (animating) return;
      build();
      const y = window.scrollY;
      const next = dir > 0 ? points.find((p) => p > y + 8) : [...points].reverse().find((p) => p < y - 8);
      if (next !== undefined) go(next);
    };

    // A new gesture starts only after a short pause in wheel events, so a trackpad's
    // momentum tail (a steady stream of small events) never turns one swipe into two pages.
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey) return;                        // pinch-zoom
      e.preventDefault();
      const now = performance.now(), gap = now - lastWheel;
      lastWheel = now;
      if (Math.abs(e.deltaY) < 3 || gap < 140) return;
      move(e.deltaY > 0 ? 1 : -1);
    };
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (["ArrowDown", "PageDown", " "].includes(e.key) && !e.shiftKey) { e.preventDefault(); move(1); }
      else if (["ArrowUp", "PageUp"].includes(e.key) || (e.key === " " && e.shiftKey)) { e.preventDefault(); move(-1); }
      else if (e.key === "Home") { e.preventDefault(); build(); go(points[0]); }
      else if (e.key === "End") { e.preventDefault(); build(); go(points[points.length - 1]); }
    };
    const onTouchStart = (e: TouchEvent) => { touchY = e.touches[0].clientY; };
    const onTouchMove = (e: TouchEvent) => { e.preventDefault(); };
    const onTouchEnd = (e: TouchEvent) => {
      const dy = touchY - e.changedTouches[0].clientY;
      if (Math.abs(dy) > 40) move(dy > 0 ? 1 : -1);
    };
    // after a tab click or resize, settle on the nearest page
    let settle = 0;
    const onScrollEnd = () => {
      if (animating) return;
      clearTimeout(settle);
      settle = window.setTimeout(() => {
        if (animating) return;
        build();
        const y = window.scrollY;
        const near = points.reduce((a, b) => (Math.abs(b - y) < Math.abs(a - y) ? b : a), points[0]);
        if (Math.abs(near - y) > 3) go(near);
      }, 180);
    };

    build();
    window.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("keydown", onKey);
    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, { passive: false });
    window.addEventListener("touchend", onTouchEnd);
    window.addEventListener("resize", build);
    window.addEventListener("scroll", onScrollEnd, { passive: true });
    (window as any).__snapTo = (el: HTMLElement) => { build(); go(Math.round(el.getBoundingClientRect().top + window.scrollY)); };
    return () => {
      cancelAnimationFrame(raf); clearTimeout(settle);
      window.removeEventListener("wheel", onWheel);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("resize", build);
      window.removeEventListener("scroll", onScrollEnd);
      delete (window as any).__snapTo;
    };
  }, []);
}
