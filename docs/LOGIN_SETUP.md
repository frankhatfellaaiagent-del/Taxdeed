# Turning on team login + cross-device sync

The dashboard ships with login built in but **dormant** — until the config below
is pasted in, it runs exactly as before (everything saved in the browser only).
This is Frank's one-time setup, about 10 minutes, all in the browser.

What the team gets once it's on: each person signs in with an email + password,
and their lists, notes, max bids and Interested/Watching/Pass marks follow them
to any device. Lists marked **Shared with team** are live for everyone — a
parcel Jennifer adds on her phone shows up on Marlon's laptop.

## 1. Create the Firebase project (free)

1. Go to <https://console.firebase.google.com> (any Google account) → **Add project**.
2. Name it e.g. `madd-taxdeed`. Google Analytics: **off** (not needed). Create.

## 2. Turn on email/password sign-in

1. In the project: **Build → Authentication → Get started**.
2. **Sign-in method** tab → **Email/Password** → Enable → Save.
3. Do **NOT** enable anything that lets people self-register (no "anonymous",
   no third-party providers). The dashboard has no sign-up form — the accounts
   you create here are the entire access list.

## 3. Create the team's accounts

Authentication → **Users** tab → **Add user** — one per person (Frank, Marlon,
Jennifer), each with their email and a starting password. Tell them the
password; the dashboard has a **Forgot password?** link that emails a reset,
so they can change it themselves.

## 4. Create the database and paste the rules

1. **Build → Firestore Database → Create database** → production mode →
   location `nam5 (United States)` → Enable.
2. **Rules** tab → replace everything with the contents of
   [`firestore.rules`](../firestore.rules) from this repo → **Publish**.

## 5. Allow the dashboard's domain

**Authentication → Settings → Authorized domains → Add domain** →
`frankhatfellaaiagent-del.github.io`
(add your custom domain here too if one is attached later).

## 6. Paste the config into the dashboard

1. Project settings (gear icon) → **General** → *Your apps* → **</>** (Web) →
   register app (name `dashboard`, no hosting) → copy the `firebaseConfig`
   object it shows.
2. In this repo, edit `dashboard/index.html` and replace the empty object at
   `const FIREBASE_CONFIG = {};` with what you copied, e.g.:

   ```js
   const FIREBASE_CONFIG = {
     apiKey: "AIza…",
     authDomain: "madd-taxdeed.firebaseapp.com",
     projectId: "madd-taxdeed",
     storageBucket: "madd-taxdeed.appspot.com",
     messagingSenderId: "…",
     appId: "…",
   };
   ```

3. Commit and push — the Pages deploy makes it live. A **Sign in** button
   appears in the dashboard sidebar.

This config is **not a secret** (every Firebase web app ships it publicly);
access is controlled by the rules from step 4 and the closed account list from
step 3.

## Checking it worked

Sign in on two devices (or one normal + one private browser window) with the
same account: add a note on one — it appears on the other within a second or
two. Make a list **Shared with team**, sign in as a different account, and the
list is there.

## Costs

Firebase's free tier (Spark) covers this comfortably: it allows ~50k reads and
~20k writes **per day**; a three-person team touching a few hundred parcels a
week uses a fraction of one percent of that. No credit card required.
