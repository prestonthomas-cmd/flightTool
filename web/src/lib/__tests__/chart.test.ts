import test from 'node:test';
import assert from 'node:assert/strict';
import { niceStep, money, plot, type Point } from '../chart.ts';

test('niceStep picks round numbers a reader can hold in their head', () => {
  assert.equal(niceStep(1000), 250);
  assert.equal(niceStep(10), 2.5);
  assert.equal(niceStep(0), 1);
});

test('money rounds and groups', () => {
  assert.equal(money(1234.6, 'USD'), 'USD 1,235');
});

test('a single observation is not a line', () => {
  assert.equal(plot([{ x: 0, y: 100 }], [], [], 'USD'), null);
});

test('a flat series still renders instead of dividing by zero', () => {
  const flat: Point[] = [
    { x: 0, y: 500 },
    { x: 1, y: 500 },
  ];
  const result = plot(flat, [], [], 'USD');
  assert.ok(result);
  assert.ok(result.line.includes('M'));
  assert.ok(Number.isFinite(result.lowest!.y));
});

test('the projection starts exactly where the recorded line ends', () => {
  // Otherwise the dashed line floats detached from the solid one, which is
  // what it did the first time this was built.
  const actual: Point[] = [
    { x: 0, y: 100 },
    { x: 1, y: 110 },
  ];
  const result = plot(actual, [{ x: 5, y: 130 }], [], 'USD')!;

  const lastDrawn = result.line.split('L').pop()!.trim();
  const firstProjected = result.projected.slice(1).split('L')[0].trim();
  assert.equal(firstProjected, lastDrawn);
});

test('only the most recent low is marked', () => {
  const tied: Point[] = [
    { x: 0, y: 100 },
    { x: 1, y: 100 },
    { x: 2, y: 120 },
  ];
  const result = plot(tied, [], [], 'USD')!;
  const second = plot([tied[1]], [], [], 'USD');
  assert.equal(second, null);
  // The marker sits at the later of the two tied points, not the first.
  assert.ok(result.lowest!.x > 60);
});
