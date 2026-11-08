// A full-bleed highway that winds across the page, with the ρ shield driving it.
import { useEffect, useState } from "react";

const reduced = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export default function WindingRoad({ id, d, height = 200, car = true, dur = 18 }: {
  id: string; d: string; height?: number; car?: boolean; dur?: number;
}) {
  const [still, setStill] = useState(reduced);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setStill(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  return (
    <svg className="winding" viewBox={`0 0 1440 ${height}`} preserveAspectRatio="none" aria-hidden>
      <defs>
        <linearGradient id={`${id}-fade`} x1="0" x2="1" y1="0" y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset=".08" stopColor="#fff" stopOpacity="1" />
          <stop offset=".92" stopColor="#fff" stopOpacity="1" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <mask id={`${id}-mask`} maskUnits="userSpaceOnUse" x="0" y="0" width="1440" height={height}><rect width="1440" height={height} fill={`url(#${id}-fade)`} /></mask>
      </defs>
      <g mask={`url(#${id}-mask)`}>
        <path d={d} fill="none" stroke="rgba(255,255,255,.10)" strokeWidth="58" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        <path d={d} fill="none" stroke="#15191d" strokeWidth="52" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
        <path d={d} fill="none" className="centerline" stroke="#e8b800" strokeWidth="2.5" strokeDasharray="16 14" vectorEffect="non-scaling-stroke" />
        <path id={id} d={d} fill="none" />
        {car && !still && (
          <g>
            <animateMotion dur={`${dur}s`} repeatCount="indefinite" calcMode="spline" keyTimes="0;1" keySplines=".45 0 .55 1">
              <mpath href={`#${id}`} />
            </animateMotion>
            <g transform="translate(-13,-14) scale(.26)">
              <path d="M50 4 C66 12 82 12 96 6 C100 40 92 78 50 104 C8 78 0 40 4 6 C18 12 34 12 50 4Z" fill="#1f4fbf" stroke="#fff" strokeWidth="6" />
              <path d="M8 8 C22 14 36 14 50 7 C64 14 78 14 92 8 L91 30 L9 30Z" fill="#c8102e" />
              <text x="50" y="84" textAnchor="middle" fontFamily="Overpass, Inter, sans-serif" fontWeight="800" fontSize="58" fill="#fff">ρ</text>
            </g>
          </g>
        )}
      </g>
    </svg>
  );
}
