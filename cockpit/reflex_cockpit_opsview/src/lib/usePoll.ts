import { useEffect, useRef, useState } from 'react';

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: any[] = [],
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    let timer: any = null;
    const tick = async () => {
      try {
        setLoading(true);
        const res = await fetcher();
        if (!alive.current) return;
        setData(res);
        setError(null);
      } catch (e: any) {
        if (!alive.current) return;
        setError(String(e?.message || e));
      } finally {
        if (!alive.current) return;
        setLoading(false);
      }
    };

    tick();
    timer = setInterval(tick, Math.max(500, intervalMs));
    return () => {
      if (timer) clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading };
}
