// "Night drive" background: tiny head/tail-light streaks (green, red, warm white) drifting
// at different depths. Scrolling moves near streaks faster than far ones (parallax).
import { useEffect, useRef } from "react";

type S = { x: number; y: number; z: number; len: number; vx: number; c: string; a: number; tw: number };
const COLORS = ["0,200,5", "255,80,0", "241,239,233", "255,204,0", "0,200,5", "255,80,0"];

export default function NightField() {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let w = 0, h = 0, dpr = 1, raf = 0, stars: S[] = [];

    const seed = () => {
      const n = Math.round((w * h) / 9000);
      stars = Array.from({ length: n }, () => {
        const z = 0.25 + Math.random() * 0.75;
        const c = COLORS[Math.floor(Math.random() * COLORS.length)];
        const isLight = c === "241,239,233" || c === "255,204,0";
        return { x: Math.random() * w, y: Math.random() * h * 2, z, len: (2 + Math.random() * 9) * z,
          vx: (isLight ? 1 : -1) * (0.04 + Math.random() * 0.22) * z, c, a: 0.25 + Math.random() * 0.55 * z, tw: Math.random() * 6.28 };
      });
    };
    const size = () => {
      dpr = Math.min(1.5, window.devicePixelRatio || 1);
      w = window.innerWidth; h = window.innerHeight;
      cv.width = w * dpr; cv.height = h * dpr; cv.style.width = `${w}px`; cv.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      seed();
    };
    const draw = (t: number) => {
      ctx.clearRect(0, 0, w, h);
      const sy = window.scrollY;
      ctx.lineCap = "round";
      for (const s of stars) {
        if (!still) s.x += s.vx;
        if (s.x < -20) s.x = w + 20; if (s.x > w + 20) s.x = -20;
        const y = ((s.y - sy * s.z * 0.35) % (h * 2) + h * 2) % (h * 2) - h * 0.5;
        if (y < -10 || y > h + 10) continue;
        const a = s.a * (0.75 + 0.25 * Math.sin(t / 900 + s.tw));
        ctx.strokeStyle = `rgba(${s.c},${a})`;
        ctx.lineWidth = s.z > 0.75 ? 1.6 : 1.1;
        ctx.beginPath();
        ctx.moveTo(s.x, y);
        ctx.lineTo(s.x - Math.sign(s.vx) * s.len, y);
        ctx.stroke();
      }
    };
    const loop = (t: number) => { draw(t); raf = requestAnimationFrame(loop); };
    size();
    window.addEventListener("resize", size);
    if (still) { draw(0); window.addEventListener("scroll", () => draw(0), { passive: true }); }
    else raf = requestAnimationFrame(loop);
    return () => { cancelAnimationFrame(raf); window.removeEventListener("resize", size); };
  }, []);

  return <canvas ref={ref} className="nightfield" aria-hidden />;
}
