// src/context/StateContext.tsx
// Version: Reflex 2.4 — ORDER_METRICS support
// Date: 2025-12-19

import React, { createContext, useReducer, useContext } from "react";

interface AppState {
  orders: Record<string, any>;
  metricsByOrderId: Record<string, any>;
  logEvents: any[];
}

const initialState: AppState = {
  orders: {},
  metricsByOrderId: {},
  logEvents: [],
};

function reducer(state: AppState, action: any): AppState {
  switch (action.type) {
    case "ORDER_EVENT": {
      const o = action.payload;
      return {
        ...state,
        orders: { ...state.orders, [o.id]: o },
        logEvents: [...state.logEvents, o],
      };
    }
    case "ORDER_METRICS": {
      const { order_id, metrics, times } = action.payload;
      return {
        ...state,
        metricsByOrderId: {
          ...state.metricsByOrderId,
          [order_id]: { metrics, times },
        },
        logEvents: [...state.logEvents, action.payload],
      };
    }
    default:
      return state;
  }
}

const StateContext = createContext<{
  state: AppState;
  dispatch: React.Dispatch<any>;
}>({
  state: initialState,
  dispatch: () => null,
});

export const StateProvider = ({ children }: any) => {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <StateContext.Provider value={{ state, dispatch }}>
      {children}
    </StateContext.Provider>
  );
};

export const useStateContext = () => useContext(StateContext);
