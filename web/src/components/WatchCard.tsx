import { removeWatch } from '@/lib/watch-actions';
import { plot, money, type Point, type BandPoint } from '@/lib/chart';
import { watchName, watchRoute, watchWindow, type Price, type Projection, type Watch } from '@/lib/types';
import { RemoveButton } from '@/components/RemoveButton';

const DAY = 24 * 60 * 60 * 1000;

export function WatchCard({
  watch,
  prices,
  projection,
}: {
  watch: Watch;
  prices: Price[];
  projection: Projection[];
}) {
  const currency = prices[0]?.currency ?? 'USD';
  const latest = prices.length ? prices[prices.length - 1] : null;

  // One price per day: the cheapest, which is what the watch is actually
  // tracking. A range watch sees several itineraries per run and showing all
  // of them would draw a band of noise rather than a line.
  const perDay = new Map<string, number>();
  for (const row of prices) {
    const day = row.observed_at.slice(0, 10);
    const seen = perDay.get(day);
    if (seen === undefined || row.price < seen) perDay.set(day, row.price);
  }

  const days = [...perDay.keys()].sort();
  const origin = days.length ? Date.parse(days[0]) : 0;
  const actual: Point[] = days.map((day) => ({
    x: (Date.parse(day) - origin) / DAY,
    y: perDay.get(day)!,
  }));

  const predicted: Point[] = projection.map((p) => ({
    x: (Date.parse(p.day) - origin) / DAY,
    y: Number(p.price),
  }));

  // The band starts pinned to the last recorded point, so it grows out of the
  // line instead of appearing beside it.
  const band: BandPoint[] = actual.length
    ? [
        {
          x: actual[actual.length - 1].x,
          low: actual[actual.length - 1].y,
          high: actual[actual.length - 1].y,
        },
        ...projection.map((p) => ({
          x: (Date.parse(p.day) - origin) / DAY,
          low: Number(p.low),
          high: Number(p.high),
        })),
      ]
    : [];

  const drawn = plot(actual, predicted, band, currency);
  const cheapest = prices.length ? Math.min(...prices.map((p) => p.price)) : null;
  const flagged =
    latest !== null && cheapest !== null && Number(latest.price) <= cheapest;

  return (
    <section className={`card${flagged ? ' flagged' : ''}`}>
      <div className="head">
        <div>
          <h2>{watchName(watch)}</h2>
          <p className="route">
            {watchRoute(watch)} · {watch.cabin}
            {watch.adults > 1 ? ` · ${watch.adults} adults` : ''}
            {watch.max_stops === 0 ? ' · nonstop' : ''}
          </p>
        </div>
        <div className="head-right">
          {flagged ? <span className="pill buy">Lowest yet</span> : null}
          <RemoveButton id={watch.id} name={watchName(watch)} action={removeWatch} />
        </div>
      </div>

      <p className="now">
        {latest ? money(Number(latest.price), currency) : 'No price yet'}
        <span className="when"> · {watchWindow(watch)}</span>
      </p>

      {drawn ? (
        <>
          <svg className="chart" viewBox="0 0 720 260" role="img"
               aria-label={`Price history and projection for ${watchName(watch)}`}>
            {drawn.band ? <polygon className="band" points={drawn.band} /> : null}
            {drawn.gridlines.map((line) => (
              <g key={line.label}>
                <line className="grid" x1={60} y1={line.y} x2={702} y2={line.y} />
                <text className="tick" x={52} y={line.y + 4} textAnchor="end">
                  {line.label}
                </text>
              </g>
            ))}
            {drawn.median ? (
              <line className="reference" x1={60} y1={drawn.median.y} x2={702} y2={drawn.median.y} />
            ) : null}
            <path className="series" d={drawn.line} />
            {drawn.projected ? <path className="series projected" d={drawn.projected} /> : null}
            {drawn.end ? (
              <circle className="marker projected-end" cx={drawn.end.x} cy={drawn.end.y} r={4} />
            ) : null}
            {drawn.lowest ? (
              <circle className="marker highlight" cx={drawn.lowest.x} cy={drawn.lowest.y} r={5} />
            ) : null}
            <line className="axis" x1={60} y1={228} x2={702} y2={228} />
          </svg>
          <div className="legend">
            <span><i className="key" />Recorded</span>
            {predicted.length ? <span><i className="key dashed" />Projected</span> : null}
            {band.length > 1 ? <span><i className="key band" />Range</span> : null}
          </div>
        </>
      ) : (
        <p className="empty">
          {prices.length
            ? 'One price so far — a second one makes a line.'
            : 'Waiting for the first run to collect a price.'}
        </p>
      )}
    </section>
  );
}
