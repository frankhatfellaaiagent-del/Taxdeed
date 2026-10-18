/* Session-local write ordering and three-way replay. No network or credentials here. */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TaxDeedState = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const clone = (v) => v === undefined ? undefined : JSON.parse(JSON.stringify(v));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const object = (v) => v && typeof v === "object" && !Array.isArray(v);
  const safe = (k) => !["__proto__", "prototype", "constructor"].includes(k);

  // Apply only the local changes since base onto freshly read remote state.
  // Named lists merge by name; untouched remote lists/fields/memberships survive.
  function rebase(base, local, remote) {
    if (equal(base, local)) return clone(remote);
    if (Array.isArray(base) && Array.isArray(local) && Array.isArray(remote) &&
        [...base, ...local, ...remote].every((v) => object(v) && typeof v.name === "string")) {
      const before = new Map(base.map((v) => [v.name, v]));
      const after = new Map(local.map((v) => [v.name, v]));
      const result = new Map(remote.map((v) => [v.name, clone(v)]));
      for (const name of new Set([...before.keys(), ...after.keys()])) {
        if (equal(before.get(name), after.get(name))) continue;
        if (!after.has(name)) result.delete(name);
        else result.set(name, rebase(before.get(name) || {}, after.get(name), result.get(name) || {}));
      }
      return [...result.values()];
    }
    if (object(base) && object(local)) {
      const out = object(remote) ? clone(remote) : {};
      for (const key of new Set([...Object.keys(base), ...Object.keys(local)])) {
        if (!safe(key) || equal(base[key], local[key])) continue;
        if (!Object.prototype.hasOwnProperty.call(local, key)) delete out[key];
        else out[key] = rebase(base[key], local[key], out[key]);
      }
      return out;
    }
    return clone(local);
  }

  function createQueue({ current, write, changed = () => {}, status = () => {}, delay = 800, initial = {} }) {
    const pending = new Map(), failures = new Map();
    let active = null, timer = null, ready = false, cancelled = false, revision = 0, wrote = false;
    // Restore the complete journal before any new edit can persist a snapshot.
    for (const [name, entry] of Object.entries(initial)) {
      if (entry && Object.prototype.hasOwnProperty.call(entry, "data")) pending.set(name, {
        data: clone(entry.data), base: clone(entry.base), seed: !!entry.seed, revision: ++revision,
      });
    }
    const valid = () => !cancelled && current();
    const notify = () => { if (valid()) status(failures.size ? "error" : pending.size ? "saving" : wrote ? "saved" : "idle"); };
    const snapshot = () => Object.fromEntries([...pending].map(([key, value]) => [key, clone(value)]));
    const persist = () => { if (valid()) changed(snapshot()); };
    function schedule() {
      clearTimeout(timer);
      if (ready && valid()) timer = setTimeout(() => { timer = null; flush(); }, delay);
    }
    function enqueue(name, data, options = {}) {
      if (!valid()) return false;
      const previous = pending.get(name);
      pending.set(name, { data: clone(data), base: clone(previous && !options.replaceBase ? previous.base : options.base),
        seed: !!options.seed, revision: ++revision });
      failures.delete(name);
      persist(); notify(); schedule();
      return true;
    }
    async function flush() {
      clearTimeout(timer); timer = null;
      if (!ready || !valid()) return false;
      if (active) return active;
      active = (async () => {
        while (valid()) {
          const next = [...pending].find(([name]) => !failures.has(name));
          if (!next) break;
          const [name, entry] = next;
          let error = null;
          try { const result = await write(name, clone(entry.data), entry); error = result && result.error; }
          catch (e) { error = e; }
          if (!valid()) return false;
          if (error) { if (pending.get(name) === entry) failures.set(name, error); }
          else {
            wrote = true;
            // An older acknowledgement must never delete a newer edit.
            if (pending.get(name) === entry) pending.delete(name);
            // A newer edit expresses changes relative to the acknowledged
            // payload, including reverting a value or removing a just-added
            // list. On failure its original baseline must stay intact.
            else if (pending.has(name)) pending.get(name).base = clone(entry.data);
          }
          persist(); notify();
        }
        return valid() && pending.size === 0;
      })();
      try { return await active; }
      finally { active = null; notify(); }
    }
    return {
      enqueue, flush, snapshot,
      start() { ready = true; return flush(); },
      retry() { failures.clear(); ready = true; notify(); return flush(); },
      has(name) { return pending.has(name); },
      discard(name) { if (!valid()) return; pending.delete(name); failures.delete(name); persist(); notify(); },
      cancel() { cancelled = true; ready = false; clearTimeout(timer); },
    };
  }
  return { clone, equal, rebase, createQueue };
});
