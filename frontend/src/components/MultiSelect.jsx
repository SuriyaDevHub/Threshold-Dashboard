import { useState, useRef, useEffect } from "react";
import { ChevronDown } from "lucide-react";

// Compact multi-select: button shows a summary, opens a checkbox list.
export default function MultiSelect({ label, options, selected, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    function onDoc(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  const toggle = (opt) =>
    onChange(
      selected.includes(opt) ? selected.filter((x) => x !== opt) : [...selected, opt]
    );

  const summary =
    selected.length === 0
      ? "None"
      : selected.length === options.length
      ? "All"
      : selected.length <= 2
      ? selected.join(", ")
      : `${selected.length} selected`;

  return (
    <div className="ms" ref={ref}>
      <span className="ms-label">{label}</span>
      <button type="button" className="ms-trigger" onClick={() => setOpen((o) => !o)}>
        <span className="ms-summary">{summary}</span>
        <ChevronDown size={15} />
      </button>
      {open && (
        <div className="ms-pop">
          <div className="ms-actions">
            <button onClick={() => onChange([...options])}>All</button>
            <button onClick={() => onChange([])}>Clear</button>
          </div>
          {options.map((opt) => (
            <label key={opt} className="ms-opt">
              <input
                type="checkbox"
                checked={selected.includes(opt)}
                onChange={() => toggle(opt)}
              />
              <span className="mono">{opt}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}
