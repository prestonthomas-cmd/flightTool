'use client';

import { useRef } from 'react';

/**
 * The x on a card. It opens a dialog rather than deleting on click, because
 * removing a watch throws away a history that took weeks to collect and a
 * mis-tap should not be able to do that.
 */
export function RemoveButton({
  id,
  name,
  action,
}: {
  id: string;
  name: string;
  action: (form: FormData) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);

  return (
    <>
      <button
        type="button"
        className="drop"
        aria-label={`Stop watching ${name}`}
        title="Stop watching"
        onClick={() => dialog.current?.showModal()}
      >
        ×
      </button>

      <dialog ref={dialog}>
        <form action={action} className="inner">
          <input type="hidden" name="id" value={id} />
          <h3>Stop watching this flight?</h3>
          <p className="why">{name}</p>

          <label className="check">
            <input type="checkbox" name="purge" />
            Also delete the prices collected so far
          </label>
          <p className="why">
            Leave that unticked and the history is kept, so adding the same
            flight back later picks up where it left off.
          </p>

          <div className="actions">
            <button type="button" onClick={() => dialog.current?.close()}>
              Cancel
            </button>
            <button type="submit" className="go danger">
              Stop watching
            </button>
          </div>
        </form>
      </dialog>
    </>
  );
}
