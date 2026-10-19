import { useEffect, useMemo, useRef, useState } from "react";

type CommonProps = {
  options: string[];
  allLabel: string;
  width?: number;
};

type SingleSelectProps = CommonProps & {
  multi?: false;
  value: string; // "all" or one of `options`
  onChange: (v: string) => void;
};

type MultiSelectProps = CommonProps & {
  multi: true;
  value: string[]; // empty array means "all" (no filter applied)
  onChange: (v: string[]) => void;
};

type Props = SingleSelectProps | MultiSelectProps;

// A minimal searchable dropdown for filters with many possible values
// (symbols, outcomes, types) where a plain <select> makes finding one
// tedious. Not a full combobox -- no keyboard nav -- just type-to-filter +
// click. `multi` switches between the original single-pick behavior
// (select closes the menu, replaces the value) and a checkbox multi-pick
// (menu stays open across picks so several can be toggled in a row, value
// is the accumulated array).
export default function SearchableSelect(props: Props) {
  const { options, allLabel, width } = props;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onDocMouseDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setQuery("");
      }
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const all = [allLabel, ...options];
    if (!q) return all;
    return all.filter((o) => o.toLowerCase().includes(q));
  }, [options, query, allLabel]);

  function selectSingle(opt: string) {
    if (props.multi) return;
    props.onChange(opt === allLabel ? "all" : opt);
    setQuery("");
    setOpen(false);
  }

  function toggleMulti(opt: string) {
    if (!props.multi) return;
    if (opt === allLabel) {
      props.onChange([]); // clears back to "no filter" -- also shows every option as checked, see below
      setQuery("");
      setOpen(false);
      return;
    }
    // An empty value[] means "no filter" AND displays every option as
    // checked (see the checked= prop below) -- so unchecking one option
    // while value is still [] has to start from the FULL option list, not
    // from an empty set (toggling membership in [] would just add opt back
    // in, which would look like re-checking the very box just clicked).
    const current = props.value.length === 0 ? options : props.value;
    const set = new Set(current);
    if (set.has(opt)) set.delete(opt);
    else set.add(opt);
    // Collapse back to the canonical [] once every option ends up checked
    // again -- keeps "All types" and the empty-means-unfiltered convention
    // in sync instead of drifting into a full-length array that means the
    // same thing but no longer satisfies `.length === 0` checks elsewhere.
    props.onChange(set.size === options.length ? [] : Array.from(set));
    // Deliberately stays open (unlike selectSingle) -- picking several
    // values in a row shouldn't require reopening the menu each time.
  }

  const displayValue = open
    ? query
    : !props.multi
      ? props.value === "all"
        ? allLabel
        : props.value
      : props.value.length === 0
        ? allLabel
        : props.value.length === 1
          ? props.value[0]
          : `${props.value.length} selected`;

  return (
    <div className="searchable-select" ref={rootRef} style={width ? { width } : undefined}>
      <input
        value={displayValue}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          setOpen(true);
          setQuery("");
        }}
        placeholder={allLabel}
      />
      {open && (
        <div className="searchable-select-menu">
          {filtered.length === 0 ? (
            <div className="searchable-select-empty">No matches</div>
          ) : (
            filtered.map((o) => (
              <div
                key={o}
                className="searchable-select-option"
                onClick={() => (props.multi ? toggleMulti(o) : selectSingle(o))}
              >
                {props.multi && (
                  <input
                    type="checkbox"
                    checked={
                      o === allLabel
                        ? props.value.length === 0
                        : props.value.length === 0 || props.value.includes(o)
                    }
                    readOnly
                  />
                )}
                {o}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
