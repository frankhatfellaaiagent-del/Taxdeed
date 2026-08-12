# Claude Code handoff — Tax Deed Dashboard

Design file: `Tax Deed Dashboard.dc.html` (+ `ds/styles.css`, the Industry design-system tokens/classes — keep it as the single stylesheet).

## What the UI expects from the backend

**Property record** (one row per scraped parcel):
`{ id, parcel, addr, city, county, type, assessed, bid (opening/minimum), auction (date), lot, sqft, owner, liens, zoning, photoUrl, appraiserUrl, clerkUrl, taxCollectorUrl, lat, lng }`
Counties: the 36 FL counties the scraper covers.

**User/team state** (persist per team):
- `interest[propertyId] = { status: 'Interested'|'Watching'|'Pass', bid: number (max willing to pay), list: string, notes: string }`
- `lists[] = { name, description }`
- `settings = { myTypes: {type: bool}, myTypesFirst: bool, budgetDefault, scheduleDay }`

## Wiring points (all currently mocked in the DC logic class)

1. **Run scrape now** (top bar) → trigger the existing weekly automation manually; button shows "Scraping…" while running. Feed results into Scrape history.
2. **Export Excel** (top bar) → generate .xlsx of the current filtered table.
3. **Auto-scrape schedule** → Settings lets the user pick the weekly day (default Mon 6:00 AM ET); sidebar shows next run.
4. **Table filters** → county, property type, max opening bid, free-text search; "My types first" sorts preferred types to the top.
5. **Property detail links** → real per-county Property Appraiser, Clerk of Court case, Tax Collector URLs.
6. **Map** → interactive/static toggle; suggested: Leaflet + OpenStreetMap (free) or Google Maps embed for interactive, Static Maps API for static. Placeholder divs marked with class `ph`.
7. **AI research card** → agent output per property: appraisal summary, FEMA flood zone, liens found at clerk/code enforcement, comps. "Re-run research" re-invokes the agent.
8. **Scrape history** → one row per run: date, trigger (auto/manual), counties covered, new records, updated, duration, status.

## Design system rules
- All colors/fonts/spacing come from `ds/styles.css` variables; components use its classes (`.btn`, `.table`, `.tag`, `.input`, `.blueprint` + corner marks).
- Square corners everywhere; cards are transparent hairline-bordered "blueprint" objects; the solid accent primary button is the only filled object.
- Icons: Lucide, stroke-width 1.5.
