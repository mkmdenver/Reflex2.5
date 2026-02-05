// src/components/log/LiveLog.tsx
// Version: Reflex 2.4 — LiveLog panel
// Date: 2025-12-19

import React, { useEffect, useRef } from "react";
import { useStateContext } from "@/context/StateContext";

export default function LiveLog() {
  const { state } = useStateContext();
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [state.logEvents]);

  return (
    <div className="p-2 h-screen bg-black text-green-400 font-mono overflow-y-scroll text-xs" ref={ref}>
      {state.logEvents.map((e: any, i: number) => (
        <pre key={i}>{JSON.stringify(e, null, 2)}</pre>
      ))}
    </div>
  );
}
