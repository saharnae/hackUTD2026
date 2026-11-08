// Dotted globe with pulsing trading hubs and arcs between them. Spins on its own and turns with scroll.
// Activity is SIMULATED and labelled that way wherever it is shown.
import { useEffect, useRef } from "react";
import { LAND } from "./land";

export const HUBS: { name: string; lat: number; lon: number }[] = [
  { name: "Dallas", lat: 32.78, lon: -96.8 }, { name: "New York", lat: 40.71, lon: -74.0 },
  { name: "Chicago", lat: 41.88, lon: -87.63 }, { name: "Toronto", lat: 43.65, lon: -79.38 },
  { name: "São Paulo", lat: -23.55, lon: -46.63 }, { name: "London", lat: 51.5, lon: -0.12 },
  { name: "Frankfurt", lat: 50.11, lon: 8.68 }, { name: "Zurich", lat: 47.37, lon: 8.54 },
  { name: "Dubai", lat: 25.2, lon: 55.27 }, { name: "Mumbai", lat: 19.07, lon: 72.88 },
  { name: "Singapore", lat: 1.35, lon: 103.8 }, { name: "Hong Kong", lat: 22.3, lon: 114.17 },
  { name: "Tokyo", lat: 35.68, lon: 139.69 }, { name: "Sydney", lat: -33.87, lon: 151.2 },
];

const R = Math.PI / 180;
type V = [number, number, number];
const toV = (lat: number, lon: number): V => [Math.cos(lat * R) * Math.sin(lon * R), Math.sin(lat * R), Math.cos(lat * R) * Math.cos(lon * R)];

function slerp(a: V, b: V, t: number): V {
  const d = Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
  if (d < 1e-6) return a;
  const s = Math.sin(d), k1 = Math.sin((1 - t) * d) / s, k2 = Math.sin(t * d) / s;
  return [a[0] * k1 + b[0] * k2, a[1] * k1 + b[1] * k2, a[2] * k1 + b[2] * k2];
}

export default function Globe({ onArc }: { onArc?: (from: string, to: string) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const cb = useRef(onArc);
  cb.current = onArc;

  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const land: V[] = [];
    for (let i = 0; i < LAND.length; i += 2) land.push(toV(LAND[i + 1], LAND[i]));
    const hubs = HUBS.map((h) => ({ ...h, v: toV(h.lat, h.lon), ph: Math.random() * 6.28 }));
    let arcs: { a: number; b: number; t0: number }[] = [];
    let size = 0, dpr = 1, raf = 0, lastArc = 0, visible = true;

    const fit = () => {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      size = cv.parentElement!.clientWidth;
      cv.width = size * dpr; cv.height = size * dpr; cv.style.width = `${size}px`; cv.style.height = `${size}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; });
    io.observe(cv);

    const draw = (t: number) => {
      const r = size * 0.44, cx = size / 2, cy = size / 2;
      const rot = 96 - (still ? 0 : t * 0.004) - window.scrollY * 0.03;    // degrees; starts centred on Dallas, turns east with scroll
      const tilt = -18 * R;
      const cr = Math.cos(rot * R), sr = Math.sin(rot * R), ct = Math.cos(tilt), st = Math.sin(tilt);
      const proj = (v: V) => {
        const x = v[0] * cr + v[2] * sr, z0 = -v[0] * sr + v[2] * cr;
        const y = v[1] * ct - z0 * st, z = v[1] * st + z0 * ct;
        return { x: cx + x * r, y: cy - y * r, z };
      };
      ctx.clearRect(0, 0, size, size);

      // atmosphere + rim
      const g = ctx.createRadialGradient(cx, cy, r * 0.75, cx, cy, r * 1.25);
      g.addColorStop(0, "rgba(0,200,5,0)"); g.addColorStop(0.45, "rgba(0,200,5,.07)"); g.addColorStop(1, "rgba(0,200,5,0)");
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(cx, cy, r * 1.25, 0, 7); ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,.015)"; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.arc(cx, cy, r, 0, 7); ctx.stroke();

      // land dots
      for (const v of land) {
        const p = proj(v);
        if (p.z < 0) continue;
        ctx.fillStyle = `rgba(241,239,233,${0.18 + p.z * 0.6})`;
        ctx.beginPath(); ctx.arc(p.x, p.y, 0.7 + p.z * 0.9, 0, 6.3); ctx.fill();
      }

      // arcs
      if (!still && t - lastArc > 650) {
        lastArc = t;
        const a = Math.floor(Math.random() * hubs.length);
        let b = Math.floor(Math.random() * hubs.length); if (b === a) b = (b + 3) % hubs.length;
        arcs.push({ a, b, t0: t });
        cb.current?.(hubs[a].name, hubs[b].name);
      }
      arcs = arcs.filter((x) => t - x.t0 < 2600);
      for (const arc of arcs) {
        const k = (t - arc.t0) / 1600;                 // head progress
        const head = Math.min(1, k), tail = Math.max(0, k - 0.55);
        const A = hubs[arc.a].v, B = hubs[arc.b].v;
        ctx.beginPath();
        let started = false;
        for (let s = tail; s <= head + 1e-6; s += 0.03) {
          const v = slerp(A, B, s), lift = 1 + 0.22 * Math.sin(Math.PI * s);
          const p = proj([v[0] * lift, v[1] * lift, v[2] * lift]);
          if (p.z < -0.15) { started = false; continue; }
          if (!started) { ctx.moveTo(p.x, p.y); started = true; } else ctx.lineTo(p.x, p.y);
        }
        const fade = Math.max(0, 1 - Math.max(0, k - 1) / 0.6);
        ctx.strokeStyle = `rgba(0,200,5,${0.75 * fade})`; ctx.lineWidth = 1.5; ctx.stroke();
      }

      // hubs
      for (const h of hubs) {
        const p = proj(h.v);
        if (p.z < 0.05) continue;
        const pulse = (t / 1400 + h.ph) % 1;
        ctx.strokeStyle = `rgba(0,200,5,${(1 - pulse) * 0.6 * p.z})`;
        ctx.lineWidth = 1.2; ctx.beginPath(); ctx.arc(p.x, p.y, 3 + pulse * 13, 0, 7); ctx.stroke();
        ctx.fillStyle = h.name === "Dallas" ? "#ffcc00" : "#00c805";
        ctx.beginPath(); ctx.arc(p.x, p.y, h.name === "Dallas" ? 3.4 : 2.6, 0, 7); ctx.fill();
        if (h.name === "Dallas" && p.z > 0.3) {
          ctx.fillStyle = "rgba(241,239,233,.85)"; ctx.font = "500 11px 'JetBrains Mono', monospace";
          ctx.fillText("DALLAS · UTD", p.x + 8, p.y - 6);
        }
      }
    };
    const loop = (t: number) => { if (visible) draw(t); raf = requestAnimationFrame(loop); };
    fit();
    window.addEventListener("resize", fit);
    if (still) { draw(0); window.addEventListener("scroll", () => draw(0), { passive: true }); }
    else raf = requestAnimationFrame(loop);
    return () => { cancelAnimationFrame(raf); io.disconnect(); window.removeEventListener("resize", fit); };
  }, []);

  return <canvas ref={ref} className="globe" aria-label="Globe of simulated trading activity" />;
}
