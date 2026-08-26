/* Public Firebase web config - not a secret: it names the project, and access
   is governed by Firebase's authorized-domains list plus the server checking
   every token's signature. Copy the values from the Firebase console:
   Project settings → General → Your apps → SDK setup and configuration.
   An empty apiKey means "not set up": the login page says so, and the
   stories board simply has nobody logged in. */
export const config = {
  apiKey: "AIzaSyAxOg0am2kjbsRvOi8CiLKH9VYmbTmszFA",
  authDomain: "delaybahndb.firebaseapp.com",
  projectId: "delaybahndb",
  appId: "1:859714693884:web:91f58e45ec0ca2ab714579",
};

/* Which ways in the login page offers. Each must also be enabled under
   Authentication → Sign-in method; apple additionally needs the Apple
   developer setup, and phone needs the Blaze plan. Email + password is
   always offered. */
export const providers = {
  google: true,
  apple: false,
  phone: false,
};
