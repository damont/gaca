import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";

// The single place React attaches to the page. index.html ships one empty
// <div id="root">; everything else is built by React at runtime.
//
// StrictMode is development-only. It deliberately runs certain code twice —
// including effects — to surface bugs that depend on running exactly once. If
// something appears to happen twice in dev and once in a production build,
// this is why.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
