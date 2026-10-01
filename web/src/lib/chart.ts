/**
 * The chart's geometry. A direct port of the Python dashboard's, so the page
 * on the web and the page the scraper writes draw the same picture.
 *
 * Pure arithmetic — no DOM, no React — so it can be unit tested without a
 * browser, which is most of why it is a separate file.
 */

export type Point = { x: number; y: number };
export type BandPoint = { x: number; low: number; high: number };

export const WIDTH = 720;
export const HEIGHT = 260;
const LEFT = 60;
const RIGHT = 18;
const TOP = 16;
const BOTTOM = 32;

export type Plot = {
  line: string;
  projected: string;
  band: string;
  gridlines: { y: number; label: string }[];
  ticks: { x: number; label: string }[];
  median: { y: number } | null;
  lowest: Point | null;
  end: Point | null;
};

/** A round number to step the axis by, so labels read 500 rather than 437. */
export function niceStep(span: number, target = 4): number {
  if (span <= 0) return 1;
  const rough = span / target;
  const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
  for (const multiple of [1, 2, 2.5, 5, 10]) {
    const step = magnitude * multiple;
    if (step >= rough) return step;
  }
  return magnitude * 10;
}

export function money(value: number, currency: string): string {
  return `${currency} ${Math.round(value).toLocaleString('en-US')}`;
}

/**
 * Lay out one watch's history and its projection.
 *
 * `actual` and `predicted` are in the same x space: days since the first
 * observation. Sharing one axis is what lets the dashed line continue the
 * solid one rather than floating beside it.
 */
export function plot(
  actual: Point[],
  predicted: Point[],
  band: BandPoint[],
  currency: string,
): Plot | null {
  if (actual.length < 2) return null;

  const xs = [...actual, ...predicted].map((p) => p.x);
  const ys = [
    ...actual.map((p) => p.y),
    ...predicted.map((p) => p.y),
    ...band.flatMap((p) => [p.low, p.high]),
  ];

  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);

  // A flat series has no range to scale against, so give it one rather than
  // dividing by zero and drawing a line through the middle of nowhere.
  const ySpan = yMax - yMin || Math.max(yMax * 0.1, 1);
  const xSpan = xMax - xMin || 1;

  const px = (x: number) => LEFT + ((x - xMin) / xSpan) * (WIDTH - LEFT - RIGHT);
  const py = (y: number) =>
    HEIGHT - BOTTOM - ((y - yMin) / ySpan) * (HEIGHT - TOP - BOTTOM);

  const path = (points: Point[]) =>
    points.map((p, i) => `${i ? 'L' : 'M'}${px(p.x).toFixed(1)} ${py(p.y).toFixed(1)}`).join(' ');

  // The band is one polygon: along the top, then back along the bottom.
  const bandPath = band.length
    ? [
        ...band.map((p) => `${px(p.x).toFixed(1)},${py(p.high).toFixed(1)}`),
        ...[...band].reverse().map((p) => `${px(p.x).toFixed(1)},${py(p.low).toFixed(1)}`),
      ].join(' ')
    : '';

  const step = niceStep(ySpan);
  const gridlines: { y: number; label: string }[] = [];
  for (let value = Math.ceil(yMin / step) * step; value <= yMax; value += step) {
    gridlines.push({ y: py(value), label: money(value, currency) });
  }

  const sorted = [...actual.map((p) => p.y)].sort((a, b) => a - b);
  const mid = sorted.length
    ? sorted.length % 2
      ? sorted[(sorted.length - 1) / 2]
      : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2
    : null;

  // Only the most recent low is marked. On a fare that has not moved every
  // point ties the minimum, and marking them all turns the line green and
  // says nothing.
  let lowest: Point | null = null;
  const best = Math.min(...actual.map((p) => p.y));
  for (const point of actual) {
    if (point.y === best) lowest = { x: px(point.x), y: py(point.y) };
  }

  return {
    line: path(actual),
    projected: predicted.length ? path([actual[actual.length - 1], ...predicted]) : '',
    band: bandPath,
    gridlines,
    ticks: [],
    median: mid === null ? null : { y: py(mid) },
    lowest,
    end: predicted.length
      ? { x: px(predicted[predicted.length - 1].x), y: py(predicted[predicted.length - 1].y) }
      : null,
  };
}
