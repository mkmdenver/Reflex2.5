// =============================================================================
// static/ticks_modal.js
// Version: 2025.10.22-rc1
//
// CHANGELOG
// - 2025-10-22: rc1
//   • Minimal modal controller + append(). Auto-scroll.
// =============================================================================
const Modal = (function(){
  const el = () => document.getElementById("modal");
  const title = () => document.getElementById("modal-title");
  const body = () => document.getElementById("modal-body");
  function open(t, initial){
    title().textContent = t || "Console";
    body().textContent = initial || "";
    el().classList.remove("hide");
  }
  function append(text){
    const b = body();
    b.textContent += text || "";
    b.scrollTop = b.scrollHeight;
  }
  function close(){
    el().classList.add("hide");
  }
  return { open, append, close };
})();
