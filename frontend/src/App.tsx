// Two views: the landing site (#/) and the live trading desk (#/desk).
import { useEffect, useState } from "react";
import Site from "./components/Site";
import Desk from "./Desk";

const read = () => (window.location.hash.startsWith("#/desk") ? "desk" : "site");

export default function App() {
  const [view, setView] = useState<"site" | "desk">(read);

  useEffect(() => {
    const on = () => { setView(read()); window.scrollTo({ top: 0 }); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const go = (v: "site" | "desk") => { window.location.hash = v === "desk" ? "#/desk" : "#/"; };

  return (
    <div key={view} className="view-fade">
      {view === "desk" ? <Desk onHome={() => go("site")} /> : <Site onLaunch={() => go("desk")} />}
    </div>
  );
}
