(() => {
  const button = document.querySelector("[data-copy-setup]");
  const details = document.getElementById("lite-setup-details");
  const status = document.querySelector("[data-copy-status]");
  if (!button || !details || !status) return;
  button.addEventListener("click", async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(details.value);
      status.textContent = "Copied. Paste these details into Lite's Master connection page.";
    } catch {
      details.focus();
      details.select();
      status.textContent = "Details selected. Press Ctrl+C to copy, then paste into Lite.";
    }
  });
})();
