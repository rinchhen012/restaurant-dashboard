self.addEventListener("push", (event) => {
  let title = "New order";
  let body = "";
  try {
    const data = event.data.json();
    title = data.title || title;
    body = data.body || "";
  } catch (_) {}
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      tag: "dash-new-order",
      renotify: true
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ("focus" in client) return client.focus();
      }
      return clients.openWindow("/");
    })
  );
});