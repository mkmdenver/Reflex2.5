// src/lib/event/EventStream.ts
// Version: Reflex 2.4 — BrokerView ORDER_METRICS handler
// Date: 2025-12-19

import { useEffect } from "react";
import { useStateContext } from "@/context/StateContext";

export function useEventStream() {
  const { dispatch } = useStateContext();

  useEffect(() => {
    const source = new EventSource("/v1/events");

    source.onmessage = (event) => {
      const parsed = JSON.parse(event.data);
      switch (event.type || parsed.type) {
        case "ORDER_REQUEST":
        case "ORDER_PLACED":
        case "ORDER_REJECTED":
        case "BROKER_UPDATE":
          dispatch({ type: "ORDER_EVENT", payload: parsed });
          break;
        case "ORDER_METRICS":
          dispatch({ type: "ORDER_METRICS", payload: parsed });
          break;
        default:
          break;
      }
    };

    return () => {
      source.close();
    };
  }, [dispatch]);
}
