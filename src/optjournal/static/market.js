/* Pure selection policy for the Market tab.
 *
 * The DOM rendering stays in page.html. These functions own the state rules that
 * are easy to get subtly wrong: an empty filter means "all", defaults come from
 * the payload, and a hidden selected day must heal to the next visible event.
 */

export function seedFacets(chosen, facets) {
  return chosen instanceof Set
    ? chosen
    : new Set((facets || []).filter((facet) => facet.default).map((facet) => facet.value));
}

export function eventPasses(event, countries, impacts) {
  return (countries.size === 0 || countries.has(event.country))
    && (impacts.size === 0 || impacts.has(event.impact));
}

export function facetsAtDefault(chosen, facets) {
  const wanted = (facets || []).filter((facet) => facet.default).map((facet) => facet.value);
  return chosen.size === wanted.length && wanted.every((value) => chosen.has(value));
}

export function chooseMarketDay(visibleDays, today, selected) {
  if (selected && visibleDays.includes(selected)) return selected;
  const ahead = visibleDays.filter((day) => day >= today);
  if (ahead.length) return ahead[0];
  const behind = visibleDays.filter((day) => day < today);
  return behind.length ? behind[behind.length - 1] : null;
}
