# Themes

Three palettes ship: **Leather** (the original), **Admiralty** (blue-black with
polished brass, which is what the compass mark was always drawing) and **Ledger**
(deep green buckram and aged gilt). The edition chip beside the title is the
control — it names the active theme and cycles on click — and the choice rides in
the URL hash, so a reload keeps it and a link carries it. No localStorage: this
page persists no other preference that way, and a setting the URL cannot express
is one that disagrees with a shared link.

**A theme has to be visibly different, and that is measured.** The first release
shipped an "Oxblood" whose 48 colours were all within 24/255 per channel of
Leather's — every structural check passed and clicking the chip appeared to do
nothing. Admiralty had the same defect in the two places a reader looks first: the
logo plate and the currency pill stayed brown on a blue-black page.
`test_each_theme_is_visibly_distinct_from_the_default` now measures landmark
distance, and judges the near-black grounds on which channel dominates rather than
absolute distance, since every `--bg` here sits a few points from zero.

The whole mechanism is that **every colour is a custom property, and nothing
outside the theme blocks holds a colour literal.** That had to be earned rather
than declared: 35 hex literals were scattered through the rules — the logo
facets, the edition pill, calendar day borders, put/call, the impact dots — so a
theme swap left brown chrome sitting on a blue page. Promoting them is why the
palette is 68 names long; that is the honest size of this page's colour
vocabulary. `test_no_colour_literal_lives_outside_a_theme_block` keeps it that
way, and `test_every_theme_declares_the_same_palette` keeps a theme a *swap*
rather than a patch — a block missing a name inherits it from `:root`, which
renders one theme's chrome on another's ground, silently and only on the panels
that use it.

**`app.css` is not the only place a colour can hide**, and checking only the
stylesheet is why the performance chart sat out the first release entirely. Its
line and fill were SVG `stroke`/`stop-color` attributes holding hexes, and each
dot was ringed in `#0a0806` — Leather's own *background*, so on Admiralty the
dots wore a brown the page no longer contained. Worse, `note()` set
`style.background` and `style.color` from a literal table: an inline style
outranks every rule, so the message banner could not have been themed even with
the right variable in place. Both were reported from the running app rather than
caught, and `test_no_colour_literal_lives_in_the_page_either` now scans
`page.html` for the same reason the CSS is scanned.

**Usability is measured, not asserted.** The contrast tests recompute WCAG
ratios from the stylesheet for every theme, so retuning a palette is free while
regressing legibility is a red test. Adding the second theme immediately caught
that the original `test_muted_text_...` had been silently checking only the first
block in the file. Between them, the checks found four real defects that reading
the CSS would not have: `--onaccent` failed AA on its own fill in two themes (one
of which, Leather's 4.31:1, predated the themes entirely), and both new themes
shipped a selected tab too close to its neighbours to read as selected — that one
found by **screenshot**, since every text-contrast figure passed while the tab
strip had stopped saying where you were.

**Adding a theme**: one `[data-theme="yourname"]` block in `app.css` declaring
every name `:root` declares → one entry in `page.html`'s `THEMES` table. No
JavaScript to touch, and the tests will tell you which names you missed and which
ratios you broke.

A light theme is the obvious next one and is deliberately not here: it inverts
the bevels (`--bevel*` and `--drop*` assume a lit-from-above dark surface) and
needs every gain/loss hue re-derived, since mint-on-paper fails contrast badly.
The variables it needs already exist, which was the point of naming them.
