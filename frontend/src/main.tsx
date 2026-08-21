import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { GoogleOAuthProvider } from "@react-oauth/google";
import "./index.css";
import App from "./App";

// Vite replaces import.meta.env.VITE_* at BUILD time, not at run time — the
// value is baked into the bundle, so changing it means rebuilding. That's fine
// here: a Google Client ID is public by design, not a secret.
const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined;

const rootNode = <App />;

// The provider is conditional so the app still boots with no Client ID
// configured — email/password sign-in keeps working and the Google button
// simply isn't rendered.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {googleClientId ? (
      <GoogleOAuthProvider clientId={googleClientId}>{rootNode}</GoogleOAuthProvider>
    ) : (
      rootNode
    )}
  </StrictMode>,
);
