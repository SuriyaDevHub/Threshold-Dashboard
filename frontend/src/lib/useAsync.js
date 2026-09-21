import { useState, useEffect, useCallback } from "react";

// Minimal data-fetching hook. fn must be stable (wrap in useCallback) or pass deps.
export function useAsync(fn, deps = []) {
  const [state, setState] = useState({ loading: true, data: null, error: null });

  const run = useCallback(() => {
    let alive = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    fn()
      .then((data) => alive && setState({ loading: false, data, error: null }))
      .catch((error) => alive && setState({ loading: false, data: null, error }));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => run(), [run]);
  return { ...state, reload: run };
}
