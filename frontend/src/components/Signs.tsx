// Highway-sign building blocks for the "Rho to Riches" theme.
import type { ReactNode } from "react";

/** Interstate-style shield with ρ: the logo and the car. */
export function RhoShield({ size = 40 }: { size?: number }) {
  return (
    <svg width={size} height={size * 1.08} viewBox="0 0 100 108" aria-label="Rho to Riches" className="shield">
      <path d="M50 4 C66 12 82 12 96 6 C100 40 92 78 50 104 C8 78 0 40 4 6 C18 12 34 12 50 4Z"
        fill="#1f4fbf" stroke="#fff" strokeWidth="5" />
      <path d="M8 8 C22 14 36 14 50 7 C64 14 78 14 92 8 L91 30 L9 30Z" fill="#c8102e" />
      <text x="50" y="84" textAnchor="middle" fontFamily="Overpass, Inter, sans-serif"
        fontWeight="800" fontSize="58" fill="#fff">ρ</text>
    </svg>
  );
}

export type SignKind = "exit" | "warn" | "stop" | "limit" | "route" | "info";

/** Small road sign used as a bullet before headers. */
export function SignBullet({ kind = "exit", text = "" }: { kind?: SignKind; text?: string }) {
  const s = 28;
  switch (kind) {
    case "route":
      return <RhoShield size={24} />;
    case "warn":
      return (
        <svg width={s} height={s} viewBox="0 0 40 40" className="sb" aria-hidden>
          <rect x="7" y="7" width="26" height="26" rx="3" transform="rotate(45 20 20)" fill="#ffcc00" stroke="#111" strokeWidth="1.5" />
          <text x="20" y="25.5" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="15" fill="#111">{text || "!"}</text>
        </svg>
      );
    case "stop":
      return (
        <svg width={s} height={s} viewBox="0 0 40 40" className="sb" aria-hidden>
          <polygon points="12,2 28,2 38,12 38,28 28,38 12,38 2,28 2,12" fill="#c8102e" stroke="#fff" strokeWidth="2" />
          <text x="20" y="24.5" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="10" fill="#fff">{text || "STOP"}</text>
        </svg>
      );
    case "limit":
      return (
        <svg width={s * 0.8} height={s} viewBox="0 0 32 40" className="sb" aria-hidden>
          <rect x="1" y="1" width="30" height="38" rx="4" fill="#fff" stroke="#111" strokeWidth="2" />
          <text x="16" y="13" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="6.5" fill="#111">LIMIT</text>
          <text x="16" y="32" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="16" fill="#111">{text || "20"}</text>
        </svg>
      );
    case "info":
      return (
        <svg width={s} height={s} viewBox="0 0 40 40" className="sb" aria-hidden>
          <rect x="2" y="2" width="36" height="36" rx="6" fill="#1f4fbf" stroke="#fff" strokeWidth="2" />
          <text x="20" y="27" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="18" fill="#fff">{text || "i"}</text>
        </svg>
      );
    default:
      return (
        <svg width={s * 1.15} height={s} viewBox="0 0 46 40" className="sb" aria-hidden>
          <rect x="1" y="5" width="44" height="34" rx="5" fill="#0b6b3a" stroke="#fff" strokeWidth="2" />
          <text x="23" y="28" textAnchor="middle" fontFamily="Overpass, sans-serif" fontWeight="800" fontSize="15" fill="#fff">{text}</text>
        </svg>
      );
  }
}

/** Section heading: road-sign bullet + kicker + title. */
export function SectionHead({ kind, text, kicker, title, lede }: {
  kind: SignKind; text?: string; kicker: string; title: ReactNode; lede?: ReactNode;
}) {
  return (
    <header className="sec-head reveal">
      <div className="kicker"><SignBullet kind={kind} text={text} /><span>{kicker}</span></div>
      <h2>{title}</h2>
      {lede && <p className="lede">{lede}</p>}
    </header>
  );
}

/** Green guide sign used as a section header inside the desk. */
export function SignHeader({ title, sub, exit, tone = "green" }: {
  title: string; sub?: ReactNode; exit?: string; tone?: "green" | "blue" | "brown";
}) {
  return (
    <div className={`sign-header ${tone}`}>
      {exit && <span className="exit-tab">{exit}</span>}
      <span className="sign-title">{title}</span>
      {sub && <span className="sign-sub">{sub}</span>}
    </div>
  );
}

/** Yellow diamond warning sign. */
export function WarnSign({ children }: { children: ReactNode }) {
  return <span className="warn-diamond"><span>{children}</span></span>;
}
