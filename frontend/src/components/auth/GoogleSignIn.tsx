import { GoogleLogin } from "@react-oauth/google";
import { useAuth } from "../../context/AuthContext";

const googleClientId = import.meta.env.VITE_GOOGLE_CLIENT_ID as string | undefined;

/**
 * The Google button plus its "or" divider. Shared by Login and Register.
 *
 * Google makes no distinction between the two — the same ID token signs you in,
 * creates an account, or links to an existing one, and the server decides
 * which. So both screens render exactly this.
 *
 * Renders nothing at all when no Client ID is configured, which is what lets
 * the app run without Google set up.
 */
export default function GoogleSignIn() {
  const { googleLogin } = useAuth();

  if (!googleClientId) return null;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-center">
        <GoogleLogin
          onSuccess={(response) => {
            // `credential` is the ID token. It's optional in the type because
            // some flows omit it, so guard rather than assert.
            if (response.credential) {
              // Errors land in AuthContext's `error` and render on the form.
              void googleLogin(response.credential).catch(() => {});
            }
          }}
          onError={() => {
            // Fired when Google's own flow fails before we get a credential.
          }}
        />
      </div>

      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-border" />
        <span className="text-xs uppercase tracking-wide text-text-muted">or</span>
        <span className="h-px flex-1 bg-border" />
      </div>
    </div>
  );
}
