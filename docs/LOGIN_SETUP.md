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

Note each user's **UID** (shown in the Users table) — you need it in the next
steps to wire team membership.

## 4. Create the database and paste the rules

1. **Build → Firestore Database → Create database** → production mode →
   location `nam5 (United States)` → Enable.
2. **Rules** tab → replace everything with the contents of
   [`firestore.rules`](../firestore.rules) from this repo → **Publish**.

## 4b. Create the team and assign the accounts to it

The dashboard is multi-tenant: every customer is a **team**, and teams are
fully isolated from each other — one customer can never see another's lists.
For each team (start with MADD, id `madd`):

1. Firestore → **Start collection** → id `teams` → document id `madd` → fields:
   - `name` (string): `MADD Assets`
   - `members` (map): one entry per member — key = the user's **UID** from
     step 3, value = `true` (boolean).
2. For each member, also create their profile doc: collection `users` →
   document id = that user's **UID** → field `team` (string): `madd`.

Onboarding a NEW customer later = repeat steps 3 + 4b with a new team id
(e.g. `acme`): create their accounts, create `teams/acme` with their UIDs in
`members`, and set `team: acme` on each of their `users/{uid}` profile docs.
Nothing else changes — same dashboard, same feed, isolated workspace.

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
