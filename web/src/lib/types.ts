export type Watch = {
  id: string;
  origin: string;
  destination: string;
  depart_from: string;
  depart_to: string;
  return_from: string | null;
  return_to: string | null;
  cabin: string;
  adults: number;
  max_stops: number | null;
  max_price: number | null;
  label: string | null;
  active: boolean;
};

export type Price = {
  observed_at: string;
  price: number;
  currency: string;
  depart_date: string;
  return_date: string | null;
};

export type Projection = {
  day: string;
  price: number;
  low: number;
  high: number;
  evidence: number;
};

/** How a watch reads when there is nothing to call it by. */
export function watchName(watch: Watch): string {
  return watch.label || `${watch.origin} to ${watch.destination}`;
}

export function watchRoute(watch: Watch): string {
  const arrow = watch.return_from ? '<->' : '->';
  return `${watch.origin} ${arrow} ${watch.destination}`;
}

export function watchWindow(watch: Watch): string {
  const out =
    watch.depart_from === watch.depart_to
      ? watch.depart_from
      : `${watch.depart_from} to ${watch.depart_to}`;
  if (!watch.return_from) return out;
  const back =
    watch.return_from === watch.return_to
      ? watch.return_from
      : `${watch.return_from} to ${watch.return_to}`;
  return `${out} → ${back}`;
}
