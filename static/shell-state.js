(() => {
  const storageKey = "manzara.ui.rail.expanded";
  const desktopDefault = typeof matchMedia === "function"
    ? matchMedia("(min-width: 901px)").matches
    : true;
  let expanded = desktopDefault;

  try {
    const stored = localStorage.getItem(storageKey);
    if (stored === "1") expanded = true;
    if (stored === "0") expanded = false;
  } catch (_error) {
    // The viewport default is sufficient when local storage is unavailable.
  }

  document.documentElement.classList.toggle("rail-expanded", expanded);
  window.ManzaraShellState = Object.freeze({
    storageKey,
    initialExpanded: expanded,
  });
})();
