// Intentionally minimal. The MIKA renderer is the existing web app and talks to the backend
// over HTTP (API_BASE === window.location.origin === the localhost sidecar), so no IPC bridge
// is needed. Kept as a file so contextIsolation has a defined (empty) preload.
