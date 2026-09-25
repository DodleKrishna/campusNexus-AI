import type { KeyboardEvent } from "react";

/** Activates a clickable table row from the keyboard (Enter / Space) as well as the mouse. */
export const rowKeys = (open: () => void) => (event: KeyboardEvent) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    open();
  }
};
