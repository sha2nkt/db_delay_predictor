/* One Firebase app for every page that deals with an account. Imported as a
   module by login.js, and dynamically by stories.js - only when the visitor
   has signed in before, so the anonymous majority never downloads the SDK.
   `auth` is null when firebase-config.js is still blank. */
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.18.0/firebase-app.js";
import { getAuth } from "https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js";
import { config, providers } from "./firebase-config.js?v=1";

export * from "https://www.gstatic.com/firebasejs/12.18.0/firebase-auth.js";
export { providers };

export const auth = config.apiKey ? getAuth(initializeApp(config)) : null;

/* Whether this browser holds a finished account: signed in, contact proven,
   username claimed. The stories page reads it to decide whether loading the
   SDK is worth it; it is only a hint - the SDK's own state is the truth. */
export function remember(on) {
  try {
    if (on) localStorage.setItem("account", "1");
    else localStorage.removeItem("account");
  } catch (e) { /* private mode: the SDK still knows */ }
}

/* What the token says about the account, in the terms the server uses:
   `verified` is whether the sign-in method proved contact with the person
   (Google and Apple vouch for the address, a phone number was just confirmed
   by SMS, email + password only once the mail was clicked), `name` the
   claimed public username. `refresh` forces a fresh token - needed after a
   verification click or a claim, since claims live inside the signed token
   and only change when it is reissued. */
export async function identity(user, refresh = false) {
  if (refresh) await user.reload();
  const { claims } = await user.getIdTokenResult(refresh);
  const provider = (claims.firebase && claims.firebase.sign_in_provider) || "";
  return {
    verified: !!claims.email_verified || provider === "phone",
    name: claims.handle || null,
    provider,
  };
}
