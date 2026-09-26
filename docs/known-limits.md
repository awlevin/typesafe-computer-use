# Known limits

- OCR only sees text, and the accessibility tree only covers apps that publish one.
  In a terminal, a canvas, or Spotify, an icon-only button reaches neither source.
- Two identical labels in one row, or in no row at all, get only a coarse region hint and
  split the vote. Ones in different rows are told apart by the text beside them.
- Only the main display is captured.
- A repeated action whose effect never shows on screen (a third "New note" in an app that
  lists nothing) reads as a cycle and stops the run: the capture is the only witness.
- Using the machine during an `--act` run fights it for focus and the cursor.
- The site catalog is small on purpose; the writer covers the rest.
- Stacked short lines merge into one item, so a list of checkboxes ("Arrives in 2-4
  days", "Free Shipping", "Local Pickup") that the app does not publish through
  accessibility is one click target, aimed at its middle.
