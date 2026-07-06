(function () {
  const status = document.getElementById('status');
  const ws = new WebSocket('ws://' + location.host + '/nh/v1/ws');
  ws.onopen = () => status.textContent = 'Connected';
  ws.onclose = () => status.textContent = 'Disconnected';
  ws.onerror = () => status.textContent = 'Error';
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    console.log(msg.type);
  };
})();
