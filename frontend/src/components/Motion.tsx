// Small motion helpers: word-by-word headings and count-up numbers.
import { useEffect, useRef, useState } from "react";

/** Heading whose words rise in one after another as it scrolls into view. */
export function Split({ text, as = "h2", className = "" }: { text: string; as?: "h1" | "h2" | "h3"; className?: string }) {
  const Tag = as;
  return (
    <Tag className={`split ${className}`} aria-label={text}>
      {text.split(" ").map((w, i) => (
        <span key={i} className="w-mask" aria-hidden><span className="w" style={{ ["--i" as any]: i }}>{w}</span></span>
      ))}
    </Tag>
  );
}

/** Number that counts up the first time it scrolls into view. */
export function CountUp({ to, decimals = 0, prefix = "", suffix = "", live = false }: {
  to: number; decimals?: number; prefix?: string; suffix?: string; live?: boolean;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const [v, setV] = useState(0);
  const done = useRef(false);
  useEffect(() => {
    const el = ref.current!;
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting || done.current) return;
      done.current = true;
      const t0 = performance.now(), from = 0;
      const step = (t: number) => {
        const k = Math.min(1, (t - t0) / 1400), ease = 1 - Math.pow(1 - k, 4);
        setV(from + (to - from) * ease);
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    }, { threshold: 0.4 });
    io.observe(el);
    return () => io.disconnect();
  }, [to]);
  useEffect(() => { if (live && done.current) setV(to); }, [to, live]);
  return <span ref={ref} className="count">{prefix}{v.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals })}{suffix}</span>;
}
