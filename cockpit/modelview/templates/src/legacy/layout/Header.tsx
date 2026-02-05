// templates/src/layout/Header.tsx

import React from "react";

export default function Header() {
  const openLog = () => {
    window.open("/log.html", "logWindow", "width=600,height=800");
  };

  return (
    <header className="flex justify-between items-center p-3 bg-gray-800 text-white">
      <h1 className="text-lg font-bold">Reflex Broker Cockpit</h1>
      <div className="space-x-2">
        <button
          onClick={openLog}
          className="bg-green-600 px-3 py-1 rounded hover:bg-green-700"
        >
          Live Log
        </button>
      </div>
      <button
        onClick={openLog}
        className="bg-red-600 px-5 py-2 rounded text-white text-xl"
      >
        LIVE LOG BUTTON!
      </button>

    </header>
  );
}
