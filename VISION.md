# Vision

Written for people first, so it stays short. Coding agents read it too.

- **The goal is cheaper, faster task completion, not fewer LLMs.** jev supplements or replaces an
  LLM loop where it can do the same job for less.
- **Accuracy comes first.** When jev is truly stuck, it hands the task to an LLM instead of
  guessing.
- **A handoff carries the story.** The LLM gets the goal, what jev did, and how it reached the
  current screen, so it does not start over.
- **Read structure before pixels.** Page markup, WebMCP, and the accessibility tree come first.
  OCR is the fallback: it sees only text, cannot tell a link from the sentence around it, and
  misses icons and logos.
