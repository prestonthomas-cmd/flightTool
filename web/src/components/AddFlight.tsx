'use client';

import { useActionState, useEffect, useRef, useState } from 'react';
import { useFormStatus } from 'react-dom';
import { addWatch, type WatchState } from '@/lib/watch-actions';

function Submit() {
  const { pending } = useFormStatus();
  return (
    <button type="submit" className="go" disabled={pending}>
      {pending ? 'Adding...' : 'Add flight'}
    </button>
  );
}

export function AddFlight() {
  const dialog = useRef<HTMLDialogElement>(null);
  const form = useRef<HTMLFormElement>(null);
  const [state, run] = useActionState<WatchState, FormData>(addWatch, {});
  const [roundTrip, setRoundTrip] = useState(true);
  const [flexible, setFlexible] = useState(false);

  // Clear the form once the server has accepted it, so the dialog is ready
  // for the next flight rather than still holding the last one.
  useEffect(() => {
    if (state.ok) form.current?.reset();
  }, [state.ok]);

  return (
    <>
      <button type="button" className="manage" onClick={() => dialog.current?.showModal()}>
        + Add a flight
      </button>

      <dialog ref={dialog}>
        <form ref={form} action={run} className="inner">
          <h3>Add a flight</h3>
          <p className="why">It starts collecting prices on the next run.</p>

          <div className="pair">
            <div className="field">
              <label htmlFor="origin">From</label>
              <input id="origin" name="origin" placeholder="JFK" maxLength={3}
                     required autoCapitalize="characters" autoComplete="off" />
            </div>
            <div className="field">
              <label htmlFor="destination">To</label>
              <input id="destination" name="destination" placeholder="HND" maxLength={3}
                     required autoCapitalize="characters" autoComplete="off" />
            </div>
          </div>

          <div className="pair">
            <div className="field">
              <label htmlFor="depart_from">Depart</label>
              <input id="depart_from" name="depart_from" type="date" required />
            </div>
            {roundTrip ? (
              <div className="field">
                <label htmlFor="return_from">Return</label>
                <input id="return_from" name="return_from" type="date" required />
              </div>
            ) : null}
          </div>

          <label className="check">
            <input type="checkbox" checked={roundTrip}
                   onChange={(e) => setRoundTrip(e.target.checked)} />
            Round trip
          </label>

          <label className="check">
            <input type="checkbox" checked={flexible}
                   onChange={(e) => setFlexible(e.target.checked)} />
            My dates are flexible
          </label>

          {flexible ? (
            <div className="pair">
              <div className="field">
                <label htmlFor="depart_to">Depart by</label>
                <input id="depart_to" name="depart_to" type="date" />
              </div>
              {roundTrip ? (
                <div className="field">
                  <label htmlFor="return_to">Return by</label>
                  <input id="return_to" name="return_to" type="date" />
                </div>
              ) : null}
            </div>
          ) : null}

          <div className="pair">
            <div className="field">
              <label htmlFor="cabin">Cabin</label>
              <select id="cabin" name="cabin" defaultValue="economy">
                <option value="economy">Economy</option>
                <option value="premium-economy">Premium economy</option>
                <option value="business">Business</option>
                <option value="first">First</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="adults">Adults</label>
              <input id="adults" name="adults" type="number" min={1} max={9} defaultValue={1} />
            </div>
          </div>

          <div className="field">
            <label htmlFor="label">Name it <span className="why">optional</span></label>
            <input id="label" name="label" placeholder="Tokyo at Christmas" maxLength={80} />
          </div>

          <div className="field">
            <label htmlFor="max_price">Always alert below <span className="why">optional</span></label>
            <input id="max_price" name="max_price" type="number" min={1} placeholder="900" />
          </div>

          <label className="check">
            <input type="checkbox" name="nonstop" />
            Nonstop only
          </label>

          {state.error ? <p className="said bad" role="alert">{state.error}</p> : null}
          {state.ok ? <p className="said" role="status">{state.ok}</p> : null}

          <div className="actions">
            <button type="button" onClick={() => dialog.current?.close()}>Close</button>
            <Submit />
          </div>
        </form>
      </dialog>
    </>
  );
}
