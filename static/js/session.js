// Auto-refresh attendance count every 5 seconds
(function () {
  const script = document.currentScript;
  const sessionId = script ? script.dataset.sessionId : null;
  if (!sessionId) return;

  const countEl = document.getElementById("live-count");
  const emptyMsg = document.getElementById("empty-msg");

  function refresh() {
    fetch(`/api/sessions/${sessionId}/count`)
      .then((r) => r.json())
      .then((data) => {
        if (countEl) countEl.textContent = data.count + "명";
      })
      .catch(() => {});
  }

  setInterval(refresh, 5000);
})();
